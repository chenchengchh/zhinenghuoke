from __future__ import annotations

import re
import warnings
from functools import lru_cache
from types import SimpleNamespace
from typing import Dict, List, Tuple

from loguru import logger

from src.common.industry_strategies import get_active_industry_strategy
from src.common.types import ConversionStage, CtaMode
from src.common.unified_knowledge_service import get_unified_knowledge_service


class FallbackReplyService:
    """通用降级回复服务。

    .. deprecated::
        请使用 :class:`src.common.reply_management_service.ReplyManagementService` 替代。

    目标：
    1. 将行业知识尽量交给知识库数据承载；
    2. 代码层只保留跨行业可复用的通用降级策略；
    3. 让 BotService 只负责编排，不再直接内嵌行业话术。
    """

    def __init__(self):
        warnings.warn(
            "FallbackReplyService 已弃用，请使用 ReplyManagementService",
            DeprecationWarning,
            stacklevel=2,
        )

    GREETING_HINTS = ("你好", "您好", "hi", "hello", "在吗", "在不在", "有人吗")
    PRICE_HINTS = ("价格", "多少钱", "费用", "收费", "报价")
    PLAN_HINTS = ("路线", "线路", "行程", "怎么去", "推荐", "方案")
    PROMOTION_HINTS = ("优惠", "折扣", "活动", "促销")
    COMPLAINT_HINTS = ("投诉", "不满", "售后", "问题", "差评")
    COOPERATION_HINTS = ("合作", "加盟", "代理", "对接")
    FALLBACK_SOURCE = "main"
    FALLBACK_MIN_SCORE = 18.0
    TECHNICAL_FALLBACK_REASONS = {
        "circuit_breaker_open",
        "llm_timeout",
        "llm_unavailable",
        "empty_smart_result",
        "executor_saturated",
        "future_none",
    }

    def normalize_knowledge_reply(self, answer: str, customer_name: str = "") -> str:
        """清洗知识库答案，只做跨行业通用的前缀和留资口径去噪。"""
        text = str(answer or "").strip()
        if not text:
            return ""

        if customer_name:
            stale_prefix_pattern = rf"^\s*{re.escape(customer_name)}(?:，|,)?您好[呀啊]?[！!，,\s]*"
            text = re.sub(stale_prefix_pattern, "", text)

        # 清洗历史知识里遗留的示例昵称，而不是继续在主链里硬编码某个行业客户名。
        text = re.sub(r"^\s*chen(?:，|,)?您好[呀啊]?[！!，,\s]*", "", text, flags=re.IGNORECASE)
        text = text.replace("留个微信或其他联系方式", "留个接收资料的联系方式")
        text = text.replace("留下您的联系方式", "留下一个方便接收资料的联系方式")
        return text.strip()

    def build_queries(self, user_message: str) -> List[str]:
        """构造跨行业通用兜底检索查询，不再写入旅游景点/线路名称。"""
        text = str(user_message or "").strip()
        lowered = text.lower()
        queries: List[str] = []

        def _push(query: str) -> None:
            normalized = str(query or "").strip()
            if normalized and normalized not in queries:
                queries.append(normalized)

        _push(text)
        if any(token in lowered for token in self.GREETING_HINTS):
            _push("你好")
            _push("在吗")
        if any(token in text for token in self.PRICE_HINTS):
            _push(f"{text} 价格")
        if any(token in text for token in self.PLAN_HINTS):
            _push(f"{text} 方案")
        if any(token in text for token in self.PROMOTION_HINTS):
            _push(f"{text} 优惠")
        if any(token in text for token in self.COOPERATION_HINTS):
            _push(f"{text} 合作")
        if any(token in text for token in self.COMPLAINT_HINTS):
            _push(f"{text} 处理")
        return queries

    def _is_technical_reason(self, reason: str) -> bool:
        normalized = str(reason or "").strip().lower()
        return normalized in self.TECHNICAL_FALLBACK_REASONS or normalized.startswith("exception:")

    def _infer_reply_objective(self, user_message: str):
        strategy = get_active_industry_strategy()
        signals = strategy.extract_conversion_signals(user_message, history=None) or {}
        stage = strategy.infer_conversion_stage(
            message=user_message,
            intent_result=None,
            signals=signals,
            history=None,
            customer_data=None,
        )
        cta_mode = CtaMode.NONE
        if stage == ConversionStage.RESERVATION:
            cta_mode = CtaMode.RESERVATION_OFFER
        elif stage == ConversionStage.HANDOFF:
            cta_mode = CtaMode.LEAD_CAPTURE
        elif stage == ConversionStage.HIGH_INTENT:
            cta_mode = CtaMode.MATERIAL_OFFER
        required_slots = list(strategy.get_required_slots_for_stage(stage, intent_result=None) or [])
        known_slots = strategy.extract_known_slots(user_message, history=None, customer_data=None) or {}
        missing_slots = [slot for slot in required_slots if not known_slots.get(slot)]
        missing_slots = list(strategy.prioritize_missing_slots(stage, missing_slots, intent_result=None) or missing_slots)
        return strategy, SimpleNamespace(
            conversion_stage=stage,
            cta_mode=cta_mode,
            should_capture_lead=cta_mode == CtaMode.LEAD_CAPTURE,
            should_offer_reservation=cta_mode == CtaMode.RESERVATION_OFFER,
            should_handoff=stage == ConversionStage.HANDOFF,
            missing_slots=missing_slots,
            known_slots=known_slots,
            signals=signals,
        )

    def knowledge_fallback_reply(
        self,
        customer_name: str,
        user_message: str,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Tuple[str, str]:
        """优先从知识库检索数据化的降级答案。"""
        try:
            knowledge_service = get_unified_knowledge_service()
        except Exception as exc:
            logger.debug(f"获取统一知识库服务失败，跳过知识兜底: {exc}")
            return "", ""

        for query in self.build_queries(user_message):
            try:
                results = knowledge_service.search(
                    query,
                    top_k=2,
                    source=self.FALLBACK_SOURCE,
                    use_vector=True,
                    min_score=self.FALLBACK_MIN_SCORE,
                    enterprise_id=str(enterprise_id or "").strip(),
                    schema_id=str(schema_id or "").strip(),
                )
            except Exception as exc:
                logger.debug(f"知识库兜底检索失败(query={query}): {exc}")
                continue
            if not results:
                continue

            best_item = results[0][0]
            answer = self.normalize_knowledge_reply(getattr(best_item, "answer", ""), customer_name)
            if not answer:
                continue
            logger.info(
                f"[兜底回复] knowledge_hit query={query[:30]} "
                f"matched={(getattr(best_item, 'question', '') or query)[:30]} "
                f"source={self.FALLBACK_SOURCE} min_score={self.FALLBACK_MIN_SCORE}"
            )
            return answer, str(getattr(best_item, "question", "") or query)
        return "", ""

    @staticmethod
    def _prepend_customer_greeting(customer_name: str, text: str) -> str:
        normalized = str(text or "").strip()
        if not normalized:
            return ""
        if not customer_name:
            return normalized
        if normalized.startswith(customer_name) or normalized.startswith(f"{customer_name}，") or normalized.startswith(f"{customer_name},"):
            return normalized
        return f"{customer_name}您好！{normalized}"

    def _generic_reply(self, customer_name: str, user_message: str) -> str:
        """无知识命中时返回跨行业策略澄清，不再按关键词拼固定模板。"""
        strategy, objective = self._infer_reply_objective(user_message)
        text = str(
            strategy.build_clarification_reply(objective, user_message, history=None)
            or strategy.build_safe_no_answer_reply("generic_fallback", user_message, history=None)
            or ""
        ).strip()
        if text and not any(marker in text for marker in ("更准确", "更具体", "继续安排")):
            text = f"{text.rstrip('。！？!?')}，您也可以说得更具体一些，我好更准确地继续安排。"
        return self._prepend_customer_greeting(customer_name, text)

    def build_clarification_fallback_reply(self, customer_name: str, user_message: str) -> str:
        strategy, objective = self._infer_reply_objective(user_message)
        text = str(
            strategy.build_clarification_reply(objective, user_message, history=None) or ""
        ).strip()
        if text:
            return self._prepend_customer_greeting(customer_name, text)
        return self._generic_reply(customer_name, user_message)

    def build_conversion_fallback_reply(self, customer_name: str, user_message: str) -> str:
        strategy, objective = self._infer_reply_objective(user_message)
        base = self._generic_reply(customer_name, user_message)
        cta = str(strategy.build_stage_cta(objective, user_message, history=None) or "").strip()
        if cta and cta not in base:
            return f"{base.rstrip()} {cta}".strip()
        return base

    def build_technical_handoff_reply(self, customer_name: str, user_message: str, reason: str) -> str:
        strategy, objective = self._infer_reply_objective(user_message)
        prefix = f"{customer_name}您好！" if customer_name else "您好！"
        missing_slots = list(getattr(objective, "missing_slots", []) or [])
        cta = str(strategy.build_stage_cta(objective, user_message, history=None) or "").strip()
        if objective.conversion_stage in {ConversionStage.RESERVATION, ConversionStage.HANDOFF, ConversionStage.HIGH_INTENT}:
            priority_reply = str(
                strategy.build_priority_capture_reply(objective, user_message, reason=reason) or ""
            ).strip()
            if missing_slots and priority_reply:
                lead = "我这边优先跟进。" if objective.conversion_stage == ConversionStage.HANDOFF else "我这边继续安排。"
                return f"{prefix}{lead}{priority_reply}".strip()
            if cta:
                lead = "我这边优先跟进。" if objective.conversion_stage == ConversionStage.HANDOFF else "我这边继续跟进。"
                return f"{prefix}{lead}{cta}".strip()
        safe_reply = str(
            strategy.build_safe_no_answer_reply(f"technical_{reason}", user_message, history=None) or ""
        ).strip()
        return safe_reply or (prefix + "您可以补充更具体的信息，我继续帮您安排。")

    def make_business_fallback_reply(
        self,
        customer_name: str,
        user_message: str,
        reason: str,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Dict[str, object]:
        knowledge_reply, matched_question = self.knowledge_fallback_reply(
            customer_name,
            user_message,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )
        if knowledge_reply:
            logger.info(f"[兜底回复] using_knowledge_fallback reason={reason} matched={str(matched_question or '')[:30]}")
            return {
                "reply": knowledge_reply,
                "intent_level": "C",
                "intent_score": 30,
                "need_human": False,
                "matched_knowledge": matched_question or "知识库兜底",
                "suggested_action": "",
                "fallback_reason": reason,
                "source": "knowledge_fallback",
            }

        logger.info(f"[兜底回复] using_clarification_fallback reason={reason}")
        return {
            "reply": self.build_clarification_fallback_reply(customer_name, user_message),
            "intent_level": "C",
            "intent_score": 25,
            "need_human": False,
            "matched_knowledge": "",
            "suggested_action": "",
            "fallback_reason": reason,
            "source": "generic_fallback",
        }

    def make_technical_fallback_reply(self, customer_name: str, user_message: str, reason: str) -> Dict[str, object]:
        logger.info(f"[兜底回复] using_technical_fallback reason={reason}")
        return {
            "reply": self.build_technical_handoff_reply(customer_name, user_message, reason),
            "intent_level": "B",
            "intent_score": 20,
            "need_human": True,
            "matched_knowledge": "",
            "suggested_action": "",
            "fallback_reason": reason,
            "source": "technical_fallback",
        }

    def make_contextual_fallback_reply(
        self,
        customer_name: str,
        user_message: str,
        reason: str,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Dict[str, object]:
        return self.make_business_fallback_reply(
            customer_name,
            user_message,
            reason,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )

    def get_universal_safe_reply(
        self,
        customer_name: str = "",
        user_message: str = "",
    ) -> str:
        """
        通用安全兜底回复（Phase 4 末端兜底链）。

        这是整个回复系统的最后一道防线：
        - 当所有 RAG/LLM/知识库/资格判定都失败时调用
        - 永远返回非空字符串
        - 不依赖任何外部服务或 LLM
        - 根据消息内容做基础社交意图识别
        """
        text = str(user_message or "").strip().lower()

        # 轻量交互意图：直接返回社交模板
        from src.common.core_utils.confidence import classify_message_category
        category = classify_message_category(text)

        name_prefix = f"{customer_name}，" if customer_name else ""

        if category.value == "greeting_social":
            return f"{name_prefix}您好！很高兴为您服务，请告诉我您想了解什么？"
        if category.value == "farewell_thanks":
            return f"{name_prefix}不客气！还有其他需要帮您的随时说～"
        if category.value == "complaint":
            return f"{name_prefix}非常抱歉给您带来困扰，麻烦您具体说说遇到的问题，我帮您尽快处理。"
        if category.value == "purchase":
            return f"{name_prefix}感谢您的兴趣！关于价格、流程我马上整理一份详细资料发您，方便留个接收方式吗？"
        if category.value == "inquiry":
            return f"{name_prefix}您的问题我已收到，麻烦您稍微说得更具体一些（比如预算、时间、人数等），我好更准确地帮您。"

        # 通用兜底
        return f"{name_prefix}您说的我已收到，方便补充一下具体需求吗？这样我能更准确地帮您。"


@lru_cache(maxsize=1)
def get_fallback_reply_service() -> FallbackReplyService:
    return FallbackReplyService()
