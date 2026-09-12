"""
上下文理解器 (ContextUnderstandingModule)
支持对话状态追踪、实体链指、指代消解、上下文窗口。
"""
import json
import logging
import re
import time as _time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .types.dialogue import (
    ContextWindow,
    DialogueStateData,
    DialogueStateEnum,
    Entity,
    EntityType,
    Slot,
)
from .industry_schema_service import get_industry_schema_service

logger = logging.getLogger(__name__)


class ContextUnderstandingModule:
    """
    上下文理解模块。

    这里是统一的上下文状态源：
    - 消息窗口
    - 实体记忆
    - DST/槽位/话题状态
    """

    PRONOUN_PATTERNS = {
        "他": {"type": "third_party", "resolution": None},
        "她": {"type": "third_party", "resolution": None},
        "它": {"type": "object", "resolution": None},
        "这个": {"type": "demonstrative", "resolution": None},
        "那个": {"type": "demonstrative", "resolution": None},
        "这些": {"type": "demonstrative", "resolution": None},
        "那些": {"type": "demonstrative", "resolution": None},
        "此": {"type": "demonstrative", "resolution": None},
        "其": {"type": "third_party", "resolution": None},
    }

    DEMONSTRATIVE_KEYWORDS = {
        "这个", "那个", "这些", "那些",
        "此", "其", "该", "前述", "上述"
    }

    GENERIC_ENTITY_KEYWORDS = {
        EntityType.PRODUCT: ["产品", "服务", "方案", "课程", "系统", "项目"],
        EntityType.PRICE: ["价格", "多少钱", "费用", "收费", "报价", "预算"],
        EntityType.SERVICE: ["服务", "流程", "预约", "下单", "开通", "交付", "支持"],
        EntityType.COMPANY: ["公司", "客服", "门店", "团队"],
        EntityType.BRAND: ["品牌", "牌子"],
        EntityType.FEATURE: ["包含", "不含", "特点", "功能", "配置", "说明"],
        EntityType.TIME: ["时间", "时候", "日期", "几号", "当天", "明天", "周末", "节假日"],
    }

    DOMAIN_ENTITY_KEYWORDS: Dict[EntityType, List[str]] = {}

    DOMAIN_ANCHOR_TERMS: List[str] = []

    GENERIC_TOPIC_KEYWORDS = {
        "联系方式咨询": ["联系", "联系方式", "客服", "怎么联系", "电话", "微信", "邮箱"],
        "价格咨询": ["价格", "多少钱", "费用", "报价", "预算", "优惠"],
        "产品咨询": ["产品", "服务", "方案", "课程", "系统", "项目", "介绍", "详情", "适合"],
        "购买咨询": ["下单", "购买", "开通", "签约", "预约", "订购"],
        "售后服务": ["退改", "改签", "售后", "退款", "支持", "处理"],
        "合作加盟": ["合作", "渠道", "代理"],
        "投诉反馈": ["投诉", "不满", "差评", "退款"],
    }

    DOMAIN_TOPIC_KEYWORDS: Dict[str, List[str]] = {}

    def __init__(
        self,
        window_size: int = 5,
        max_context_tokens: int = 4000,
        max_context_messages: int = 20,
        state_ttl_minutes: int = 30,
    ):
        self.window_size = window_size
        self.max_context_tokens = max_context_tokens
        self.max_context_messages = max_context_messages
        self.state_ttl_minutes = state_ttl_minutes
        self.context_windows: Dict[str, ContextWindow] = {}
        # 兼容旧接口：state_tracker._states 直接映射到统一窗口内的 state 对象。
        self._states: Dict[str, DialogueStateData] = {}
        self._slot_definitions: Dict[str, List[str]] = self._load_slot_definitions()
        logger.info(
            "ContextUnderstandingModule initialized: "
            f"window_size={window_size}, max_context_messages={max_context_messages}"
        )

    def _load_slot_definitions(self) -> Dict[str, List[str]]:
        return {
            "product_inquiry": [],
            "price_inquiry": [],
            "purchase_intent": ["contact_method"],
            "cooperation_intent": ["company_name"],
            "complaint": ["issue_type"],
            "service_inquiry": [],
            "consultation": [],
            "greeting": [],
            "farewell": [],
            "thanks": [],
            "confirmation": [],
            "rejection": [],
            "unknown": [],
            "contact_inquiry": ["contact_method"],
        }

    @staticmethod
    def _get_active_schema(
        enterprise_id: str = "",
        preferred_schema_id: str = "",
    ) -> Dict[str, Any]:
        try:
            return get_industry_schema_service().get_active_schema(
                enterprise_id=str(enterprise_id or "").strip(),
                preferred_schema_id=str(preferred_schema_id or "").strip(),
            ) or {}
        except Exception:
            return {}

    @classmethod
    def _has_domain_signals(
        cls,
        text: str,
        *,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
    ) -> bool:
        normalized = str(text or "")
        active_schema = cls._get_active_schema(
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
        )
        if bool(active_schema.get("is_domain_specific", False)):
            metadata = active_schema.get("metadata") or {}
            ir_config = metadata.get("intent_recognition") or {}
            domain_terms = ir_config.get("domain_context_terms") or []
            if any(term in normalized for term in domain_terms):
                return True
            domain_topics = ir_config.get("domain_topic_keywords") or {}
            for keywords in domain_topics.values():
                if any(kw in normalized for kw in keywords):
                    return True
        return False

    @classmethod
    def _get_entity_keywords(
        cls,
        message: str = "",
        *,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
    ) -> Dict[EntityType, List[str]]:
        keywords = {
            entity_type: list(values)
            for entity_type, values in cls.GENERIC_ENTITY_KEYWORDS.items()
        }

        active_schema = cls._get_active_schema(
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
        )
        entity_type = str(active_schema.get("entity_type") or "").strip()
        metadata = active_schema.get("metadata") or {}
        category_profiles = metadata.get("category_profiles") or {}

        profile_keyword_set = {
            str(keyword or "").strip()
            for profile in category_profiles.values()
            if isinstance(profile, dict)
            for keyword in (profile.get("keywords") or [])
            if str(keyword or "").strip()
        }
        if profile_keyword_set:
            keywords[EntityType.PRODUCT] = list(dict.fromkeys(keywords.get(EntityType.PRODUCT, []) + sorted(profile_keyword_set)))

        if cls._has_domain_signals(
            message,
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
        ):
            active_schema_2 = cls._get_active_schema(
                enterprise_id=enterprise_id,
                preferred_schema_id=preferred_schema_id,
            )
            metadata_2 = active_schema_2.get("metadata") or {}
            ir_config_2 = metadata_2.get("intent_recognition") or {}
            domain_entity_keywords = ir_config_2.get("domain_entity_keywords") or {}
            for entity_type_key, entity_keywords in domain_entity_keywords.items():
                try:
                    et = EntityType(str(entity_type_key).strip().lower())
                    merged = keywords.get(et, []) + list(entity_keywords)
                    keywords[et] = list(dict.fromkeys(merged))
                except ValueError:
                    pass
            for entity_type, entity_keywords in cls.DOMAIN_ENTITY_KEYWORDS.items():
                merged = keywords.get(entity_type, []) + list(entity_keywords)
                keywords[entity_type] = list(dict.fromkeys(merged))

        return keywords

    @classmethod
    def _get_domain_terms(
        cls,
        text: str = "",
        *,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
    ) -> List[str]:
        active_schema = cls._get_active_schema(
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
        )
        grounded_config = active_schema.get("grounded_config") or {}
        alias_groups = grounded_config.get("entity_alias_groups") or {}
        active_entity_type = str(active_schema.get("entity_type") or "").strip()
        domain_terms = list(cls.DOMAIN_ANCHOR_TERMS) if (
            active_entity_type
            and cls._has_domain_signals(
                text,
                enterprise_id=enterprise_id,
                preferred_schema_id=preferred_schema_id,
            )
        ) else []
        for route_name, aliases in alias_groups.items():
            normalized = str(route_name or "").strip()
            if normalized:
                domain_terms.append(normalized)
            for alias in aliases or []:
                normalized_alias = str(alias or "").strip()
                if normalized_alias:
                    domain_terms.append(normalized_alias)
        return list(dict.fromkeys(domain_terms))

    @classmethod
    def _get_topic_keywords(
        cls,
        message: str,
        *,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
    ) -> Dict[str, List[str]]:
        keywords = {topic: list(values) for topic, values in cls.GENERIC_TOPIC_KEYWORDS.items()}
        active_schema = cls._get_active_schema(
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
        )
        if str(active_schema.get("entity_type") or "").strip() or cls._has_domain_signals(
            message,
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
        ):
            metadata = active_schema.get("metadata") or {}
            ir_config = metadata.get("intent_recognition") or {}
            for topic, values in cls.DOMAIN_TOPIC_KEYWORDS.items():
                keywords[topic] = list(values)
            for topic, values in (ir_config.get("domain_topic_keywords") or {}).items():
                normalized_topic = str(topic or "").strip()
                normalized_values = [str(value or "").strip() for value in list(values or []) if str(value or "").strip()]
                if normalized_topic and normalized_values:
                    keywords[normalized_topic] = normalized_values
        return keywords

    def _estimate_tokens(self, message: Dict[str, Any]) -> int:
        content = message.get("content", "")
        if isinstance(content, str):
            return len(content) // 4 + 100
        return 50

    def _current_token_count(self, window: ContextWindow) -> int:
        return sum(self._estimate_tokens(message) for message in window.messages)

    def _prune_messages(self, window: ContextWindow):
        while len(window.messages) > self.max_context_messages:
            window.messages.pop(0)

        while (
            len(window.messages) > 1
            and self._current_token_count(window) > self.max_context_tokens
        ):
            window.messages.pop(0)

    def _is_expired(self, window: ContextWindow) -> bool:
        elapsed = datetime.now() - window.state.updated_at
        return elapsed.total_seconds() > self.state_ttl_minutes * 60

    def _carry_forward_recent_messages(self, window: ContextWindow, max_messages: int = 4) -> List[Dict[str, Any]]:
        """TTL 过期后保留最近关键轮次，避免短追问直接失忆。"""
        if not window.messages:
            return []
        recent_messages = [
            dict(message)
            for message in window.messages[-max_messages:]
            if isinstance(message, dict) and str(message.get("content") or "").strip()
        ]
        return recent_messages

    def _carry_forward_recent_entities(self, window: ContextWindow) -> Dict[str, Entity]:
        """TTL 过期后保留最近知识锚点与最近提到的实体。"""
        retained: Dict[str, Entity] = {}
        candidate_names: List[str] = []
        grounded = window.state.context_variables.get("grounded_knowledge", {})
        if isinstance(grounded, dict):
            recommended = str(grounded.get("recommended_route") or "").strip()
            if recommended:
                candidate_names.append(recommended)
            for route_name in grounded.get("routes") or []:
                route_name = str(route_name or "").strip()
                if route_name:
                    candidate_names.append(route_name)
        last_entity = str(window.state.last_entity_mentioned or "").strip()
        if last_entity:
            candidate_names.append(last_entity)

        for entity_name in dict.fromkeys(candidate_names):
            entity = window.entities.get(entity_name)
            if entity:
                retained[entity_name] = entity
        return retained

    def get_or_create_window(self, customer_id: str, customer_name: str = "") -> ContextWindow:
        """获取或创建统一上下文窗口。"""
        if customer_id in self.context_windows:
            window = self.context_windows[customer_id]
            if self._is_expired(window):
                retained_messages = self._carry_forward_recent_messages(window)
                retained_entities = self._carry_forward_recent_entities(window)
                retained_topics = window.state.topic_stack[-3:]
                retained_intents = window.state.intent_history[-5:]
                retained_context_vars = {}
                grounded = window.state.context_variables.get("grounded_knowledge", {})
                if isinstance(grounded, dict) and grounded:
                    retained_context_vars["grounded_knowledge"] = dict(grounded)
                window = ContextWindow(
                    messages=retained_messages,
                    entities=retained_entities,
                    state=DialogueStateData(
                        session_id=customer_id,
                        customer_id=customer_name or window.state.customer_id,
                        topic_stack=list(retained_topics),
                        intent_history=list(retained_intents),
                        current_topic=str(window.state.current_topic or ""),
                        current_intent=str(window.state.current_intent or ""),
                        last_entity_mentioned=window.state.last_entity_mentioned,
                        context_variables=retained_context_vars,
                    ),
                    window_size=self.window_size,
                )
                self.context_windows[customer_id] = window
        else:
            window = ContextWindow(
                messages=[],
                entities={},
                state=DialogueStateData(
                    session_id=customer_id,
                    customer_id=customer_name,
                ),
                window_size=self.window_size,
            )
            self.context_windows[customer_id] = window

        window.last_accessed = _time.time()
        if customer_name and not window.state.customer_id:
            window.state.customer_id = customer_name
        if not window.state.session_id:
            window.state.session_id = customer_id
        self._states[customer_id] = window.state
        return window

    def get_or_create_state(self, session_id: str, customer_id: str = "") -> DialogueStateData:
        return self.get_or_create_window(session_id, customer_id).state

    def update(
        self,
        customer_id: str,
        user_message: str,
        bot_response: str = "",
        intent: Dict[str, Any] = None,
        entities: List[Dict[str, Any]] = None,
    ) -> ContextWindow:
        """
        兼容旧接口：按同一个底层对象更新上下文。
        """
        window = self.register_user_turn(
            session_id=customer_id,
            customer_id=customer_id,
            user_message=user_message,
            intent=intent,
            entities=entities,
        )
        if bot_response:
            self.register_assistant_turn(customer_id, bot_response)
        return window

    def _coerce_intent_dict(
        self,
        intent: Any = None,
        extracted_entities: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if isinstance(intent, dict):
            return dict(intent)
        if isinstance(intent, str):
            return {
                "type": intent,
                "confidence": 1.0,
                "keywords": list((extracted_entities or {}).keys()),
                "slots": extracted_entities or {},
            }
        return None

    def _coerce_entities(self, entities: Any) -> List[Dict[str, Any]]:
        if not entities:
            return []
        if isinstance(entities, list):
            return [item for item in entities if isinstance(item, dict)]
        if isinstance(entities, dict):
            normalized: List[Dict[str, Any]] = []
            for key, value in entities.items():
                if isinstance(value, list):
                    for item in value:
                        normalized.append({"name": str(item), "type": str(key)})
                else:
                    normalized.append({"name": str(value), "type": str(key)})
            return normalized
        return []

    def _append_message(self, window: ContextWindow, message: Dict[str, Any]):
        window.messages.append(message)
        self._prune_messages(window)
        window.last_accessed = _time.time()
        window.state.updated_at = datetime.now()

    def register_user_turn(
        self,
        session_id: str,
        customer_id: str,
        user_message: str,
        intent: Any = None,
        entities: Any = None,
        *,
        update_state: bool = True,
    ) -> ContextWindow:
        window = self.get_or_create_window(session_id, customer_id)
        timestamp = datetime.now()
        intent_dict = self._coerce_intent_dict(intent)
        entity_dicts = self._coerce_entities(entities)

        self._append_message(
            window,
            {
                "session_id": session_id,
                "role": "user",
                "direction": "inbound",
                "content": user_message,
                "timestamp": timestamp.isoformat(),
                "created_at": timestamp.isoformat(),
                "intent": intent_dict,
                "entities": entities,
            },
        )

        for entity_dict in entity_dicts:
            self._update_entity(window, entity_dict, timestamp)

        if update_state and intent_dict:
            self._update_state(window, intent_dict, user_message)

        return window

    def register_assistant_turn(
        self,
        session_id: str,
        response: str,
        matched_knowledge: Optional[str] = None,
        is_critical: bool = False,
    ) -> ContextWindow:
        window = self.get_or_create_window(session_id)
        timestamp = datetime.now()
        metadata: Dict[str, Any] = {}
        if matched_knowledge:
            metadata["matched_knowledge"] = matched_knowledge
        if is_critical:
            metadata["is_critical"] = True
        self._append_message(
            window,
            {
                "session_id": session_id,
                "role": "assistant",
                "direction": "outbound",
                "content": response,
                "timestamp": timestamp.isoformat(),
                "created_at": timestamp.isoformat(),
                "metadata": metadata,
            },
        )
        return window

    def remember_grounded_knowledge(
        self,
        session_id: str,
        *,
        routes: Optional[List[str]] = None,
        recommended_route: str = "",
        matched_knowledge: str = "",
        retrieval_query: str = "",
        comparison_summary: str = "",
        route_facts: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """把最近一次知识结论写回共享上下文，供后续短追问与弱意图纠偏使用。"""
        window = self.get_or_create_window(session_id)
        timestamp = datetime.now()

        normalized_routes = self._normalize_grounded_routes(
            routes=routes or [],
            recommended_route=recommended_route,
            matched_knowledge=matched_knowledge,
            retrieval_query=retrieval_query,
        )

        grounded_state = {
            "routes": normalized_routes,
            "recommended_route": (recommended_route or "").strip(),
            "matched_knowledge": (matched_knowledge or "").strip(),
            "retrieval_query": (retrieval_query or "").strip(),
            "comparison_summary": (comparison_summary or "").strip(),
            "route_facts": dict(route_facts or {}),
            "updated_at": timestamp.isoformat(),
        }
        window.state.context_variables["grounded_knowledge"] = grounded_state

        for route_name in normalized_routes:
            self._update_entity(
                window,
                {"name": route_name, "type": EntityType.PRODUCT.value},
                timestamp,
            )

        if grounded_state["recommended_route"]:
            window.state.last_entity_mentioned = grounded_state["recommended_route"]
            grounded_topic = self._infer_topic(grounded_state["recommended_route"], {})
            if grounded_topic:
                window.state.current_topic = grounded_topic
                if not window.state.topic_stack or window.state.topic_stack[-1] != grounded_topic:
                    window.state.topic_stack.append(grounded_topic)
                    if len(window.state.topic_stack) > 10:
                        window.state.topic_stack.pop(0)

        window.state.updated_at = timestamp
        window.last_accessed = _time.time()
        return grounded_state

    def _normalize_grounded_routes(
        self,
        *,
        routes: List[str],
        recommended_route: str = "",
        matched_knowledge: str = "",
        retrieval_query: str = "",
    ) -> List[str]:
        ordered_routes: List[str] = []
        seen = set()

        def _append_route(route_name: str):
            route_name = str(route_name or "").strip()
            if not route_name or route_name in seen:
                return
            seen.add(route_name)
            ordered_routes.append(route_name)

        for route_name in routes or []:
            _append_route(route_name)
        _append_route(recommended_route)

        searchable_text = " ".join(
            text for text in [matched_knowledge, retrieval_query] if isinstance(text, str) and text.strip()
        )
        for route_name in self._get_domain_terms(searchable_text):
            if route_name in searchable_text:
                _append_route(route_name)

        return ordered_routes

    def get_grounded_knowledge(self, session_id: str) -> Dict[str, Any]:
        window = self.get_or_create_window(session_id)
        grounded = window.state.context_variables.get("grounded_knowledge", {})
        return dict(grounded) if isinstance(grounded, dict) else {}

    def get_recent_grounded_anchor(self, session_id: str) -> str:
        grounded = self.get_grounded_knowledge(session_id)
        recommended = str(grounded.get("recommended_route") or "").strip()
        if recommended:
            return recommended

        routes = grounded.get("routes") or []
        if isinstance(routes, list):
            for route_name in routes:
                route_name = str(route_name or "").strip()
                if route_name:
                    return route_name
        return ""

    def add_message(
        self,
        message: Dict[str, Any],
        session_id: str = None,
        customer_id: str = "",
    ) -> bool:
        """兼容旧 ContextWindowManager.add_message 接口。"""
        if not isinstance(message, dict):
            return False
        effective_session_id = session_id or str(message.get("session_id") or "")
        if not effective_session_id:
            return False

        role = str(message.get("role") or "user").lower()
        content = str(message.get("content") or "")
        if not content.strip():
            return False

        if role == "user":
            self.register_user_turn(
                session_id=effective_session_id,
                customer_id=customer_id or effective_session_id,
                user_message=content,
                intent=message.get("intent"),
                entities=message.get("entities"),
            )
            return True

        self.register_assistant_turn(
            session_id=effective_session_id,
            response=content,
            matched_knowledge=(message.get("metadata") or {}).get("matched_knowledge"),
            is_critical=bool((message.get("metadata") or {}).get("is_critical")),
        )
        return True

    def _update_entity(self, window: ContextWindow, entity_dict: Dict, timestamp: datetime):
        """更新实体"""
        name = entity_dict.get("name", "")
        entity_type_str = entity_dict.get("type", "unknown")

        try:
            entity_type = EntityType(entity_type_str)
        except ValueError:
            entity_type = EntityType.UNKNOWN

        if name in window.entities:
            entity = window.entities[name]
            entity.mentions += 1
            entity.last_mentioned = timestamp
        else:
            entity = Entity(
                name=name,
                type=entity_type,
                aliases=entity_dict.get("aliases", []),
                first_mentioned=timestamp,
                last_mentioned=timestamp
            )
            window.entities[name] = entity

    def _fill_slots(self, state: DialogueStateData, extracted_slots: Dict[str, Any]):
        required_slots = self._slot_definitions.get(state.current_intent, [])
        for slot_name, slot_value in extracted_slots.items():
            if slot_name not in required_slots:
                continue
            slot = state.slots.get(slot_name)
            if slot:
                slot.value = slot_value
                slot.status = "filled"
                slot.confirmed_at = datetime.now()
            else:
                state.slots[slot_name] = Slot(
                    name=slot_name,
                    value=slot_value,
                    status="filled",
                    confirmed_at=datetime.now(),
                    source="user_input",
                )

    def _get_missing_slots(self, state: DialogueStateData) -> List[str]:
        required_slots = self._slot_definitions.get(state.current_intent, [])
        missing = []
        for slot_name in required_slots:
            if slot_name not in state.slots or state.slots[slot_name].status == "empty":
                missing.append(slot_name)
        return missing

    def _detect_topic_switch(self, state: DialogueStateData, user_message: str) -> bool:
        detected_topic = self._infer_topic(user_message, {})
        if not detected_topic:
            return False
        current_topic = state.current_topic or (state.topic_stack[-1] if state.topic_stack else "")
        return bool(current_topic and current_topic != detected_topic)

    def _handle_topic_switch(self, state: DialogueStateData, user_message: str):
        detected_topic = self._infer_topic(user_message, {})
        if not detected_topic:
            return
        state.topic_stack.append(detected_topic)
        if len(state.topic_stack) > 10:
            state.topic_stack.pop(0)
        if state.current_intent and state.slots:
            state.context_variables[f"pending_{state.current_intent}"] = {
                key: value.value for key, value in state.slots.items()
            }
        state.current_topic = detected_topic
        state.slots.clear()
        state.pending_questions.clear()
        state.state = DialogueStateEnum.TOPIC_SWITCH

    def _transition_state(self, state: DialogueStateData, bot_response: str = ""):
        if state.pending_questions:
            state.state = DialogueStateEnum.SLOT_FILLING
        elif bot_response:
            state.state = DialogueStateEnum.COMPLETED
        elif state.current_intent:
            state.state = DialogueStateEnum.ANSWERING

    def update_dialogue_state(
        self,
        session_id: str,
        customer_id: str,
        intent: str,
        extracted_slots: Dict[str, Any],
        user_message: str,
        bot_response: str = "",
    ) -> DialogueStateData:
        window = self.get_or_create_window(session_id, customer_id)
        state = window.state

        state.turn_count += 1
        if intent != state.current_intent:
            if state.current_intent:
                state.intent_history.append(state.current_intent)
                if len(state.intent_history) > 5:
                    state.intent_history.pop(0)
            state.current_intent = intent
            state.state = DialogueStateEnum.INTENT_RECOGNIZED

        self._fill_slots(state, extracted_slots or {})
        missing_slots = self._get_missing_slots(state)
        state.pending_questions = missing_slots

        if self._detect_topic_switch(state, user_message):
            self._handle_topic_switch(state, user_message)

        inferred_topic = self._infer_topic(user_message, {"type": intent})
        if inferred_topic:
            if state.current_topic and state.current_topic != inferred_topic:
                state.topic_history.append(state.current_topic)
            state.current_topic = inferred_topic
            if not state.topic_stack or state.topic_stack[-1] != inferred_topic:
                state.topic_stack.append(inferred_topic)
                if len(state.topic_stack) > 10:
                    state.topic_stack.pop(0)

        self._transition_state(state, bot_response=bot_response)
        state.updated_at = datetime.now()
        window.last_accessed = _time.time()
        return state

    def _update_state(self, window: ContextWindow, intent: Dict[str, Any], message: str):
        """兼容旧接口的状态更新。"""
        intent_type = str(intent.get("type", "") or "")
        if not intent_type:
            return
        intent_confidence = float(intent.get("confidence", 0.0) or 0.0)

        if intent_confidence >= 0.6 and window.state.current_intent != intent_type:
            if window.state.current_intent:
                window.state.intent_history.append(window.state.current_intent)
            window.state.current_intent = intent_type
            window.state.state = DialogueStateEnum.INTENT_RECOGNIZED

        keywords = intent.get("keywords", [])
        if keywords:
            window.state.last_entity_mentioned = str(keywords[0])

        self._fill_slots(window.state, intent.get("slots") or {})
        window.state.pending_questions = self._get_missing_slots(window.state)

        topic = self._infer_topic(message, intent)
        if topic:
            if window.state.current_topic and window.state.current_topic != topic:
                window.state.topic_history.append(window.state.current_topic)
            window.state.current_topic = topic
            if not window.state.topic_stack or window.state.topic_stack[-1] != topic:
                window.state.topic_stack.append(topic)
                if len(window.state.topic_stack) > 10:
                    window.state.topic_stack.pop(0)

        self._transition_state(window.state)

    def _infer_topic(
        self,
        message: str,
        intent: Dict,
        *,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
    ) -> str:
        """推断话题"""
        del intent

        message_lower = message.lower()
        tourism_markers = ("一日游", "门票", "午餐", "景点", "行程", "仙女山", "天坑", "地缝")
        if any(marker in message_lower for marker in tourism_markers):
            if any(marker in message_lower for marker in ("包含", "含", "门票", "午餐", "车费", "费用")):
                return "旅游包含项咨询"
            return "旅游套餐咨询"
        for topic, keywords in self._get_topic_keywords(
            message_lower,
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
        ).items():
            if any(kw in message_lower for kw in keywords):
                return topic

        return ""

    def resolve_references(self, message: str, customer_id: str) -> str:
        """
        指代消解

        Args:
            message: 原始消息
            customer_id: 客户ID

        Returns:
            str: 消解后的消息
        """
        window = self.get_or_create_window(customer_id)

        import re
        for pronoun, info in self.PRONOUN_PATTERNS.items():
            if pronoun in message:
                pattern = r'(?<!\w)' + re.escape(pronoun) + r'(?!\w)'
                if re.search(pattern, message):
                    resolved = self._resolve_pronoun(pronoun, info, window, message)
                    if resolved:
                        message = re.sub(pattern, lambda m: resolved, message)

        for demo in self.DEMONSTRATIVE_KEYWORDS:
            if demo in message:
                pattern = r'(?<!\w)' + re.escape(demo) + r'(?!\w)'
                if re.search(pattern, message):
                    resolved = self._resolve_demonstrative(demo, window)
                    if resolved:
                        message = re.sub(pattern, lambda m: resolved, message)

        return message

    def _resolve_pronoun(self, pronoun: str, info: Dict,
                        window: ContextWindow, full_message: str) -> Optional[str]:
        """解析代词，优先匹配最近提及的实体"""
        ptype = info.get("type")

        if pronoun in ["他", "她"]:
            recent_person = self._find_recent_entity(window, EntityType.PERSON)
            if recent_person:
                return recent_person

        if pronoun == "它":
            recent_product = self._find_recent_entity(window, EntityType.PRODUCT)
            if recent_product:
                return recent_product
            recent_any = self._find_recent_entity(window, None)
            if recent_any:
                return recent_any

        return None

    def _resolve_demonstrative(self, demonstrative: str, window: ContextWindow) -> Optional[str]:
        """解析指示代词"""
        if not window.entities:
            return None

        last_entity = None
        max_mentions = 0

        for entity in window.entities.values():
            if entity.mentions >= max_mentions:
                max_mentions = entity.mentions
                last_entity = entity.name

        return last_entity

    def _find_recent_entity(
        self,
        window: ContextWindow,
        entity_type: Optional[EntityType],
    ) -> Optional[str]:
        candidates = [
            entity
            for entity in window.entities.values()
            if entity_type is None or entity.type == entity_type
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda entity: (
                getattr(entity, "last_mentioned", datetime.min),
                entity.mentions,
            ),
            reverse=True,
        )
        return candidates[0].name

    def link_entities(self, message: str, customer_id: str) -> Dict[str, str]:
        """
        实体链指

        Args:
            message: 消息文本
            customer_id: 客户ID

        Returns:
            Dict[str, str]: 实体名 -> 实体类型 的映射
        """
        window = self.get_or_create_window(customer_id)
        linked = {}

        for entity_type, keywords in self._get_entity_keywords(message).items():
            for keyword in keywords:
                if keyword in message:
                    idx = message.index(keyword)
                    start = max(0, idx - 10)
                    end = min(len(message), idx + len(keyword) + 10)
                    context = message[start:end]

                    for name, entity in window.entities.items():
                        if name in context or any(alias in context for alias in entity.aliases):
                            linked[name] = entity.type.value

        return linked

    def get_context(self, session_id: str = None) -> List[Dict[str, Any]]:
        if not session_id:
            return []
        return list(self.get_or_create_window(session_id).messages)

    def get_context_for_rag(self, session_id: str, max_turns: int = 5) -> List[Dict[str, Any]]:
        state = self.get_or_create_state(session_id)
        context: List[Dict[str, Any]] = []

        if state.slots:
            slot_info = {
                key: value.value
                for key, value in state.slots.items()
                if value.status == "filled"
            }
            if slot_info:
                context.append(
                    {
                        "role": "system",
                        "content": f"当前已收集信息: {json.dumps(slot_info, ensure_ascii=False)}",
                    }
                )

        if state.topic_stack:
            context.append(
                {
                    "role": "system",
                    "content": f"对话话题历史: {' -> '.join(state.topic_stack[-3:])}",
                }
            )

        if state.intent_history:
            context.append(
                {
                    "role": "system",
                    "content": f"相关意图: {state.intent_history[-1]}",
                }
            )

        messages = self.get_context(session_id)[-max_turns * 2 :]
        context.extend(messages)
        return context

    def get_context_for_retrieval(self, customer_id: str, max_turns: int = 3) -> str:
        """
        获取用于检索的上下文文本

        Args:
            customer_id: 客户ID
            max_turns: 最大轮次

        Returns:
            str: 格式化的上下文文本
        """
        window = self.get_or_create_window(customer_id)

        if not window.messages:
            return ""

        recent_messages = window.messages[-max_turns * 2:]
        context_parts = []

        for msg in recent_messages:
            role = "用户" if msg["role"] == "user" else "客服"
            content = msg["content"]
            context_parts.append(f"{role}: {content}")

        entities_info = []
        if window.entities:
            for name, entity in list(window.entities.items())[-3:]:
                entities_info.append(f"相关实体: {name}({entity.type.value})")

        context_text = "\n".join(context_parts)
        if entities_info:
            context_text += "\n" + "\n".join(entities_info)

        return context_text

    def get_session_info(self, session_id: str) -> Dict[str, Any]:
        window = self.get_or_create_window(session_id)
        return {
            "session_id": session_id,
            "message_count": len(window.messages),
            "token_count": self._current_token_count(window),
            "is_full": self._current_token_count(window) >= self.max_context_tokens * 0.9,
            "last_accessed": window.last_accessed,
        }

    def get_summary(self, session_id: str, max_messages: int = 6) -> str:
        messages = self.get_context(session_id)[-max_messages:]
        return "\n".join(
            f"{('用户' if item.get('role') == 'user' else '客服')}: {item.get('content', '')}"
            for item in messages
        )

    def get_all_entities(self, session_id: str) -> List[Dict[str, Any]]:
        window = self.get_or_create_window(session_id)
        return [
            {
                "name": entity.name,
                "type": entity.type.value,
                "mentions": entity.mentions,
            }
            for entity in window.entities.values()
        ]

    def hydrate_history(
        self,
        session_id: str,
        customer_id: str,
        message_history: Optional[List[Dict[str, Any]]],
    ) -> ContextWindow:
        """
        将外部会话历史同步到统一上下文对象，供非主回复链共享使用。
        """
        window = self.get_or_create_window(session_id, customer_id)
        if not message_history:
            return window

        normalized_messages: List[Dict[str, Any]] = []
        for item in message_history[-self.max_context_messages :]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            direction = str(item.get("direction") or "").lower().strip()
            role = "assistant" if direction == "outbound" else "user"
            normalized_messages.append(
                {
                    "session_id": session_id,
                    "role": role,
                    "direction": direction or ("outbound" if role == "assistant" else "inbound"),
                    "content": content,
                    "timestamp": item.get("timestamp") or item.get("created_at") or datetime.now().isoformat(),
                    "created_at": item.get("created_at") or item.get("timestamp") or datetime.now().isoformat(),
                }
            )

        if normalized_messages:
            window.messages = normalized_messages
            self._prune_messages(window)
            window.last_accessed = _time.time()
            last_customer_message = next(
                (
                    msg.get("content", "")
                    for msg in reversed(normalized_messages)
                    if msg.get("role") == "user"
                ),
                "",
            )
            if last_customer_message and not window.state.current_topic:
                window.state.current_topic = self._infer_topic(last_customer_message, {})

        return window

    def clear_context(self, customer_id: str):
        self.clear_session(customer_id)

    def clear_session(self, session_id: str):
        """清除统一上下文对象中的会话数据。"""
        if session_id in self.context_windows:
            del self.context_windows[session_id]
        self._states.pop(session_id, None)
        logger.info(f"Context cleared for session: {session_id}")

    def cleanup_expired(self, ttl: float = 1800) -> int:
        """清理过期的上下文窗口
        
        Args:
            ttl: 过期时间（秒），默认30分钟
            
        Returns:
            int: 清理的窗口数量
        """
        import time as _time
        current_time = _time.time()
        expired_keys = [
            k for k, v in self.context_windows.items()
            if current_time - v.last_accessed > ttl
        ]
        for k in expired_keys:
            del self.context_windows[k]
            self._states.pop(k, None)
        if expired_keys:
            logger.info(f"清理了 {len(expired_keys)} 个过期上下文窗口")
        return len(expired_keys)

    def get_context_summary(self, customer_id: str) -> Dict[str, Any]:
        """获取上下文摘要"""
        window = self.get_or_create_window(customer_id)

        return {
            "message_count": len(window.messages),
            "entity_count": len(window.entities),
            "current_topic": window.state.current_topic,
            "current_intent": window.state.current_intent,
            "entities": [
                {
                    "name": e.name,
                    "type": e.type.value,
                    "mentions": e.mentions
                }
                for e in list(window.entities.values())[-5:]
            ],
            "topic_history": window.state.topic_history[-3:],
            "intent_history": window.state.intent_history[-3:]
        }


context_understanding_module = ContextUnderstandingModule()
