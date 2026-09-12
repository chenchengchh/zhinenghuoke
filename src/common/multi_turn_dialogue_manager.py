"""
多轮对话管理器 (MultiTurnDialogueManager)
提供多轮对话兼容接口，但底层状态统一落到 ContextUnderstandingModule。
"""

import logging
from typing import Any, Dict, List

from .context_understanding import context_understanding_module
from .industry_schema_service import get_industry_schema_service
from .types.dialogue import DialogueStateData, DialogueStateEnum

logger = logging.getLogger(__name__)


def _get_active_slot_questions(enterprise_id: str = "") -> Dict[str, str]:
    try:
        active_schema = get_industry_schema_service().get_active_schema(
            enterprise_id=str(enterprise_id or "").strip()
        ) or {}
        metadata = active_schema.get("metadata") or {}
        slot_questions = metadata.get("slot_questions") or {}
        if isinstance(slot_questions, dict):
            return {
                str(key or "").strip(): str(value or "").strip()
                for key, value in slot_questions.items()
                if str(key or "").strip() and str(value or "").strip()
            }
    except Exception:
        return {}
    return {}


class IntentInheritResolver:
    """
    意图继承解析器

    处理跨轮次的意图传递和继承
    """

    def __init__(self):
        self._intent_relations: Dict[str, List[str]] = {
            "product_inquiry": ["price_inquiry", "purchase_intent"],
            "price_inquiry": ["product_inquiry", "purchase_intent"],
            "purchase_intent": ["price_inquiry", "service_inquiry"],
            "cooperation_intent": ["price_inquiry"],
            "complaint": ["service_inquiry", "cooperation_intent"],
            "service_inquiry": ["product_inquiry", "price_inquiry"]
        }

    def should_inherit(self, current_intent: str, previous_intent: str) -> bool:
        """判断是否应该继承上一轮意图"""
        if current_intent == previous_intent:
            return True

        related_intents = self._intent_relations.get(previous_intent, [])
        return current_intent in related_intents

    def get_inherited_context(
        self,
        previous_intent: str,
        previous_slots: Dict[str, Any]
    ) -> Dict[str, Any]:
        """获取继承的上下文"""
        return {
            "inherited_intent": previous_intent,
            "inherited_slots": previous_slots,
            "inheritance_reason": f"从{previous_intent}意图继承"
        }


class UnifiedStateTrackerProxy:
    """兼容旧 state_tracker 接口，底层直接转发到统一上下文对象。"""

    def __init__(self, context_module):
        self._context_module = context_module

    @property
    def _states(self):
        return self._context_module._states

    def get_or_create_state(self, session_id: str, customer_id: str) -> DialogueStateData:
        return self._context_module.get_or_create_state(session_id, customer_id)

    def update_state(
        self,
        session_id: str,
        customer_id: str,
        intent: str,
        extracted_slots: Dict[str, Any],
        user_message: str,
        bot_response: str = "",
    ) -> DialogueStateData:
        return self._context_module.update_dialogue_state(
            session_id=session_id,
            customer_id=customer_id,
            intent=intent,
            extracted_slots=extracted_slots or {},
            user_message=user_message,
            bot_response=bot_response,
        )

    def _detect_topic_switch(self, state: DialogueStateData, user_message: str) -> bool:
        return self._context_module._detect_topic_switch(state, user_message)

    def get_context_for_rag(self, session_id: str, max_turns: int = 5):
        return self._context_module.get_context_for_rag(session_id, max_turns=max_turns)

    def get_state_summary(self, session_id: str) -> Dict[str, Any]:
        state = self._states.get(session_id)
        if not state:
            return {}
        return {
            "session_id": state.session_id,
            "customer_id": state.customer_id,
            "state": state.state.value,
            "current_intent": state.current_intent,
            "turn_count": state.turn_count,
            "filled_slots": [key for key, value in state.slots.items() if value.status == "filled"],
            "missing_slots": list(state.pending_questions),
            "topic_history": state.topic_stack[-3:],
            "pending_questions": list(state.pending_questions),
        }


