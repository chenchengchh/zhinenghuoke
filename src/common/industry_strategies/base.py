from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src.common.types import ConversionStage, CtaMode


class BaseIndustryStrategy:
    """行业策略基类。"""

    schema_id = "generic.service_sales"
    industry_code = "generic"

    def get_domain_keywords(self) -> set[str]:
        return set()

    def get_domain_intent_patterns(self) -> Dict[str, Any]:
        return {}

    def extract_conversion_signals(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, float]:
        del message, history
        return {}

    def infer_conversion_stage(
        self,
        message: str,
        intent_result: Any,
        signals: Dict[str, float],
        history: Optional[List[Dict[str, Any]]] = None,
        customer_data: Optional[Dict[str, Any]] = None,
    ) -> ConversionStage:
        del message, intent_result, signals, history, customer_data
        return ConversionStage.DISCOVERY

    def get_required_slots_for_stage(self, stage: ConversionStage, intent_result: Any) -> List[str]:
        del stage, intent_result
        return []

    def extract_known_slots(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
        customer_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        del message, history, customer_data
        return {}

    def prioritize_missing_slots(
        self,
        stage: ConversionStage,
        missing_slots: List[str],
        intent_result: Any = None,
    ) -> List[str]:
        del intent_result
        priority_map: Dict[ConversionStage, List[str]] = {
            ConversionStage.DISCOVERY: ["对象", "场景", "需求"],
            ConversionStage.QUALIFICATION: ["对象", "场景", "预算", "数量", "时间"],
            ConversionStage.CONSIDERATION: ["对象", "预算", "时间", "数量", "联系方式"],
            ConversionStage.HIGH_INTENT: ["联系方式", "对象", "预算", "时间", "数量"],
            ConversionStage.RESERVATION: ["时间", "数量", "联系方式", "对象"],
            ConversionStage.HANDOFF: ["联系方式", "对象", "时间"],
        }
        priority_order = priority_map.get(stage, [])
        missing = list(missing_slots or [])
        ranked = sorted(
            missing,
            key=lambda slot: (
                priority_order.index(slot) if slot in priority_order else len(priority_order),
                missing.index(slot),
            ),
        )
        return ranked

    @staticmethod
    def _join_history_text(history: Optional[List[Dict[str, Any]]] = None) -> str:
        return " ".join(
            str((item or {}).get("content") or "")
            for item in (history or [])
            if isinstance(item, dict)
        )

    @classmethod
    def _build_combined_text(
        cls,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        history_text = cls._join_history_text(history)
        return f"{history_text}\n{str(message or '')}".strip()

    @staticmethod
    def _contains_contact_info(text: str, customer_data: Optional[Dict[str, Any]] = None) -> bool:
        normalized = str(text or "")
        if any(token in normalized for token in ("联系方式", "联系电话", "接收资料", "加微", "微信", "电话", "手机")):
            return True
        if re.search(r"(1[3-9]\d{9})", normalized):
            return True
        if customer_data and any(customer_data.get(key) for key in ("sec_uid", "customer_id", "phone", "contact")):
            return True
        return False

    @staticmethod
    def _contains_time_info(text: str) -> bool:
        normalized = str(text or "")
        if any(token in normalized for token in ("今天", "明天", "后天", "本周", "下周", "上午", "下午", "晚上")):
            return True
        return bool(
            re.search(
                r"(\d{1,2}月\d{1,2}[日号]?|\d{1,2}[日号]|本月|下月|月底|月初|周[一二三四五六日天])",
                normalized,
            )
        )

    @staticmethod
    def _contains_quantity_info(text: str) -> bool:
        normalized = str(text or "")
        if any(token in normalized for token in ("一个人", "两个人", "三个人", "几个人", "几位", "一套", "两套", "一份", "两份")):
            return True
        return bool(re.search(r"(\d+)\s*(人|位|份|套|个|间|台)", normalized))

    @staticmethod
    def _contains_budget_info(text: str) -> bool:
        normalized = str(text or "")
        if any(token in normalized for token in ("预算", "价格", "费用", "报价", "多少钱", "收费")):
            return True
        return bool(re.search(r"(\d{2,6})\s*(元|块|w|万)", normalized, flags=re.IGNORECASE))

    @staticmethod
    def _contains_subject_hint(text: str) -> bool:
        normalized = str(text or "")
        subject_hints = (
            "产品", "服务", "方案", "套餐", "系统", "软件", "课程", "项目",
            "线路", "景点", "服务包", "功能", "版本", "档期", "名额",
        )
        return any(token in normalized for token in subject_hints)

    def build_stage_cta(
        self,
        objective: Any,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        del objective, message, history
        return ""

    def build_clarification_reply(
        self,
        objective: Any,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        del history
        missing_slots = list(getattr(objective, "missing_slots", []) or [])
        if missing_slots:
            slot_text = "、".join(missing_slots[:2])
            lead = str(getattr(getattr(objective, "conversion_stage", None), "value", "") or "")
            if lead in {"high_intent", "reservation", "handoff"}:
                return f"方便补充一下{slot_text}吗？这样我可以更快帮您推进下一步。"
            return f"方便补充一下{slot_text}吗？这样我可以给您更准确的建议。"
        if str(message or "").strip():
            return "方便再说具体一点吗？这样我能更准确地继续整理。"
        return "方便补充一下更具体的需求吗？我再继续整理。"

    def build_safe_no_answer_reply(
        self,
        reason: str,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        del message, history
        if str(reason or "").startswith("technical_"):
            return "您可以补充更具体的信息，我这边继续跟进并安排后续处理。"
        return "您可以补充更具体的信息，我再继续整理并给您更明确的建议。"

    def build_priority_capture_reply(
        self,
        objective: Any,
        message: str,
        reason: str = "",
    ) -> str:
        del reason
        clarification = self.build_clarification_reply(objective, message, history=None)
        cta = self.build_stage_cta(objective, message, history=None)
        if clarification and cta and cta not in clarification:
            return f"{clarification} {cta}".strip()
        if clarification:
            return clarification
        return cta or self.build_safe_no_answer_reply("technical_capture", message, history=None)

    def get_reply_persona(self, mode: str) -> str:
        del mode
        return "你是一名专业顾问，回答要准确、自然，并推动下一步有效转化。"

    def get_safe_generation_rules(self, mode: str) -> List[str]:
        del mode
        return [
            "优先准确回答当前问题",
            "如果信息不足，先澄清再推进",
            "不要编造事实",
            "只在时机合适时推进下一步动作",
        ]

    def score_domain_match(self, query: str, item: Any) -> float:
        del query, item
        return 0.0

    def normalize_query(
        self,
        message: str,
        retrieval_query: Optional[str] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
    ) -> Dict[str, Any]:
        del history, session_id
        primary_query = str(retrieval_query or message or "").strip()
        return {
            "primary_query": primary_query,
            "aux_queries": [],
        }

    def is_generic_followup(self, service: Any, message: str) -> bool:
        del service, message
        return False

    def get_recent_anchor(
        self,
        service: Any,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
    ) -> str:
        del service, conversation_history, session_id
        return ""

    def expand_followup_query(
        self,
        service: Any,
        message: str,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
    ) -> str:
        del service, conversation_history, session_id
        return str(message or "").strip()

    def infer_followup_intent(
        self,
        service: Any,
        message: str,
        grounded_routes: List[str],
    ):
        del service, message, grounded_routes
        return None

    def refine_intent_with_grounded_context(
        self,
        service: Any,
        message: str,
        intent_result: Any,
        session_id: str,
    ):
        del service, message, session_id
        return intent_result

    def remember_grounded_context(
        self,
        service: Any,
        *,
        session_id: str,
        retrieval_query: str,
        matched_knowledge: Any,
        reply_analysis: Dict[str, Any],
        retrieved_contexts: List[Dict[str, Any]],
    ) -> None:
        del service, session_id, retrieval_query, matched_knowledge, reply_analysis, retrieved_contexts

    def should_prefer_grounded_plan_direct(
        self,
        service: Any,
        message: str,
        answer_plan: Dict[str, Any],
    ) -> bool:
        del service, message, answer_plan
        return False

    def render_grounded_plan_direct_reply(
        self,
        service: Any,
        *,
        mode: str,
        message: str,
        answer_plan: Dict[str, Any],
        route_facts: Dict[str, Any],
    ) -> str:
        del service, mode, message, answer_plan, route_facts
        return ""

    def should_prefer_single_route_grounded_direct(
        self,
        service: Any,
        message: str,
        answer_plan: Dict[str, Any],
        route_facts: Dict[str, Any],
        route_evidence: Dict[str, Any],
        matched_knowledge: Any,
    ) -> bool:
        del service, message, answer_plan, route_facts, route_evidence, matched_knowledge
        return False

    def render_single_route_grounded_reply(
        self,
        service: Any,
        *,
        mode: str,
        message: str,
        answer_plan: Dict[str, Any],
        route_facts: Dict[str, Any],
        route_evidence: Dict[str, Any],
        matched_knowledge: Any,
    ) -> str:
        del service, mode, message, answer_plan, route_facts, route_evidence, matched_knowledge
        return ""


class GenericIndustryStrategy(BaseIndustryStrategy):
    """默认通用行业策略。"""

    CONTACT_HINTS = ("联系", "电话", "手机号", "微信", "发我", "发资料", "怎么找你", "联系方式")
    RESERVATION_HINTS = ("预约", "预留", "锁定", "先预留", "安排", "档期", "库存", "名额")
    ORDER_HINTS = ("下单", "付款", "支付", "签约", "定金", "购买", "成交", "订")
    COMPARISON_HINTS = ("哪个好", "区别", "对比", "怎么选")
    OBJECTION_HINTS = ("贵", "再看看", "考虑下", "不放心", "靠谱吗", "值不值")

    def extract_conversion_signals(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, float]:
        del history
        text = str(message or "")
        return {
            "buying_signal": 0.9 if any(k in text for k in self.ORDER_HINTS) else 0.0,
            "reservation_signal": 0.9 if any(k in text for k in self.RESERVATION_HINTS) else 0.0,
            "contact_acceptance_signal": 0.8 if any(k in text for k in self.CONTACT_HINTS) else 0.0,
            "comparison_signal": 0.7 if any(k in text for k in self.COMPARISON_HINTS) else 0.0,
            "objection_signal": 0.7 if any(k in text for k in self.OBJECTION_HINTS) else 0.0,
            "urgency_signal": 0.7 if any(k in text for k in ("今天", "马上", "尽快", "现在", "急")) else 0.0,
        }

    def infer_conversion_stage(
        self,
        message: str,
        intent_result: Any,
        signals: Dict[str, float],
        history: Optional[List[Dict[str, Any]]] = None,
        customer_data: Optional[Dict[str, Any]] = None,
    ) -> ConversionStage:
        del message, history, customer_data
        primary_intent = str(getattr(getattr(intent_result, "primaryIntent", None), "value", "") or "")
        if signals.get("buying_signal", 0.0) >= 0.8:
            return ConversionStage.HANDOFF
        if signals.get("reservation_signal", 0.0) >= 0.8:
            return ConversionStage.RESERVATION
        if signals.get("contact_acceptance_signal", 0.0) >= 0.7:
            return ConversionStage.HIGH_INTENT
        if signals.get("comparison_signal", 0.0) >= 0.6 or primary_intent in {
            "price_inquiry", "comparison", "contact_inquiry"
        }:
            return ConversionStage.CONSIDERATION
        return ConversionStage.DISCOVERY

    def get_required_slots_for_stage(self, stage: ConversionStage, intent_result: Any) -> List[str]:
        del intent_result
        mapping = {
            ConversionStage.DISCOVERY: [],
            ConversionStage.QUALIFICATION: ["对象", "场景"],
            ConversionStage.CONSIDERATION: ["对象", "预算"],
            ConversionStage.HIGH_INTENT: ["对象", "联系方式"],
            ConversionStage.RESERVATION: ["时间", "数量", "联系方式"],
            ConversionStage.HANDOFF: ["联系方式"],
        }
        return list(mapping.get(stage, []))

    def extract_known_slots(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
        customer_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        known: Dict[str, str] = {}
        text = str(message or "")
        combined = self._build_combined_text(text, history)
        if self._contains_time_info(combined):
            known["时间"] = "yes"
        if self._contains_contact_info(combined, customer_data):
            known["联系方式"] = "yes"
        if self._contains_quantity_info(combined):
            known["数量"] = "yes"
        if self._contains_budget_info(combined):
            known["预算"] = "yes"
        if self._contains_subject_hint(combined):
            known["对象"] = "yes"
        if any(token in combined for token in ("场景", "用途", "目标", "需求", "使用", "想要", "计划")) or len(text) >= 10:
            known["场景"] = "yes"
        if len(text) >= 6 or self._contains_subject_hint(combined):
            known["需求"] = "yes"
        return known

    def build_stage_cta(
        self,
        objective: Any,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        del message, history
        cta_mode = getattr(objective, "cta_mode", CtaMode.NONE)
        if cta_mode == CtaMode.LEAD_CAPTURE:
            return "方便的话留一个接收资料或后续沟通的联系方式，我帮您继续安排。"
        if cta_mode == CtaMode.RESERVATION_OFFER:
            return "如果您想先预留，我可以继续帮您确认安排，您留个方便接收通知的联系方式。"
        if cta_mode == CtaMode.ORDER_OFFER:
            return "如果您这边确认了，我可以继续帮您推进下一步。"
        if cta_mode == CtaMode.MATERIAL_OFFER:
            return "如果需要，我也可以继续帮您整理更完整的资料或方案。"
        return ""
