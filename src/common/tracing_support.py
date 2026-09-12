"""
统一 tracing / routing 支撑工具。

为不同回复链和 RAG 编排链提供一致的 trace 快照、span 追加和 routing 写入能力。
"""
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional

from src.common.types import RoutingDecision
from src.infrastructure.logger import get_request_context, request_context


@dataclass
class SpanHandle:
    """统一 span 句柄，支持 start/end 和上下文管理。"""
    payload: Dict[str, Any]
    span_id: str
    parent_span_id: str
    name: str
    stage: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0


def build_trace_snapshot() -> Dict[str, Any]:
    """获取当前请求上下文的最小 trace 快照。"""
    ctx = get_request_context()
    current_span_id = ctx.get("current_span_id", ctx.get("span_id", ""))
    span_stack = list(ctx.get("span_stack", []) or [])
    parent_span_id = span_stack[-2] if len(span_stack) >= 2 else ""
    return {
        "request_id": ctx.get("request_id", ""),
        "trace_id": ctx.get("trace_id", ""),
        "span_id": current_span_id,
        "root_span_id": ctx.get("root_span_id", ctx.get("span_id", "")),
        "current_span_id": current_span_id,
        "parent_span_id": parent_span_id,
    }


def merge_trace(existing_trace: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合并已有 trace 与当前上下文，保留已有 spans。"""
    existing_trace = existing_trace or {}
    trace_snapshot = build_trace_snapshot()
    merged_trace = {
        **trace_snapshot,
        **existing_trace,
    }
    merged_trace.setdefault("root_span_id", existing_trace.get("root_span_id") or trace_snapshot.get("span_id", ""))
    merged_trace.setdefault("spans", existing_trace.get("spans", []))
    return merged_trace


def _update_request_context(**updates: Any) -> Dict[str, Any]:
    ctx = dict(get_request_context() or {})
    ctx.update(updates)
    request_context.set(ctx)
    return ctx


def _push_active_span(span_id: str) -> Dict[str, Any]:
    ctx = dict(get_request_context() or {})
    span_stack = list(ctx.get("span_stack", []) or [])
    span_stack.append(span_id)
    ctx["span_stack"] = span_stack
    ctx["current_span_id"] = span_id
    ctx["span_id"] = span_id
    request_context.set(ctx)
    return ctx


def _pop_active_span(span_id: str) -> Dict[str, Any]:
    ctx = dict(get_request_context() or {})
    span_stack = list(ctx.get("span_stack", []) or [])
    if span_stack and span_stack[-1] == span_id:
        span_stack.pop()
    else:
        span_stack = [item for item in span_stack if item != span_id]
    root_span_id = ctx.get("root_span_id", "")
    current_span_id = span_stack[-1] if span_stack else root_span_id
    ctx["span_stack"] = span_stack
    ctx["current_span_id"] = current_span_id
    ctx["span_id"] = current_span_id
    request_context.set(ctx)
    return ctx


def attach_routing_decision(
    payload: Optional[Dict[str, Any]],
    decision: RoutingDecision,
) -> Dict[str, Any]:
    """把路由决策写入一个可序列化 payload。"""
    if payload is None:
        payload = {}
    existing_routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
    routing_payload = decision.to_dict()
    for preserved_key in ("execution", "pipeline", "orchestration"):
        if preserved_key in existing_routing:
            routing_payload[preserved_key] = existing_routing[preserved_key]
    payload["routing"] = routing_payload
    payload["trace"] = merge_trace(payload.get("trace"))
    return payload


def attach_execution_routing(
    payload: Optional[Dict[str, Any]],
    decision: RoutingDecision,
) -> Dict[str, Any]:
    """把执行层 agent routing 写入 payload.routing.execution。"""
    if payload is None:
        payload = {}
    routing = payload.setdefault("routing", {})
    if not isinstance(routing, dict):
        routing = {}
        payload["routing"] = routing
    routing["execution"] = decision.to_dict()
    payload["trace"] = merge_trace(payload.get("trace"))
    return payload


def attach_orchestration_plan(
    payload: Optional[Dict[str, Any]],
    plan: Dict[str, Any],
) -> Dict[str, Any]:
    """把多 agent orchestrator 计划写入 payload.routing.orchestration。"""
    if payload is None:
        payload = {}
    routing = payload.setdefault("routing", {})
    if not isinstance(routing, dict):
        routing = {}
        payload["routing"] = routing
    routing["orchestration"] = dict(plan or {})
    payload["trace"] = merge_trace(payload.get("trace"))
    return payload


def start_trace_span(
    payload: Optional[Dict[str, Any]],
    *,
    name: str,
    metadata: Optional[Dict[str, Any]] = None,
    parent_span_id: Optional[str] = None,
    stage: str = "",
) -> SpanHandle:
    """开始一个 span，并把它设为当前活跃 span。"""
    if payload is None:
        payload = {}
    trace = payload.setdefault("trace", merge_trace())
    trace.setdefault("spans", [])
    ctx = get_request_context()
    active_parent_span_id = (
        parent_span_id
        if parent_span_id is not None
        else ctx.get("current_span_id", trace.get("root_span_id", trace.get("span_id", "")))
    )
    span_id = uuid.uuid4().hex[:16]
    trace["spans"].append(
        {
            "span_id": span_id,
            "parent_span_id": active_parent_span_id,
            "name": name,
            "stage": stage,
            "status": "running",
            "error": "",
            "duration_ms": 0.0,
            "metadata": dict(metadata or {}),
        }
    )
    _push_active_span(span_id)
    return SpanHandle(
        payload=payload,
        span_id=span_id,
        parent_span_id=active_parent_span_id,
        name=name,
        stage=stage,
        metadata=dict(metadata or {}),
        started_at=time.perf_counter(),
    )


def end_trace_span(
    handle: SpanHandle,
    *,
    status: str = "ok",
    error: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """结束一个 span，写入耗时和状态，并恢复当前活跃 span。"""
    payload = handle.payload or {}
    trace = payload.setdefault("trace", merge_trace())
    spans = trace.setdefault("spans", [])
    for span in reversed(spans):
        if span.get("span_id") == handle.span_id:
            metadata_updates = dict(metadata or {})
            duration_override = metadata_updates.pop("duration_ms_override", None)
            span["status"] = status
            span["error"] = error
            span["duration_ms"] = (
                round(float(duration_override), 1)
                if duration_override is not None
                else round((time.perf_counter() - handle.started_at) * 1000, 1)
            )
            merged_metadata = dict(span.get("metadata", {}) or {})
            merged_metadata.update(metadata_updates)
            span["metadata"] = merged_metadata
            break
    _pop_active_span(handle.span_id)
    trace["current_span_id"] = get_request_context().get("current_span_id", trace.get("root_span_id", ""))
    trace["parent_span_id"] = get_request_context().get("parent_span_id", "")
    trace["span_id"] = trace.get("current_span_id", trace.get("span_id", ""))
    return payload


@contextmanager
def trace_span(
    payload: Optional[Dict[str, Any]],
    *,
    name: str,
    metadata: Optional[Dict[str, Any]] = None,
    parent_span_id: Optional[str] = None,
    stage: str = "",
) -> Iterator[SpanHandle]:
    """统一 span 上下文管理器，自动维护父子链路与状态。"""
    handle = start_trace_span(
        payload,
        name=name,
        metadata=metadata,
        parent_span_id=parent_span_id,
        stage=stage,
    )
    try:
        yield handle
    except Exception as exc:
        end_trace_span(handle, status="error", error=str(exc))
        raise
    else:
        end_trace_span(handle, status="ok")


def append_trace_span(
    payload: Optional[Dict[str, Any]],
    *,
    name: str,
    duration_ms: float,
    metadata: Optional[Dict[str, Any]] = None,
    parent_span_id: Optional[str] = None,
    stage: str = "",
    status: str = "ok",
    error: str = "",
) -> Dict[str, Any]:
    """追加 span 到 payload.trace.spans。"""
    handle = start_trace_span(
        payload,
        name=name,
        metadata=metadata,
        parent_span_id=parent_span_id,
        stage=stage,
    )
    return end_trace_span(
        handle,
        status=status,
        error=error,
        metadata={"duration_ms_override": round(float(duration_ms), 1)},
    )


def merge_trace_payload(
    target_payload: Optional[Dict[str, Any]],
    source_payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """把 source 的 trace/spans 合并进 target，保留 target 现有字段优先级。"""
    if target_payload is None:
        target_payload = {}
    if not source_payload:
        return target_payload

    source_trace = (source_payload.get("trace") or {}) if isinstance(source_payload, dict) else {}
    if not source_trace:
        return target_payload

    target_trace = target_payload.setdefault("trace", merge_trace())
    target_spans = target_trace.setdefault("spans", [])
    source_spans = source_trace.get("spans", [])
    if source_spans:
        target_spans[:0] = list(source_spans)
    return target_payload
