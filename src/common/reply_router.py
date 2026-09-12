"""
统一回复路由模块

收口主回复链的路由选择，避免路由规则散落在 `EnhancedCustomerService.process_message()`
中，便于后续接入更复杂的 agent routing 策略。
"""
from typing import Any, Dict, Iterable, Optional

from src.common.types import RoutingDecision


def _normalize_intent(intent: Any) -> str:
    if hasattr(intent, "value"):
        return str(intent.value)
    return str(intent or "")


def _normalize_intent_set(intents: Iterable[Any]) -> set[str]:
    return {_normalize_intent(item) for item in intents}


def decide_unified_route(
    *,
    intent: Any,
    confidence: float,
    retrieval_final_action: str = "",
    matched_knowledge: bool = False,
    used_generated_reply: bool = False,
    need_human: bool = False,
    lightweight_interaction_intents: Optional[Iterable[Any]] = None,
    high_priority_intents: Optional[Iterable[Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> RoutingDecision:
    """
    统一回复路由决策（Phase 2 合并函数）。

    整合了"主路由选择"和"回复执行选择"两阶段逻辑，
    避免多层路由之间决策不一致。
    返回单一 strategy，对应 RAG 链路最终由哪个执行体完成。
    """
    normalized_intent = _normalize_intent(intent)
    lightweight_set = _normalize_intent_set(lightweight_interaction_intents or [])
    high_priority_set = _normalize_intent_set(high_priority_intents or [])
    metadata = dict(metadata or {})
    metadata.setdefault("intent", normalized_intent)
    metadata["is_lightweight"] = normalized_intent in lightweight_set
    metadata["is_high_priority"] = normalized_intent in high_priority_set
    metadata["retrieval_final_action"] = retrieval_final_action
    metadata["matched_knowledge"] = bool(matched_knowledge)

    # 优先级 1：人工接管（高风险/投诉）
    if need_human:
        return RoutingDecision(
            route_name="human_handoff_agent",
            reason="risk_or_intent_requires_human",
            confidence=1.0,
            stage="unified_route",
            selected_strategy="human_handoff",
            fallback_route="llm_generation_agent",
            route_scores={"human_handoff": 1.0, "llm": 0.3, "rag": 0.2, "direct": 0.1},
            metadata=metadata,
        )

    # 优先级 2：轻量交互（问候/道别/致谢）
    # 与 confidence 完全解耦，一旦识别为社交意图，强制走快速通道
    if normalized_intent in lightweight_set:
        return RoutingDecision(
            route_name="lightweight_interaction",
            reason="lightweight_intent_direct_reply",
            confidence=max(confidence, 0.7),
            stage="unified_route",
            selected_strategy="direct_reply",
            fallback_route="llm_generation_agent",
            route_scores={"direct": 1.0, "llm": 0.3, "rag": 0.1, "human": 0.0},
            metadata=metadata,
        )

    # 优先级 3：检索被显式拒绝 → 安全无回答
    if retrieval_final_action == "no_answer" and not used_generated_reply:
        return RoutingDecision(
            route_name="safe_no_answer_agent",
            reason="retrieval_gate_rejected",
            confidence=0.9,
            stage="unified_route",
            selected_strategy="safe_no_answer",
            fallback_route="llm_generation_agent",
            route_scores={"safe_no_answer": 0.9, "llm": 0.3, "rag": 0.0},
            metadata=metadata,
        )

    # 优先级 4：知识库直接命中
    if matched_knowledge or retrieval_final_action == "pass":
        return RoutingDecision(
            route_name="retrieval_augmented_agent",
            reason="knowledge_grounded_reply",
            confidence=max(confidence, 0.7),
            stage="unified_route",
            selected_strategy="knowledge_grounded",
            fallback_route="llm_generation_agent",
            route_scores={"rag": 0.85, "llm": 0.5, "direct": 0.3},
            metadata=metadata,
        )

    # 优先级 5：LLM 生成（兜底主力）
    if used_generated_reply:
        return RoutingDecision(
            route_name="llm_generation_agent",
            reason="llm_generated_reply",
            confidence=max(confidence, 0.65),
            stage="unified_route",
            selected_strategy="rag_llm_generation",
            fallback_route="retrieval_augmented_agent",
            route_scores={"llm": 0.85, "rag": 0.5, "direct": 0.2},
            metadata=metadata,
        )

    # 优先级 6：基础回复（最后兜底）
    return RoutingDecision(
        route_name="base_reply_agent",
        reason="default_execution_route",
        confidence=max(confidence, 0.6),
        stage="unified_route",
        selected_strategy="base_result",
        fallback_route="llm_generation_agent",
        route_scores={"base": 0.7, "llm": 0.5, "rag": 0.3},
        metadata=metadata,
    )


def decide_main_reply_route(
    *,
    intent: Any,
    confidence: float,
    reply_objective: Optional[Any] = None,
    use_enhanced: bool,
    math_result_available: bool,
    lightweight_interaction_intents: Iterable[Any],
    high_priority_intents: Iterable[Any],
    metadata: Dict[str, Any] | None = None,
) -> RoutingDecision:
    """统一决定主回复链应走哪条路由。"""
    normalized_intent = _normalize_intent(intent)
    metadata = dict(metadata or {})
    metadata.setdefault("intent", normalized_intent)
    metadata.setdefault("execution_mode", "standard_analysis")
    metadata.setdefault("routing_role", "execution_route")
    metadata.setdefault("observability_label", "standard_analysis")
    if reply_objective is not None:
        metadata.setdefault(
            "conversion_stage",
            str(getattr(getattr(reply_objective, "conversion_stage", None), "value", "") or ""),
        )
        metadata.setdefault(
            "next_best_action",
            str(getattr(getattr(reply_objective, "next_best_action", None), "value", "") or ""),
        )
    metadata["use_enhanced"] = bool(use_enhanced)
    metadata["math_result_available"] = bool(math_result_available)
    metadata["lightweight_interaction_candidate"] = normalized_intent in _normalize_intent_set(lightweight_interaction_intents)
    metadata["high_touch_candidate"] = normalized_intent in _normalize_intent_set(high_priority_intents)

    if metadata["lightweight_interaction_candidate"]:
        metadata["observability_label"] = "lightweight_interaction"
        metadata["candidate_strategy"] = "direct_reply"
        return RoutingDecision(
            route_name="standard_analysis",
            reason="standard_analysis_with_lightweight_label",
            confidence=confidence,
            stage="process_message",
            selected_strategy="standard_analysis",
            fallback_route="llm_only_fallback",
            route_scores={
                "single_mainline": round(max(confidence, 0.5), 3),
                "lightweight_interaction_candidate": round(1.0, 3),
                "high_touch_candidate": round(0.0, 3),
            },
            metadata=metadata,
        )

    if metadata["high_touch_candidate"] and confidence >= 0.6:
        metadata["observability_label"] = "high_touch_analysis"
        metadata["candidate_strategy"] = "high_touch_analysis"
        return RoutingDecision(
            route_name="standard_analysis",
            reason="standard_analysis_with_high_touch_label",
            confidence=confidence,
            stage="process_message",
            selected_strategy="standard_analysis",
            fallback_route="llm_only_fallback",
            route_scores={
                "single_mainline": round(max(confidence, 0.5), 3),
                "lightweight_interaction_candidate": round(0.0, 3),
                "high_touch_candidate": round(1.0, 3),
            },
            metadata=metadata,
        )

    return RoutingDecision(
        route_name="standard_analysis",
        reason="standard_rag_pipeline",
        confidence=confidence,
        stage="process_message",
        selected_strategy="standard_analysis",
        fallback_route="llm_only_fallback",
        route_scores={
            "single_mainline": round(max(confidence, 0.5), 3),
            "lightweight_interaction_candidate": round(1.0 if metadata["lightweight_interaction_candidate"] else 0.0, 3),
            "high_touch_candidate": round(1.0 if metadata["high_touch_candidate"] else 0.0, 3),
        },
        metadata=metadata,
    )


def decide_rag_upgrade_route(
    *,
    intent: Any,
    confidence: float,
    need_retrieval: bool,
    complexity: float,
    modular_confidence: float,
    modular_available: bool,
    modular_retrieved: bool,
    high_complexity_intents: Iterable[Any],
    agentic_min_complexity: float,
    agentic_confidence_threshold: float,
    metadata: Dict[str, Any] | None = None,
) -> RoutingDecision:
    """统一决定 UnifiedRAGService 是否升级到 agentic RAG。"""
    normalized_intent = _normalize_intent(intent)
    metadata = dict(metadata or {})
    metadata.setdefault("intent", normalized_intent)

    route_scores = {
        "direct_reply": round(1.0 if not need_retrieval else 0.0, 3),
        "modular_rag": round(max(modular_confidence, 0.0), 3),
        "agentic_rag": 0.0,
        "fallback_generation": round(0.6 if not modular_available else 0.2, 3),
    }

    if not need_retrieval:
        return RoutingDecision(
            route_name="direct_reply",
            reason="no_retrieval_needed",
            confidence=max(confidence, 0.9),
            stage="rag_pipeline",
            selected_strategy="direct_reply",
            fallback_route="modular_rag",
            route_scores=route_scores,
            metadata=metadata,
        )

    agentic_score = 0.0
    if normalized_intent in _normalize_intent_set(high_complexity_intents):
        agentic_score += 0.6
    if complexity >= agentic_min_complexity:
        agentic_score += 0.2
    if modular_confidence < agentic_confidence_threshold:
        agentic_score += 0.2
    route_scores["agentic_rag"] = round(min(agentic_score, 1.0), 3)

    if (
        normalized_intent in _normalize_intent_set(high_complexity_intents)
        or (complexity >= agentic_min_complexity and modular_available and modular_retrieved and modular_confidence < agentic_confidence_threshold)
        or complexity >= 0.85
    ):
        return RoutingDecision(
            route_name="agentic_rag",
            reason="agentic_upgrade",
            confidence=route_scores["agentic_rag"],
            stage="rag_pipeline",
            selected_strategy="knowledge_graph",
            fallback_route="modular_rag",
            route_scores=route_scores,
            metadata=metadata,
        )

    if modular_available:
        return RoutingDecision(
            route_name="modular_rag",
            reason="modular_result_available",
            confidence=max(modular_confidence, 0.0),
            stage="rag_pipeline",
            selected_strategy="modular_rag",
            fallback_route="fallback_generation",
            route_scores=route_scores,
            metadata=metadata,
        )

    return RoutingDecision(
        route_name="fallback_generation",
        reason="modular_result_missing",
        confidence=route_scores["fallback_generation"],
        stage="rag_pipeline",
        selected_strategy="fallback_generation",
        fallback_route="direct_reply",
        route_scores=route_scores,
        metadata=metadata,
    )


def decide_reply_execution_route(
    *,
    main_route: str,
    retrieval_final_action: str = "",
    retrieved_count: int = 0,
    reply_source: str = "",
    matched_knowledge: bool = False,
    used_generated_reply: bool = False,
    need_human: bool = False,
    metadata: Dict[str, Any] | None = None,
) -> RoutingDecision:
    """统一决定主回复链最终由哪个执行 agent 完成回复。"""
    metadata = dict(metadata or {})
    metadata.update(
        {
            "main_route": main_route,
            "retrieval_final_action": retrieval_final_action,
            "retrieved_count": int(retrieved_count or 0),
            "reply_source": reply_source,
            "matched_knowledge": bool(matched_knowledge),
            "used_generated_reply": bool(used_generated_reply),
            "need_human": bool(need_human),
        }
    )

    if need_human:
        return RoutingDecision(
            route_name="human_handoff_agent",
            reason="risk_or_intent_requires_human",
            confidence=1.0,
            stage="reply_execution",
            selected_strategy="human_handoff",
            fallback_route="llm_generation_agent",
            executor="human_handoff",
            route_scores={
                "human_handoff_agent": 1.0,
                "llm_generation_agent": 0.3,
                "retrieval_augmented_agent": 0.2,
                "base_reply_agent": 0.1,
            },
            metadata=metadata,
        )

    if retrieval_final_action == "no_answer":
        return RoutingDecision(
            route_name="safe_no_answer_agent",
            reason="retrieval_gate_rejected",
            confidence=0.92,
            stage="reply_execution",
            selected_strategy="safe_no_answer",
            fallback_route="human_handoff_agent",
            executor="safe_no_answer",
            handoff_reason="low_evidence_guardrail",
            route_scores={
                "safe_no_answer_agent": 0.92,
                "human_handoff_agent": 0.6,
                "llm_generation_agent": 0.0,
            },
            metadata=metadata,
        )

    if reply_source.startswith("rag_llm") or (used_generated_reply and reply_source not in {"knowledge_base_direct", "knowledge_base_direct_timeout"}):
        return RoutingDecision(
            route_name="llm_generation_agent",
            reason="llm_generated_reply",
            confidence=0.86,
            stage="reply_execution",
            selected_strategy="rag_llm_generation",
            fallback_route="retrieval_augmented_agent",
            executor="llm_generation",
            route_scores={
                "llm_generation_agent": 0.86,
                "retrieval_augmented_agent": 0.7,
                "base_reply_agent": 0.2,
            },
            metadata=metadata,
        )

    if matched_knowledge or retrieved_count > 0 or retrieval_final_action == "pass":
        return RoutingDecision(
            route_name="retrieval_augmented_agent",
            reason="retrieval_context_available",
            confidence=0.84,
            stage="reply_execution",
            selected_strategy="knowledge_grounded_reply",
            fallback_route="base_reply_agent",
            executor="knowledge_retrieval",
            route_scores={
                "retrieval_augmented_agent": 0.84,
                "llm_generation_agent": 0.5,
                "base_reply_agent": 0.3,
            },
            metadata=metadata,
        )

    return RoutingDecision(
        route_name="base_reply_agent",
        reason="default_execution_route",
        confidence=0.7,
        stage="reply_execution",
        selected_strategy="base_result",
        fallback_route="llm_generation_agent",
        executor="base_reply",
        route_scores={
            "base_reply_agent": 0.7,
            "llm_generation_agent": 0.4,
            "retrieval_augmented_agent": 0.2,
        },
        metadata=metadata,
    )


def decide_rag_execution_route(
    *,
    final_route: str,
    preferred_route: str = "",
    modular_available: bool = False,
    modular_confidence: float = 0.0,
    agentic_attempted: bool = False,
    agentic_confidence: float = 0.0,
    tools_used: Iterable[str] | None = None,
    metadata: Dict[str, Any] | None = None,
) -> RoutingDecision:
    """统一决定统一 RAG 链最终由哪个执行 agent 完成答案。"""
    tools = [str(tool) for tool in (tools_used or []) if tool]
    metadata = dict(metadata or {})
    metadata.update(
        {
            "final_route": final_route,
            "preferred_route": preferred_route or final_route,
            "modular_available": bool(modular_available),
            "modular_confidence": float(modular_confidence or 0.0),
            "agentic_attempted": bool(agentic_attempted),
            "agentic_confidence": float(agentic_confidence or 0.0),
            "tools_used": tools,
        }
    )

    route_scores = {
        "direct_reply_agent": round(1.0 if final_route == "direct_reply" else 0.0, 3),
        "modular_rag_agent": round(max(float(modular_confidence or 0.0), 0.0), 3),
        "agentic_rag_agent": round(max(float(agentic_confidence or 0.0), 0.0), 3),
        "fallback_generation_agent": round(0.7 if final_route == "fallback_generation" else 0.2, 3),
    }

    if final_route == "direct_reply":
        return RoutingDecision(
            route_name="direct_reply_agent",
            reason="rag_pipeline_direct_reply",
            confidence=0.95,
            stage="rag_execution",
            selected_strategy="direct_reply",
            fallback_route="modular_rag_agent",
            executor="direct_reply",
            route_scores=route_scores,
            metadata=metadata,
        )

    if final_route == "agentic_rag":
        return RoutingDecision(
            route_name="agentic_rag_agent",
            reason="agentic_result_selected",
            confidence=max(float(agentic_confidence or 0.0), route_scores["agentic_rag_agent"]),
            stage="rag_execution",
            selected_strategy="knowledge_graph",
            fallback_route="modular_rag_agent",
            executor="agentic_rag",
            handoff_reason="modular_to_agentic_upgrade" if preferred_route != final_route or agentic_attempted else "",
            route_scores=route_scores,
            metadata=metadata,
        )

    if final_route == "modular_rag":
        return RoutingDecision(
            route_name="modular_rag_agent",
            reason="modular_result_selected",
            confidence=max(float(modular_confidence or 0.0), route_scores["modular_rag_agent"]),
            stage="rag_execution",
            selected_strategy="modular_rag",
            fallback_route="fallback_generation_agent",
            executor="modular_rag",
            handoff_reason="agentic_result_weaker" if agentic_attempted and preferred_route == "agentic_rag" else "",
            route_scores=route_scores,
            metadata=metadata,
        )

    return RoutingDecision(
        route_name="fallback_generation_agent",
        reason="rag_fallback_generation",
        confidence=route_scores["fallback_generation_agent"],
        stage="rag_execution",
        selected_strategy="fallback_generation",
        fallback_route="direct_reply_agent",
        executor="fallback_generation",
        handoff_reason="all_rag_paths_unavailable" if not modular_available else "",
        route_scores=route_scores,
        metadata=metadata,
    )
