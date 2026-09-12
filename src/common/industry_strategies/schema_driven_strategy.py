from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from src.common.industry_schema_service import get_industry_schema_service, get_active_schema_with_compat
from src.common.types import ConversionStage, CtaMode

from .base import BaseIndustryStrategy
from .followup_rule_engine import FollowupRuleEngine


class SchemaDrivenStrategy(BaseIndustryStrategy):
    """
    Schema 驱动的行业策略。

    所有行业数据（关键词、信号词、模板、追问规则）完全从 Schema JSON 读取，
    零硬编码默认值。新增行业只需添加 Schema JSON 文件，无需编写 Python 代码。
    """

    def __init__(self, enterprise_id: str = ""):
        self._enterprise_id = str(enterprise_id or "").strip()
        self._rule_engine = FollowupRuleEngine(enterprise_id=self._enterprise_id)
        self._schema_cache = None
        self._schema_cache_key = None

    def _get_schema(self) -> Dict[str, Any]:
        service = get_industry_schema_service()
        active_schema = get_active_schema_with_compat(service, enterprise_id=self._enterprise_id)
        cache_key = f"{self._enterprise_id}::{active_schema.get('schema_id', '')}"
        if cache_key != self._schema_cache_key:
            self._schema_cache = active_schema
            self._schema_cache_key = cache_key
        return self._schema_cache

    def _get_metadata(self) -> Dict[str, Any]:
        return self._get_schema().get("metadata") or {}

    def _get_followup_config(self) -> Dict[str, Any]:
        return self._get_metadata().get("followup_strategy") or {}

    def _get_intent_config(self) -> Dict[str, Any]:
        return self._get_metadata().get("intent_recognition") or {}

    def _get_reply_config(self) -> Dict[str, Any]:
        return self._get_metadata().get("reply_profile") or {}

    def _get_category_profiles(self) -> Dict[str, Any]:
        return self._get_metadata().get("category_profiles") or {}

    @property
    def schema_id(self) -> str:
        return str(self._get_schema().get("schema_id", "generic.service_sales"))

    @property
    def industry_code(self) -> str:
        return str(self._get_schema().get("industry_code", "generic"))

    @staticmethod
    def _merge_unique(*collections: Any) -> List[str]:
        merged: List[str] = []
        seen = set()
        for collection in collections:
            for item in list(collection or []):
                value = str(item or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                merged.append(value)
        return merged

    @staticmethod
    def _intent_from_name(name: str):
        from src.common.types.intent import IntentType
        normalized = str(name or "").strip().lower()
        for intent in IntentType:
            if intent.value == normalized:
                return intent
        return None

    def get_domain_keywords(self) -> set[str]:
        config = self._get_followup_config()
        keywords = config.get("domain_keywords") or []
        return set(keywords)

    def get_domain_intent_patterns(self) -> Dict[str, Any]:
        return self._get_schema().get("intent_keywords") or {}

    def score_domain_match(self, query: str, item: Any) -> float:
        domain_keywords = self.get_domain_keywords()
        if not domain_keywords:
            return 0.0
        query_lower = str(query or "").lower()
        item_text = ""
        if isinstance(item, dict):
            item_text = " ".join(str(v) for v in item.values() if isinstance(v, str))
        else:
            item_text = str(getattr(item, "question", "") or "") + " " + str(getattr(item, "answer", "") or "")
        item_lower = item_text.lower()
        match_count = sum(1 for kw in domain_keywords if kw.lower() in query_lower or kw.lower() in item_lower)
        return min(match_count / max(len(domain_keywords), 1), 1.0)

    def extract_conversion_signals(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, float]:
        config = self._get_followup_config()
        signal_terms = config.get("signal_terms") or {}
        if not signal_terms:
            return {}

        combined_text = self._build_combined_text(message, history)
        signals: Dict[str, float] = {}
        for signal_name, keywords in signal_terms.items():
            if not isinstance(keywords, (list, tuple)):
                continue
            count = sum(1 for kw in keywords if kw in combined_text)
            if count > 0:
                signals[signal_name] = min(count / max(len(keywords), 1) * 2.0, 1.0)
        return signals

    def infer_conversion_stage(
        self,
        message: str,
        intent_result: Any = None,
        signals: Optional[Dict[str, float]] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        customer_data: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> ConversionStage:
        del intent_result, customer_data
        if signals is None:
            signals = self.extract_conversion_signals(message, history)

        if signals.get("buying_signal", 0) >= 0.5 or signals.get("reservation_signal", 0) >= 0.5:
            return ConversionStage.RESERVATION
        if signals.get("contact_acceptance_signal", 0) >= 0.4:
            return ConversionStage.HANDOFF
        if signals.get("comparison_signal", 0) >= 0.3 or signals.get("urgency_signal", 0) >= 0.3:
            return ConversionStage.HIGH_INTENT
        if any(v >= 0.2 for v in signals.values()):
            return ConversionStage.CONSIDERATION
        if any(v > 0 for v in signals.values()):
            return ConversionStage.QUALIFICATION
        return ConversionStage.DISCOVERY

    def get_required_slots_for_stage(self, stage: ConversionStage, intent_result: Any = None, **kwargs) -> List[str]:
        del intent_result
        config = self._get_followup_config()
        slot_config = config.get("required_slots") or {}
        stage_name = stage.name.lower() if stage else ""
        return slot_config.get(stage_name, [])

    def prioritize_missing_slots(
        self,
        stage: ConversionStage,
        missing_slots: List[str],
        intent_result: Any = None,
        **kwargs,
    ) -> List[str]:
        del intent_result
        config = self._get_followup_config()
        priority_map = config.get("slot_priority") or {}
        stage_name = stage.name.lower() if stage else ""
        stage_priorities = priority_map.get(stage_name, missing_slots)
        ordered = []
        for slot in stage_priorities:
            if slot in missing_slots and slot not in ordered:
                ordered.append(slot)
        for slot in missing_slots:
            if slot not in ordered:
                ordered.append(slot)
        return ordered

    def extract_known_slots(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
        customer_data: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Dict[str, str]:
        del customer_data
        config = self._get_followup_config()
        slot_terms = config.get("known_slot_terms") or {}
        slot_name_map = config.get("slot_name_map") or {}
        if not slot_terms:
            return {}

        combined_text = self._build_combined_text(message, history)
        slots: Dict[str, str] = {}

        time_terms = slot_terms.get("time_terms", [])
        if any(t in combined_text for t in time_terms):
            slot_key = slot_name_map.get("time_terms", "时间")
            slots[slot_key] = "yes"

        party_terms = slot_terms.get("party_terms", [])
        if any(t in combined_text for t in party_terms):
            slot_key = slot_name_map.get("party_terms", "数量")
            slots[slot_key] = "yes"

        route_terms = slot_terms.get("route_terms", [])
        if any(t in combined_text for t in route_terms):
            slot_key = slot_name_map.get("route_terms", "对象")
            slots[slot_key] = "yes"

        contact_terms = slot_terms.get("contact_terms", [])
        if any(t in combined_text for t in contact_terms):
            slot_key = slot_name_map.get("contact_terms", "联系方式")
            slots[slot_key] = "yes"

        return slots

    def build_stage_cta(
        self,
        objective: Any,
        message: str = "",
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        del history
        config = self._get_followup_config()
        templates = config.get("stage_cta_templates") or {}
        if isinstance(objective, ConversionStage):
            stage = objective
        else:
            stage = getattr(objective, "conversion_stage", None)
        stage_name = stage.name.lower() if stage else ""
        return templates.get(stage_name, "")

    def build_clarification_reply(
        self,
        objective: Any,
        message: str = "",
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        del history
        if isinstance(objective, list):
            missing_slots = list(objective or [])
        else:
            missing_slots = list(getattr(objective, "missing_slots", []) or [])
        config = self._get_followup_config()
        templates = config.get("clarification_templates") or {}
        if not missing_slots or not templates:
            return ""
        for slot in missing_slots:
            for template_key, template_text in templates.items():
                if slot in template_key or template_key in slot:
                    return template_text
        return list(templates.values())[0] if templates else ""

    def get_reply_persona(self, mode: str = "") -> str:
        del mode
        config = self._get_reply_config()
        return config.get("sales_persona", "")

    def get_safe_generation_rules(self, mode: str = "") -> List[str]:
        del mode
        config = self._get_reply_config()
        return config.get("domain_rules", [])

    def is_generic_followup(self, service: Any, message: str, **kwargs) -> bool:
        del service
        config = self._get_followup_config()
        short_clues = config.get("short_followup_clues") or []
        short_leads = config.get("short_followup_leads") or []
        msg = str(message or "").strip()
        if len(msg) <= 4 and any(clue in msg for clue in short_clues):
            return True
        if len(msg) <= 8 and any(lead in msg for lead in short_leads):
            return True
        return False

    def get_recent_anchor(
        self,
        service: Any,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
    ) -> str:
        del service, session_id
        config = self._get_followup_config()
        anchor_terms = config.get("anchor_terms") or []
        if not anchor_terms:
            return ""
        for msg in reversed(conversation_history or []):
            content = str(msg.get("content") or msg.get("message") or "")
            for term in sorted(anchor_terms, key=len, reverse=True):
                if term in content:
                    return term
        return ""

    def expand_followup_query(
        self,
        service: Any,
        message: str,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
        **kwargs,
    ) -> str:
        anchor = self.get_recent_anchor(service, conversation_history or [], session_id=session_id)
        if not anchor:
            return message
        config = self._get_followup_config()
        generic_patterns = config.get("generic_patterns") or {}
        expanded = generic_patterns.get(message, message)
        result = self._rule_engine.evaluate(anchor, expanded, conversation_history)
        if result:
            return result
        if anchor and expanded and anchor != expanded:
            return f"{anchor}{expanded}"
        return expanded

    def infer_followup_intent(
        self,
        service: Any,
        message: str,
        grounded_routes: Optional[List[str]] = None,
        **kwargs,
    ) -> Optional[str]:
        if isinstance(service, str) and isinstance(message, list):
            message = service
        del grounded_routes
        config = self._get_followup_config()
        intent_terms = config.get("followup_intent_terms") or {}
        combined = str(message or "")
        for intent_name, keywords in intent_terms.items():
            if not isinstance(keywords, (list, tuple)):
                continue
            if any(kw in combined for kw in keywords):
                return intent_name
        return None

    def build_safe_no_answer_reply(
        self,
        reason: str,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        del reason, message, history
        config = self._get_reply_config()
        base_replies = config.get("base_replies") or {}
        template = base_replies.get("fallback", "")
        return template

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

    def remember_grounded_context(
        self,
        service: Any,
        *,
        session_id: str = "",
        retrieval_query: str = "",
        matched_knowledge=None,
        reply_analysis=None,
        retrieved_contexts=None,
        **kwargs,
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

    def refine_intent_with_grounded_context(
        self,
        service: Any,
        message: str,
        intent_result: Any,
        session_id: str,
    ) -> Any:
        del service, message, session_id
        return intent_result