class UnifiedContextManagerProxy:
    """兼容旧 context_manager 接口，底层直接转发到统一上下文对象。"""

    def __init__(self, context_module):
        self._context_module = context_module

    def add_message(self, message: Dict[str, Any], session_id: str = None) -> bool:
        return self._context_module.add_message(message, session_id=session_id)

    def get_context(self, session_id: str = None):
        return self._context_module.get_context(session_id)

    def clear_session(self, session_id: str):
        self._context_module.clear_session(session_id)

    def get_session_info(self, session_id: str) -> Dict[str, Any]:
        return self._context_module.get_session_info(session_id)

    def get_summary(self, session_id: str) -> str:
        return self._context_module.get_summary(session_id)

    def get_all_entities(self, session_id: str):
        return self._context_module.get_all_entities(session_id)


class MultiTurnDialogueManager:
    """
    多轮对话管理器

    整合状态追踪和上下文管理，提供完整的多轮对话支持
    """

    def __init__(self, enterprise_id: str = ""):
        self._enterprise_id = str(enterprise_id or "").strip()
        self._context_module = context_understanding_module
        self.state_tracker = UnifiedStateTrackerProxy(self._context_module)
        self.context_manager = UnifiedContextManagerProxy(self._context_module)
        self.intent_resolver = IntentInheritResolver()
        self._slot_prompt_repeat_limit = 2
        self._slot_prompt_turn_gap = 2

    def process_message(
        self,
        session_id: str,
        customer_id: str,
        user_message: str,
        recognized_intent: str,
        extracted_entities: Dict[str, Any],
        enterprise_id: str = "",
    ) -> Dict[str, Any]:
        """
        处理多轮对话

        Args:
            session_id: 会话ID
            customer_id: 客户ID
            user_message: 用户消息
            recognized_intent: 识别的意图
            extracted_entities: 提取的实体

        Returns:
            {
                "response_needed": bool,
                "response": str,
                "slots_filled": List[str],
                "missing_slots": List[str],
                "context_for_rag": str,
                "state": str,
                "topic_switch": bool
            }
        """
        current_state = self.state_tracker.get_or_create_state(session_id, customer_id)

        topic_switch = self.state_tracker._detect_topic_switch(current_state, user_message)

        updated_state = self.state_tracker.update_state(
            session_id=session_id,
            customer_id=customer_id,
            intent=recognized_intent,
            extracted_slots=extracted_entities,
            user_message=user_message
        )

        self.context_manager.add_message(
            {
                "session_id": session_id,
                "role": "user",
                "content": user_message,
                "entities": extracted_entities,
            },
            session_id=session_id,
        )

        missing_slots = updated_state.pending_questions
        next_slot = self._select_slot_to_prompt(updated_state, missing_slots)
        response_needed = bool(next_slot)

        response = ""
        if next_slot:
            response = self._generate_slot_filling_question(
                next_slot,
                enterprise_id=str(enterprise_id or self._enterprise_id or "").strip(),
            )
            self._record_slot_prompt(updated_state, next_slot)

        context_for_rag = self.context_manager.get_context(session_id)
        if not isinstance(context_for_rag, str):
            context_for_rag = "\n".join([
                f"{ctx.get('role', 'user')}：{ctx.get('content', '')}"
                for ctx in context_for_rag[-5:]
            ]) if context_for_rag else ""

        rag_context = self.state_tracker.get_context_for_rag(session_id)
        if rag_context:
            context_for_rag = self._merge_context(context_for_rag, rag_context)

        return {
            "response_needed": response_needed,
            "response": response,
            "slots_filled": list(extracted_entities.keys()),
            "missing_slots": missing_slots,
            "context_for_rag": context_for_rag,
            "state": updated_state.state.value,
            "topic_switch": topic_switch,
            "need_human": updated_state.state == DialogueStateEnum.HUMAN_TRANSFER
        }

    def _select_slot_to_prompt(self, state: DialogueStateData, missing_slots: List[str]) -> str:
        if not missing_slots:
            return ""
        prompt_state = state.context_variables.get("slot_prompt_state", {})
        if not isinstance(prompt_state, dict):
            prompt_state = {}

        for slot_name in missing_slots:
            slot_meta = prompt_state.get(slot_name, {})
            prompt_count = int(slot_meta.get("count", 0) or 0)
            last_turn = int(slot_meta.get("last_turn", -999) or -999)
            if prompt_count >= self._slot_prompt_repeat_limit:
                continue
            if state.turn_count - last_turn < self._slot_prompt_turn_gap:
                continue
            return slot_name
        return ""

    def _record_slot_prompt(self, state: DialogueStateData, slot_name: str) -> None:
        prompt_state = state.context_variables.get("slot_prompt_state", {})
        if not isinstance(prompt_state, dict):
            prompt_state = {}
        slot_meta = prompt_state.get(slot_name, {})
        prompt_state[slot_name] = {
            "count": int(slot_meta.get("count", 0) or 0) + 1,
            "last_turn": int(state.turn_count),
        }
        state.context_variables["slot_prompt_state"] = prompt_state

    def add_bot_response(
        self,
        session_id: str,
        response: str,
        matched_knowledge: str = None,
        is_critical: bool = False
    ):
        """添加机器人回复到上下文"""
        self.context_manager.add_message(
            {
                "session_id": session_id,
                "role": "assistant",
                "content": response,
                "metadata": {
                    "matched_knowledge": matched_knowledge,
                    "is_critical": is_critical,
                }
                if matched_knowledge or is_critical
                else {},
            },
            session_id=session_id,
        )

    def _generate_slot_filling_question(self, slot_name: str, enterprise_id: str = "") -> str:
        """生成槽位填充问题"""
        slot_questions = {
            "product_name": "您想了解的是哪个产品呢？",
            "quantity": "请问需要多少数量？",
            "budget_range": "您的预算范围是多少？",
            "contact_method": "如果您需要我继续跟进，方便留一个接收资料的联系方式吗？",
            "timeline": "您希望什么时间完成？",
            "company_name": "请问您公司名称是？",
            "cooperation_type": "您想了解哪种合作方式？",
            "scale": "您的业务规模大概是怎样的？",
            "issue_type": "您遇到的是什么问题？",
            "order_id": "您的订单号是多少？",
            "evidence": "您方便提供相关截图或凭证吗？",
            "service_type": "您需要哪类服务呢？",
            "service_time": "您希望什么时间开始服务？",
            "location": "在哪个地区呢？",
            "product_feature": "您想了解产品的哪些功能或特点呢？",
            "use_case": "您打算应用在什么场景呢？"
        }
        slot_questions.update(_get_active_slot_questions(enterprise_id=enterprise_id or self._enterprise_id))
        return slot_questions.get(slot_name, f"能否详细说明{slot_name}？")

    def _merge_context(
        self,
        context1: str,
        context2: List[Dict]
    ) -> str:
        """合并两段上下文"""
        if not context2:
            return context1

        additional_parts = []
        for ctx in context2:
            if ctx.get("role") == "system":
                additional_parts.append(ctx["content"])

        if additional_parts:
            return context1 + "\n\n" + "\n".join(additional_parts)
        return context1

    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        """获取会话摘要"""
        state_summary = self.state_tracker.get_state_summary(session_id)
        session_info = self.context_manager.get_session_info(session_id)

        return {
            **state_summary,
            **session_info,
            "summary": self.context_manager.get_summary(session_id),
            "recent_entities": self.context_manager.get_all_entities(session_id)
        }

    def clear_session(self, session_id: str):
        """清除会话数据"""
        self.context_manager.clear_session(session_id)

    def get_state(self, session_id: str, customer_id: str):
        """获取当前状态"""
        return self.state_tracker.get_or_create_state(session_id, customer_id)
