from __future__ import annotations

import re
from functools import lru_cache
from typing import Callable, Dict, List, Optional


class SalesFollowupService:
    """通用销售跟进策略服务。"""

    HIGH_INTENT_MARKERS = (
        "报价",
        "价格",
        "多少钱",
        "费用",
        "查余位",
        "余位",
        "预留",
        "报名",
        "预订",
        "下单",
        "购买",
        "套餐",
        "行程发我",
        "报价发我",
        "资料发我",
        "详情发我",
        "集合说明",
        "上车通知",
        "发货",
        "安排",
        "明天出发",
        "后天出发",
        "当天能不能报名",
    )
    KNOWLEDGE_HIGH_INTENT_MARKERS = ("价格", "费用", "报价", "余位", "预留", "报名", "预订", "下单")
    EXISTING_FOLLOWUP_MARKERS = (
        "接收资料的联系方式",
        "接收通知的联系方式",
        "方便接收资料",
        "继续跟进",
        "继续帮您安排",
        "完整资料",
        "详细说明",
        "具体安排",
        "首选和备选",
        "帮您筛一遍",
        "先排掉",
    )
    TOPIC_GUIDANCE_MARKERS = ("推荐", "产品", "方案", "服务", "欢迎", "可以咨询")
    SYNTHETIC_CONTEXT_PREFIXES = ("[多轮上下文]", "对话话题历史:")
    EXPLICIT_CONTACT_MARKERS = (
        "联系方式",
        "怎么联系",
        "联系你",
        "联系您",
        "资料发我",
        "发我资料",
        "报价发我",
        "行程发我",
        "加微信",
        "vx",
        "wx",
        "留电话",
        "留个联系方式",
        "留联系方式",
    )
    FACT_QUERY_BLOCK_MARKERS = (
        "多少钱",
        "价格",
        "费用",
        "报价多少",
        "含什么",
        "包含",
        "不含",
        "行程",
        "路线",
        "景点",
        "怎么安排",
        "集合时间",
        "几点",
        "换乘车",
        "纯玩",
        "返40",
        "返20",
    )

    @classmethod
    def _is_real_user_history_message(cls, item: Dict) -> bool:
        if not isinstance(item, dict):
            return False
        direction = str(item.get("direction") or "").strip().lower()
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if not content:
            return False
        if any(content.startswith(prefix) for prefix in cls.SYNTHETIC_CONTEXT_PREFIXES):
            return False
        if direction:
            return direction == "inbound"
        return role in {"user", "human", "customer"}

    def should_append_contact_guidance(
        self,
        *,
        message: str,
        reply: str,
        conversation_history: Optional[List[Dict]],
        matched_question: str = "",
        looks_like_misclassified_assistant_message: Optional[Callable[[str], bool]] = None,
    ) -> bool:
        if any(marker in str(reply or "") for marker in self.EXISTING_FOLLOWUP_MARKERS):
            return False

        looks_like_misclassified_assistant_message = looks_like_misclassified_assistant_message or (lambda _text: False)
        inbound_context_parts = []
        for item in (conversation_history or [])[-6:]:
            if not self._is_real_user_history_message(item):
                continue
            content = str(item.get("content") or "").strip()
            if not content or looks_like_misclassified_assistant_message(content):
                continue
            inbound_context_parts.append(content)

        recent_text = " ".join(inbound_context_parts[-3:])
        combined_text = f"{recent_text} {message}".strip()
        if not combined_text:
            return False

        if any(marker in combined_text for marker in self.EXPLICIT_CONTACT_MARKERS):
            return True
        if any(marker in combined_text for marker in self.FACT_QUERY_BLOCK_MARKERS):
            return False

        if any(marker in combined_text for marker in self.HIGH_INTENT_MARKERS):
            return True
        if matched_question and any(marker in matched_question for marker in self.KNOWLEDGE_HIGH_INTENT_MARKERS):
            return True
        return False

    def append_contact_guidance(
        self,
        reply: str,
        *,
        message: str,
        conversation_history: Optional[List[Dict]],
        matched_question: str = "",
        customer_name: str = "",
        extract_contact_info: Callable[[str], Optional[str]],
        looks_like_misclassified_assistant_message: Optional[Callable[[str], bool]] = None,
    ) -> str:
        current_contact = extract_contact_info(message)

        user_contact_in_history = None
        if conversation_history:
            for item in reversed(conversation_history[-3:]):
                if item.get("direction") == "inbound":
                    user_contact_in_history = extract_contact_info(str(item.get("content") or ""))
                    if user_contact_in_history:
                        break

        if not user_contact_in_history and customer_name:
            user_contact_in_history = extract_contact_info(customer_name)

        if current_contact:
            confirmation = (
                f"好的，已收到您的联系方式({current_contact})，"
                "我会安排专人尽快联系您，并为您补充更详细的资料和说明。请留意后续通知。"
            )
            contact_free_message = str(message or "").replace(current_contact, " ")
            contact_free_message = re.sub(
                r"(我电话是|电话是|电话|手机号是|手机号|手机|联系方式|联系号码|号码|微信是|微信|vx|wx|邮箱|email|联系我|加我)",
                " ",
                contact_free_message,
                flags=re.IGNORECASE,
            )
            contact_free_message = re.sub(r"[\s,，。；;：:、\-]+", "", contact_free_message)
            has_followup_question = (
                len(contact_free_message) >= 4
                or any(marker in str(message or "") for marker in ("?", "？", "吗", "价格", "费用", "含", "路线", "行程", "余位", "资料", "安排"))
            )
            if not has_followup_question and (len(str(message or "")) < 25 or any(marker in str(reply or "") for marker in self.TOPIC_GUIDANCE_MARKERS)):
                return confirmation
            return f"{str(reply or '').rstrip()}\n\n{confirmation}"

        if user_contact_in_history:
            confirmation_markers = ("收到", "联系您", "安排专人", "稍后", "跟进")
            if not any(marker in str(reply or "") for marker in confirmation_markers):
                confirmation = (
                    f"好的，我之前已记录您的联系方式({user_contact_in_history})，"
                    "正为您安排专人跟进，稍后会继续联系您。"
                )
                return f"{str(reply or '').rstrip()}\n{confirmation}"
            return reply

        if not self.should_append_contact_guidance(
            message=message,
            reply=reply,
            conversation_history=conversation_history,
            matched_question=matched_question,
            looks_like_misclassified_assistant_message=looks_like_misclassified_assistant_message,
        ):
            return reply

        guidance = (
            "如果您需要更完整的资料、详细说明，或者希望我继续帮您跟进后续安排，"
            "方便的话也可以留个接收资料的联系方式，我继续为您处理。"
        )
        return f"{str(reply or '').rstrip()}\n{guidance}"

    def build_context_followup_reply(self, message: str, *, customer_name: str = "") -> str:
        normalized_message = str(message or "").strip()
        if not normalized_message:
            return ""
        prefix = f"{customer_name}，" if customer_name else ""
        return (
            f"{prefix}收到，您这边是{normalized_message.rstrip('。！？!?,，')}。"
            "我可以继续按这个需求帮您看更具体的价格、说明和安排；"
            "如果您还在比较不同方案，我也可以继续帮您对比。"
        )

    def build_no_answer_reply(self, mode: str, *, business_intro: bool = False, contextual_followup: str = "") -> str:
        if business_intro:
            return (
                "您好，我们这边主要处理当前系统已接入的产品或服务咨询。"
                "如果您想了解具体方案、价格、流程、时间安排或适用场景，可以直接告诉我，我按现有资料继续帮您整理。"
            )
        if contextual_followup:
            return contextual_followup
        if mode == "sales":
            return "这个问题我先不给您乱答。您可以告诉我想了解的产品、价格、流程、时间安排或使用场景，我按现有资料继续帮您整理。"
        return "这个问题我先不给您乱答。您可以换成更具体的问题、对象或场景，我再按现有资料准确帮您回答。"


@lru_cache(maxsize=1)
def get_sales_followup_service() -> SalesFollowupService:
    return SalesFollowupService()
