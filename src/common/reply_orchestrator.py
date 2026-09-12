"""
回复主链编排器

将增强回复链中的 retrieval -> generation -> guardrail 后半段整理为可复用编排层，
便于从超大服务类中逐步拆出稳定阶段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .causal_reasoning import causal_reasoning_engine
from .priority_decision import priority_decision_engine
from .reply_observability import build_mainline_observability
from .risk_assessment import risk_assessment_engine


@dataclass(frozen=True)
class ReplyOrchestrationMode:
    name: str
    context_window_size: int
    rag_complexity: float
    record_learning_match: bool
    include_implied_facts: bool
    causal_label: str
    use_full_risk_assessment: bool = False
    allow_need_human: bool = False
    run_proactive: bool = False
    auto_save_learning: bool = False


HIGH_TOUCH_ANALYSIS_MODE = ReplyOrchestrationMode(
    name="high_touch_analysis",
    context_window_size=5,
    rag_complexity=0.7,
    record_learning_match=True,
    include_implied_facts=True,
    causal_label="因果分析",
    use_full_risk_assessment=True,
    allow_need_human=True,
)

STANDARD_ANALYSIS_MODE = ReplyOrchestrationMode(
    name="standard_analysis",
    context_window_size=3,
    rag_complexity=0.5,
    record_learning_match=False,
    include_implied_facts=False,
    causal_label="因果",
    use_full_risk_assessment=False,
    allow_need_human=False,
    run_proactive=True,
    auto_save_learning=True,
)

LIGHTWEIGHT_INTERACTION_MODE = ReplyOrchestrationMode(
    name="lightweight_interaction",
    context_window_size=1,
    rag_complexity=0.0,
    record_learning_match=False,
    include_implied_facts=False,
    causal_label="",
    use_full_risk_assessment=False,
    allow_need_human=False,
    run_proactive=False,
    auto_save_learning=False,
)


class ReplyOrchestrator:
    """编排增强回复链后半段的统一流程。"""

    @staticmethod
    def _require_stage(service: Any, attr_name: str, method_name: str) -> Any:
        stage = getattr(service, attr_name, None)
        if stage is None or not hasattr(stage, method_name):
            raise RuntimeError(
                f"ReplyOrchestrator requires {attr_name}.{method_name} and no longer falls back to legacy service methods"
            )
        return stage

    def run(
        self,
        *,
        service: Any,
        mode: ReplyOrchestrationMode,
        message: str,
        customer_name: str,
        conversation_history: Optional[List[Dict[str, Any]]],
        customer_data: Dict[str, Any],
        base_result: Dict[str, Any],
        intent_result: Any,
        retrieval_query: Optional[str] = None,
        session_id: Optional[str] = None,
        reply_objective: Any = None,
        rag_need_retrieval: bool = True,
        generation_mode: str = "sales",
    ) -> Dict[str, Any]:
        if mode.name == "lightweight_interaction":
            return self._run_lightweight(
                service=service,
                message=message,
                customer_name=customer_name,
                intent_result=intent_result,
                base_result=base_result,
            )

        context = conversation_history or []
        context_text = [msg.get("content", "") for msg in context[-mode.context_window_size:]]
        search_query = retrieval_query or message
        intent_value = str(getattr(getattr(intent_result, "primaryIntent", None), "value", "") or "")
        enterprise_id = str(service._resolve_enterprise_id(customer_data, conversation_history) or "")

        reasoning_result = causal_reasoning_engine.reason(message, context_text)
        intent_level = service._map_intent_to_level(intent_result.primaryIntent)

        risk_profile = None
        risk_level_str = "low"
        risk_level = "low"
        if mode.use_full_risk_assessment:
            risk_profile = risk_assessment_engine.assess_comprehensive_risk(
                customer_data,
                conversation_history,
                context_text,
            )
            risk_level_str = self._extract_risk_level(risk_profile)
            risk_level = service._map_risk_level(risk_level_str)

        priority_decision = priority_decision_engine.decide_priority(
            customer_data=customer_data,
            message=message,
            intent=intent_value,
            risk_level=risk_level,
        )

        retrieval_stage = self._require_stage(service, "_retrieval_stage", "collect")
        matched_knowledge, reply_analysis, retrieved_contexts = retrieval_stage.collect(
            service=service,
            message=message,
            search_query=search_query,
            conversation_history=conversation_history,
            customer_data=customer_data,
            intent_result=intent_result,
            rag_need_retrieval=rag_need_retrieval,
            rag_complexity=mode.rag_complexity,
            record_learning_match=mode.record_learning_match,
            reply_objective=reply_objective,
        )

        generation_stage = self._require_stage(service, "_generation_stage", "generate")
        smart_reply = generation_stage.generate(
            service=service,
            mode=generation_mode,
            message=message,
            retrieval_query=search_query,
            enterprise_id=enterprise_id,
            conversation_history=conversation_history,
            matched_knowledge=matched_knowledge,
            reply_analysis=reply_analysis,
            retrieved_contexts=retrieved_contexts,
            reply_objective=reply_objective,
        )

        enhanced_result = service._build_analysis_result(
            base_result=base_result,
            intent_result=intent_result,
            intent_level=intent_level,
            priority_decision=priority_decision,
            reasoning=self._build_reasoning(reasoning_result, intent_value, mode),
            suggested_action=self._build_suggested_action(
                service=service,
                mode=mode,
                priority_decision=priority_decision,
                risk_profile=risk_profile,
                intent_result=intent_result,
            ),
            need_human=self._should_need_human(
                intent_result=intent_result,
                risk_level_str=risk_level_str,
                mode=mode,
            ),
            extra_fields={
                "risk_assessment": self._build_risk_assessment_payload(
                    service=service,
                    mode=mode,
                    risk_profile=risk_profile,
                ),
                "reply_objective": self._build_reply_objective_payload(reply_objective),
            },
        )

        guardrail_stage = self._require_stage(service, "_guardrail_stage", "apply")
        guardrail_stage.apply(
            service=service,
            enhanced_result=enhanced_result,
            smart_reply=smart_reply,
            matched_knowledge=matched_knowledge,
            reply_analysis=reply_analysis,
            retrieved_contexts=retrieved_contexts,
            message=message,
            conversation_history=conversation_history,
            session_id=session_id,
            customer_name=customer_name,
        )

        observability = build_mainline_observability(
            mode_name=mode.name,
            enterprise_id=enterprise_id,
            intent=intent_value,
            need_human=bool(enhanced_result.get("need_human", False)),
            matched_knowledge=matched_knowledge,
            reply_analysis=reply_analysis,
            reply_objective=reply_objective,
        )
        enhanced_result["mainline_observability"] = observability
        if isinstance(reply_analysis, dict):
            reply_analysis["stage_summary"] = observability
        if hasattr(service, "_record_mainline_observability_summary"):
            service._record_mainline_observability_summary(observability)

        service._run_learning(
            message,
            conversation_history,
            enhanced_result,
            matched_knowledge,
            retrieved_contexts,
        )

        if mode.run_proactive:
            customer_id = str((customer_data or {}).get("sec_uid") or "unknown")
            service._run_proactive_service(customer_id, enhanced_result)

        if mode.auto_save_learning:
            service._learning_engine.auto_save_if_needed_async()

        return enhanced_result

    @staticmethod
    def _extract_risk_level(risk_profile: Any) -> str:
        if isinstance(risk_profile, dict):
            overall_risk = risk_profile.get("overall_risk_level", "low")
            if hasattr(overall_risk, "value"):
                return str(overall_risk.value)
            return str(overall_risk)
        overall_risk = getattr(risk_profile, "overall_risk_level", "low")
        if hasattr(overall_risk, "value"):
            return str(overall_risk.value)
        return str(overall_risk)

    @staticmethod
    def _build_reasoning(
        reasoning_result: Dict[str, Any],
        intent_value: str,
        mode: ReplyOrchestrationMode,
    ) -> str:
        reasoning_parts: List[str] = []
        if reasoning_result.get("causes") and reasoning_result.get("effects"):
            reasoning_parts.append(mode.causal_label)
        if mode.include_implied_facts and reasoning_result.get("implied_facts"):
            reasoning_parts.append(f"常识{len(reasoning_result.get('implied_facts', []))}条")
        reasoning_parts.append(intent_value)
        return " | ".join([part for part in reasoning_parts if part])

    @staticmethod
    def _build_reply_objective_payload(reply_objective: Any) -> Dict[str, Any]:
        if not reply_objective:
            return {
                "conversion_stage": "",
                "next_best_action": "",
                "cta_mode": "",
                "missing_slots": [],
                "reason": "",
            }
        return {
            "conversion_stage": getattr(getattr(reply_objective, "conversion_stage", ""), "value", ""),
            "next_best_action": getattr(getattr(reply_objective, "next_best_action", ""), "value", ""),
            "cta_mode": getattr(getattr(reply_objective, "cta_mode", ""), "value", ""),
            "missing_slots": list(getattr(reply_objective, "missing_slots", []) or []),
            "reason": getattr(reply_objective, "reason", "") or "",
        }

    @staticmethod
    def _build_risk_assessment_payload(
        *,
        service: Any,
        mode: ReplyOrchestrationMode,
        risk_profile: Any,
    ) -> Dict[str, Any]:
        if mode.use_full_risk_assessment and risk_profile is not None:
            return service._build_risk_assessment(risk_profile)
        return {
            "overall_level": "low",
            "overall_score": 20,
        }

    @staticmethod
    def _build_suggested_action(
        *,
        service: Any,
        mode: ReplyOrchestrationMode,
        priority_decision: Dict[str, Any],
        risk_profile: Any,
        intent_result: Any,
    ) -> str:
        if mode.use_full_risk_assessment and risk_profile is not None:
            return service._generate_suggested_action(priority_decision, risk_profile, intent_result)
        return service._generate_simple_action(priority_decision)

    @staticmethod
    def _should_need_human(
        *,
        intent_result: Any,
        risk_level_str: str,
        mode: ReplyOrchestrationMode,
    ) -> bool:
        if not mode.allow_need_human:
            return False
        if risk_level_str in {"critical", "high"}:
            return True
        intent_value = str(getattr(getattr(intent_result, "primaryIntent", None), "value", "") or "")
        return intent_value == "complaint"

    @staticmethod
    def _run_lightweight(
        *,
        service: Any,
        message: str,
        customer_name: str,
        intent_result: Any,
        base_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        intent_value = str(getattr(getattr(intent_result, "primaryIntent", None), "value", "") or "")
        lightweight_replies = {
            "greeting": f"您好{customer_name}！很高兴为您服务，有什么可以帮您的吗？",
            "farewell": f"感谢您的咨询{customer_name}，祝您生活愉快！",
            "thanks": f"不客气{customer_name}，还有其他问题随时问我！",
            "confirmation": "好的，收到！",
            "status_inquiry": "我在的，随时为您服务！",
            "ready_check": "您好，我已经准备好了，请问有什么可以帮您的？",
        }
        reply = lightweight_replies.get(intent_value, f"您好，有什么可以帮您的吗？")

        enhanced_result = dict(base_result) if base_result else {}
        enhanced_result.update({
            "reply": reply,
            "intent": intent_value,
            "intent_level": "lightweight",
            "reasoning": "lightweight_interaction",
            "need_human": False,
            "priority": "low",
            "suggested_action": "no_action",
            "risk_assessment": {"overall_level": "low", "overall_score": 0},
            "mainline_observability": {
                "mode": "lightweight_interaction",
                "intent": intent_value,
            },
        })
        return enhanced_result
