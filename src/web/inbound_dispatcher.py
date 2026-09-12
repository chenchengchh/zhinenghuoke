from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING, List

from loguru import logger

from src.web.inbound_detector import InboundDetectionResult

if TYPE_CHECKING:
    from src.web.bot_service import BotService


@dataclass(frozen=True)
class InboundDispatchResult:
    detection: InboundDetectionResult
    submitted: bool
    reason: str
    source: str

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@dataclass(frozen=True)
class InboundRouteRequest:
    detection: InboundDetectionResult
    source: str
    apply_echo_guard: bool
    apply_self_reply_guard: bool

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class InboundDispatcher:
    """统一承接主链路由请求构建与提交。"""

    def __init__(self, bot_service: "BotService"):
        self.bot = bot_service

    def build_route_requests(
        self,
        events: List[InboundDetectionResult],
        *,
        source: str,
        apply_echo_guard: bool = False,
        apply_self_reply_guard: bool = False,
    ) -> List[InboundRouteRequest]:
        route_requests: List[InboundRouteRequest] = []
        for detection in events:
            dispatch_source = self.resolve_dispatch_source(
                fallback_source=source,
                signal_source=detection.signal_source,
            )
            route_requests.append(
                InboundRouteRequest(
                    detection=detection,
                    source=dispatch_source,
                    apply_echo_guard=apply_echo_guard,
                    apply_self_reply_guard=apply_self_reply_guard,
                )
            )
        return route_requests

    def submit_route_requests(
        self,
        route_requests: List[InboundRouteRequest],
    ) -> List[InboundDispatchResult]:
        if not route_requests:
            return []

        bot = self.bot
        results: List[InboundDispatchResult] = []
        for route_request in route_requests:
            detection = route_request.detection
            trace_id = str(
                getattr(detection, "logical_message_id", "")
                or getattr(detection, "msg_id", "")
                or ""
            )[:12]
            logger.info(
                f"[入站分发] submit trace={trace_id or '-'} source={route_request.source} "
                f"signal_source={str(getattr(detection, 'signal_source', '') or '-')} "
                f"customer={detection.customer_name or '-'} "
                f"conversation_id={str(getattr(detection, 'conversation_id', '') or '-')} "
                f"source_message_id={str(getattr(detection, 'msg_id', '') or '-')} "
                f"logical_message_id={str(getattr(detection, 'logical_message_id', '') or '-')} "
                f"content={(detection.content or '')[:40]}"
            )
            route_result = bot._route_live_inbound_message(
                source=route_request.source,
                customer_name=detection.customer_name,
                content=detection.content,
                direction=detection.direction,
                is_new=bool(detection.is_new),
                conversation_id=detection.conversation_id,
                platform=detection.platform,
                customer_id=detection.customer_id,
                msg_id=detection.msg_id,
                timestamp=detection.timestamp,
                signal_source=detection.signal_source,
                apply_echo_guard=route_request.apply_echo_guard,
                apply_self_reply_guard=route_request.apply_self_reply_guard,
                direction_confidence=getattr(detection, 'direction_confidence', 'high') or 'high',
                enterprise_id=getattr(detection, "enterprise_id", "") or "",
                preferred_schema_id=getattr(detection, "preferred_schema_id", "") or "",
                schema_id=getattr(detection, "schema_id", "") or "",
                tenant_resolution_mode=getattr(detection, "tenant_resolution_mode", "") or "",
            )
            if isinstance(route_result, tuple) and len(route_result) >= 2:
                submitted, reason = bool(route_result[0]), str(route_result[1] or "")
            elif route_result is None:
                submitted, reason = True, "submitted"
            else:
                submitted = bool(route_result)
                reason = "submitted" if submitted else "failed"

            results.append(
                InboundDispatchResult(
                    detection=detection,
                    submitted=submitted,
                    reason=reason,
                    source=route_request.source,
                )
            )
            logger.info(
                f"[入站分发] result trace={trace_id or '-'} source={route_request.source} "
                f"submitted={submitted} reason={reason or '-'} "
                f"conversation_id={str(getattr(detection, 'conversation_id', '') or '-')} "
                f"source_message_id={str(getattr(detection, 'msg_id', '') or '-')} "
                f"logical_message_id={str(getattr(detection, 'logical_message_id', '') or '-')}"
            )
        return results

    @staticmethod
    def resolve_dispatch_source(*, fallback_source: str, signal_source: Any) -> str:
        normalized_fallback_source = str(fallback_source or "").strip()
        if normalized_fallback_source in {"RPA消息回调", "统一监听回调"}:
            return normalized_fallback_source

        normalized_signal_source = str(signal_source or "").strip().lower()
        if normalized_signal_source == "api_intercept":
            return "API拦截轮询"
        if normalized_signal_source.startswith("network_"):
            return "网络拦截轮询"
        if normalized_signal_source.startswith("active_snapshot"):
            return "活动快照轮询"
        if normalized_signal_source == "dom_snapshot":
            return "DOM快照轮询"
        if normalized_signal_source == "active_preview_fallback":
            return "活动预览兜底"
        if normalized_signal_source == "non_active_preview_fallback":
            return "非活动预览兜底"
        if normalized_signal_source in {"conversation_preview", "preview"}:
            return "传统预览轮询"
        return str(normalized_fallback_source or "统一监听轮询")
