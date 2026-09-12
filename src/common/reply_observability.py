"""
回复主链观测汇总

为回复主链输出统一的阶段摘要，并维护轻量级聚合统计。
"""
from __future__ import annotations

import copy

from typing import Any, Dict, List, Optional

from .tracing_support import build_trace_snapshot


def build_mainline_observability(
    *,
    mode_name: str,
    enterprise_id: str,
    intent: str,
    need_human: bool,
    matched_knowledge: Any,
    reply_analysis: Optional[Dict[str, Any]],
    reply_objective: Any = None,
) -> Dict[str, Any]:
    reply_analysis = reply_analysis or {}
    retrieval = reply_analysis.get("retrieval") or {}
    fallback = reply_analysis.get("fallback") or {}
    rag_llm = reply_analysis.get("rag_llm") or {}
    trace = {
        **build_trace_snapshot(),
        **(reply_analysis.get("trace") or {}),
    }
    spans = list(trace.get("spans") or [])

    stage_metrics = _build_stage_metrics(reply_analysis, spans)
    span_names = [str(span.get("name") or "") for span in spans if str(span.get("name") or "")]
    fallback_source = str(fallback.get("source") or "")
    final_action = str(retrieval.get("final_action") or "")
    reply_source = str(rag_llm.get("source") or fallback_source or "")
    reason_code = str(
        retrieval.get("reason_code")
        or fallback.get("reason_code")
        or rag_llm.get("reason_code")
        or ""
    )
    retrieval_query = str(retrieval.get("query") or "")
    top_hits = _build_top_hits_snapshot(retrieval)
    if not top_hits:
        fallback_hit = _build_matched_knowledge_snapshot(matched_knowledge)
        if fallback_hit:
            top_hits = [fallback_hit]
    sample_id = str(trace.get("request_id") or trace.get("trace_id") or "")
    inferred_action = _infer_sample_action(
        reason_code=reason_code,
        reply_source=reply_source,
        final_action=final_action,
        need_human=bool(need_human),
    )
    event_sequence = _build_event_sequence(span_names, reply_analysis, reason_code)
    sample = {
        "sample_id": sample_id,
        "enterprise_id": str(enterprise_id or "default"),
        "request_id": str(trace.get("request_id") or ""),
        "trace_id": str(trace.get("trace_id") or ""),
        "root_span_id": str(trace.get("root_span_id") or ""),
        "current_span_id": str(trace.get("current_span_id") or trace.get("span_id") or ""),
        "intent": str(intent or ""),
        "reason_code": reason_code,
        "reply_source": reply_source or "unknown",
        "fallback_source": fallback_source or "none",
        "retrieval_final_action": final_action or "unknown",
        "retrieval_query": retrieval_query,
        "retrieval_result_count": int(retrieval.get("result_count", 0) or 0),
        "top_hits": top_hits,
        "matched_knowledge": _normalize_matched_knowledge(matched_knowledge),
        "need_human": bool(need_human),
        "action": inferred_action,
        "reply": _extract_reply_text(reply_analysis),
        "workflow_state": _extract_workflow_state(reply_analysis, need_human, final_action),
        "generation_policy": _copy_dict(reply_analysis.get("generation_policy")),
        "guardrail": _copy_dict(reply_analysis.get("guardrail")),
        "routing": _copy_dict(reply_analysis.get("routing")),
        "verification": _build_verification_snapshot(reply_analysis),
        "event_sequence": event_sequence,
        "trace_span_names": span_names,
        "trace_span_count": len(span_names),
    }

    return {
        "mode": mode_name,
        "enterprise_id": str(enterprise_id or "default"),
        "intent": str(intent or ""),
        "conversion_stage": getattr(getattr(reply_objective, "conversion_stage", ""), "value", ""),
        "cta_mode": getattr(getattr(reply_objective, "cta_mode", ""), "value", ""),
        "need_human": bool(need_human),
        "matched_knowledge": bool(matched_knowledge),
        "retrieval_requested": bool(retrieval.get("requested", False)),
        "retrieval_result_count": int(retrieval.get("result_count", 0) or 0),
        "retrieval_final_action": final_action,
        "reason_code": reason_code,
        "fallback_source": fallback_source,
        "reply_source": reply_source or "unknown",
        "generation_source": str(rag_llm.get("source") or fallback_source or "unknown"),
        "structured_bundle": bool(retrieval.get("structured_bundle", False)),
        "rewrite_attempted": bool(retrieval.get("rewrite_attempted", False)),
        "stage_metrics_ms": stage_metrics,
        "trace_span_names": span_names,
        "trace_span_count": len(span_names),
        "trace": {
            "request_id": str(trace.get("request_id") or ""),
            "trace_id": str(trace.get("trace_id") or ""),
            "span_id": str(trace.get("span_id") or ""),
            "root_span_id": str(trace.get("root_span_id") or ""),
            "current_span_id": str(trace.get("current_span_id") or trace.get("span_id") or ""),
            "parent_span_id": str(trace.get("parent_span_id") or ""),
        },
        "sample": sample,
    }


def update_mainline_observability_stats(
    stats: Dict[str, Any],
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    stats["total_requests"] = int(stats.get("total_requests", 0) or 0) + 1

    enterprise_id = str(summary.get("enterprise_id") or "default")
    intent = str(summary.get("intent") or "unknown")
    final_action = str(summary.get("retrieval_final_action") or "unknown")
    reason_code = str(summary.get("reason_code") or "none")
    reply_source = str(summary.get("reply_source") or "unknown")
    fallback_source = str(summary.get("fallback_source") or "none")
    generation_source = str(summary.get("generation_source") or "unknown")
    mode_name = str(summary.get("mode") or "unknown")

    _increment(stats.setdefault("modes", {}), mode_name)
    _increment(stats.setdefault("final_actions", {}), final_action)
    _increment(stats.setdefault("reason_codes", {}), reason_code)
    _increment(stats.setdefault("reply_sources", {}), reply_source)
    _increment(stats.setdefault("fallback_sources", {}), fallback_source)
    _increment(stats.setdefault("generation_sources", {}), generation_source)
    samples = stats.setdefault("samples", [])
    sample_payload = summary.get("sample")
    if isinstance(sample_payload, dict) and sample_payload:
        samples.append(sample_payload)
        if len(samples) > 50:
            del samples[:-50]

    by_enterprise = stats.setdefault("by_enterprise", {})
    enterprise_bucket = by_enterprise.setdefault(
        enterprise_id,
        {
            "total_requests": 0,
            "intents": {},
            "final_actions": {},
            "reason_codes": {},
            "reply_sources": {},
            "fallback_sources": {},
        },
    )
    enterprise_bucket["total_requests"] = int(enterprise_bucket.get("total_requests", 0) or 0) + 1
    _increment(enterprise_bucket.setdefault("intents", {}), intent)
    _increment(enterprise_bucket.setdefault("final_actions", {}), final_action)
    _increment(enterprise_bucket.setdefault("reason_codes", {}), reason_code)
    _increment(enterprise_bucket.setdefault("reply_sources", {}), reply_source)
    _increment(enterprise_bucket.setdefault("fallback_sources", {}), fallback_source)

    by_intent = stats.setdefault("by_intent", {})
    intent_bucket = by_intent.setdefault(
        intent,
        {
            "total_requests": 0,
            "enterprises": {},
            "final_actions": {},
            "reason_codes": {},
            "reply_sources": {},
            "fallback_sources": {},
        },
    )
    intent_bucket["total_requests"] = int(intent_bucket.get("total_requests", 0) or 0) + 1
    _increment(intent_bucket.setdefault("enterprises", {}), enterprise_id)
    _increment(intent_bucket.setdefault("final_actions", {}), final_action)
    _increment(intent_bucket.setdefault("reason_codes", {}), reason_code)
    _increment(intent_bucket.setdefault("reply_sources", {}), reply_source)
    _increment(intent_bucket.setdefault("fallback_sources", {}), fallback_source)
    return stats


def _build_stage_metrics(reply_analysis: Dict[str, Any], spans: List[Dict[str, Any]]) -> Dict[str, float]:
    metrics: Dict[str, float] = {}
    retrieval_perf = reply_analysis.get("retrieval", {}).get("perf") or {}
    generation_perf = reply_analysis.get("generation_perf") or {}
    for key, value in retrieval_perf.items():
        try:
            metrics[f"retrieval.{key}"] = round(float(value), 1)
        except (TypeError, ValueError):
            continue
    for key, value in generation_perf.items():
        try:
            metrics[f"generation.{key}"] = round(float(value), 1)
        except (TypeError, ValueError):
            continue
    for span in spans:
        name = str(span.get("name") or "").strip()
        if not name:
            continue
        try:
            metrics[f"trace.{name}"] = round(float(span.get("duration_ms", 0.0) or 0.0), 1)
        except (TypeError, ValueError):
            continue
    return metrics


def _increment(bucket: Dict[str, int], key: str) -> None:
    bucket[key] = int(bucket.get(key, 0) or 0) + 1


def _normalize_matched_knowledge(matched_knowledge: Any) -> str:
    if isinstance(matched_knowledge, str):
        return matched_knowledge.strip()
    if hasattr(matched_knowledge, "question"):
        return str(getattr(matched_knowledge, "question", "") or "").strip()
    if isinstance(matched_knowledge, dict):
        return str(matched_knowledge.get("question") or matched_knowledge.get("id") or "").strip()
    return ""


def _build_top_hits_snapshot(retrieval: Dict[str, Any]) -> List[Dict[str, Any]]:
    contexts = retrieval.get("contexts") or retrieval.get("retrieved_contexts") or []
    if not isinstance(contexts, list):
        return []
    top_hits: List[Dict[str, Any]] = []
    for item in contexts[:3]:
        if not isinstance(item, dict):
            continue
        top_hits.append(
            {
                "question": str(item.get("question") or item.get("content") or "")[:120],
                "category": str(item.get("category") or ""),
                "source": str(item.get("source") or ""),
                "score": item.get("score"),
            }
        )
    return top_hits


def _build_matched_knowledge_snapshot(matched_knowledge: Any) -> Optional[Dict[str, Any]]:
    if hasattr(matched_knowledge, "question"):
        question = str(getattr(matched_knowledge, "question", "") or "").strip()
        category = str(getattr(matched_knowledge, "category", "") or "")
    elif isinstance(matched_knowledge, dict):
        question = str(matched_knowledge.get("question") or matched_knowledge.get("id") or "").strip()
        category = str(matched_knowledge.get("category") or "")
    elif isinstance(matched_knowledge, str):
        question = matched_knowledge.strip()
        category = ""
    else:
        return None
    if not question:
        return None
    return {
        "question": question[:120],
        "category": category,
        "source": "matched_knowledge",
        "score": None,
    }


def _copy_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict) and payload:
        return copy.deepcopy(payload)
    return {}


def _extract_reply_text(reply_analysis: Dict[str, Any]) -> str:
    candidate_paths = (
        reply_analysis.get("reply"),
        (reply_analysis.get("rag_llm") or {}).get("reply"),
        (reply_analysis.get("rag_llm") or {}).get("final_reply"),
        (reply_analysis.get("fallback") or {}).get("reply"),
        (reply_analysis.get("fallback") or {}).get("final_reply"),
    )
    for value in candidate_paths:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _extract_workflow_state(reply_analysis: Dict[str, Any], need_human: bool, final_action: str) -> str:
    workflow = reply_analysis.get("workflow") if isinstance(reply_analysis.get("workflow"), dict) else {}
    explicit_state = str(workflow.get("state") or "").strip()
    if explicit_state:
        return explicit_state
    if need_human or final_action == "handoff":
        return "handoff"
    if final_action in {"reply", "done"}:
        return "done"
    if final_action:
        return final_action
    return ""


def _infer_sample_action(
    *,
    reason_code: str,
    reply_source: str,
    final_action: str,
    need_human: bool,
) -> str:
    normalized_reason = str(reason_code or "").strip()
    normalized_final_action = str(final_action or "").strip()
    normalized_reply_source = str(reply_source or "").strip()
    if normalized_reason == "reply_sent" or normalized_final_action == "reply":
        return "send"
    if "retry" in normalized_reason or normalized_final_action == "retry":
        return "retry"
    if "mismatch" in normalized_reason or normalized_final_action == "pause":
        return "pause"
    if need_human or normalized_final_action == "handoff":
        return "ack_only"
    if normalized_reply_source not in {"", "unknown", "human_handoff"}:
        return "send"
    return "ack_only"


def _build_event_sequence(span_names: List[str], reply_analysis: Dict[str, Any], reason_code: str) -> List[str]:
    sequence = [str(item or "").strip() for item in span_names if str(item or "").strip()]
    if not sequence:
        if reply_analysis.get("retrieval"):
            sequence.append("retrieval")
        if reply_analysis.get("rag_llm") or reply_analysis.get("generation_policy"):
            sequence.append("generation")
        if reply_analysis.get("guardrail"):
            sequence.append("guardrail_apply")
    normalized_reason = str(reason_code or "").strip()
    if normalized_reason == "reply_sent" and "send_confirmed" not in sequence:
        sequence.append("send_confirmed")
    if normalized_reason.startswith("blocked_") and "handoff_ticket" not in sequence:
        sequence.append("handoff_ticket")
    return sequence


def _build_verification_snapshot(reply_analysis: Dict[str, Any]) -> Dict[str, Any]:
    verification = reply_analysis.get("verification")
    if isinstance(verification, dict) and verification:
        return copy.deepcopy(verification)
    send_verification = reply_analysis.get("send_verification")
    if isinstance(send_verification, dict) and send_verification:
        return copy.deepcopy(send_verification)
    return {}
