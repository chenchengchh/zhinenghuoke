from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class InboundDetectionResult:
    customer_name: str
    content: str
    direction: str
    is_new: bool
    conversation_id: str
    platform: str
    customer_id: str
    msg_id: str
    timestamp: Any
    signal_source: str
    direction_confidence: str = "high"
    direction_source: str = ""
    sender_id: str = ""
    enterprise_id: str = ""
    preferred_schema_id: str = ""
    tenant_resolution_mode: str = ""
    schema_id: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class InboundDetector:
    """统一承接入站检测结果标准化与多证据归并。"""

    def normalize_traditional_message(self, message: Dict[str, Any]) -> InboundDetectionResult:
        return InboundDetectionResult(
            customer_name=str(message.get("customer_name", "") or ""),
            content=str(message.get("last_message_content", message.get("content", "")) or ""),
            direction=str(message.get("direction", "inbound") or "inbound"),
            is_new=bool(message.get("is_new", False)),
            conversation_id=str(message.get("conversation_id", "") or ""),
            platform=str(message.get("platform", "douyin") or "douyin"),
            customer_id=str(message.get("customer_id", "") or ""),
            msg_id=str(message.get("msg_id", "") or ""),
            timestamp=message.get("timestamp", ""),
            signal_source=str(message.get("signal_source", "") or ""),
        )

    def normalize_rpa_message(self, message: Any) -> InboundDetectionResult:
        if isinstance(message, dict):
            getter = lambda obj, key, default="": obj.get(key, default)
        else:
            getter = lambda obj, key, default="": getattr(obj, key, default)
        direction = getter(message, "direction", "inbound")
        if hasattr(direction, "value"):
            direction = getattr(direction, "value", direction)
        return InboundDetectionResult(
            customer_name=str(getter(message, "customer_name", "") or ""),
            content=str(getter(message, "content", "") or ""),
            direction=str(direction or "inbound"),
            is_new=bool(getter(message, "is_new", True)),
            conversation_id=str(getter(message, "conversation_id", "") or ""),
            platform=str(getter(message, "platform", "douyin") or "douyin"),
            customer_id=str(getter(message, "customer_id", "") or ""),
            msg_id=str(getter(message, "msg_id", "") or ""),
            timestamp=getter(message, "timestamp", ""),
            signal_source=str(getter(message, "signal_source", "") or ""),
            direction_confidence=str(getter(message, "direction_confidence", "high") or "high"),
            direction_source=str(getter(message, "direction_source", "") or ""),
            sender_id=str(getter(message, "sender_id", "") or ""),
            enterprise_id=str(getter(message, "enterprise_id", "") or ""),
            preferred_schema_id=str(getter(message, "preferred_schema_id", "") or ""),
            tenant_resolution_mode=str(getter(message, "tenant_resolution_mode", "") or ""),
            schema_id=str(getter(message, "schema_id", "") or ""),
        )

    def coerce_detection_result(self, event: Any) -> InboundDetectionResult | None:
        if isinstance(event, InboundDetectionResult):
            return event
        if isinstance(event, dict):
            return InboundDetectionResult(
                customer_name=str(event.get("customer_name", "") or ""),
                content=str(event.get("content", "") or ""),
                direction=str(event.get("direction", "inbound") or "inbound"),
                is_new=bool(event.get("is_new", False)),
                conversation_id=str(event.get("conversation_id", "") or ""),
                platform=str(event.get("platform", "douyin") or "douyin"),
                customer_id=str(event.get("customer_id", "") or ""),
                msg_id=str(event.get("msg_id", "") or ""),
                timestamp=event.get("timestamp", ""),
                signal_source=str(event.get("signal_source", "") or ""),
                direction_confidence=str(event.get("direction_confidence", "high") or "high"),
                direction_source=str(event.get("direction_source", "") or ""),
                sender_id=str(event.get("sender_id", "") or ""),
                enterprise_id=str(event.get("enterprise_id", "") or ""),
                preferred_schema_id=str(event.get("preferred_schema_id", "") or ""),
                tenant_resolution_mode=str(event.get("tenant_resolution_mode", "") or ""),
                schema_id=str(event.get("schema_id", "") or ""),
            )
        return None

    def merge_detection_results(self, events: List[Any]) -> List[InboundDetectionResult]:
        merged: Dict[str, InboundDetectionResult] = {}
        for event in events:
            detection = self.coerce_detection_result(event)
            if detection is None:
                continue
            merge_key = self.build_detection_merge_key(detection)
            previous = merged.get(merge_key)
            if previous is None:
                merged[merge_key] = detection
                continue
            merged[merge_key] = self.prefer_detection_result(previous, detection)
        return list(merged.values())

    @staticmethod
    def build_detection_merge_key(detection: InboundDetectionResult) -> str:
        conversation_key = (
            str(detection.conversation_id or "").strip()
            or str(detection.customer_id or "").strip()
            or str(detection.customer_name or "").strip()
        )
        normalized_content = " ".join(str(detection.content or "").split()).strip().lower()
        return "|".join(
            [
                str(detection.platform or "douyin").strip().lower(),
                conversation_key.lower(),
                str(detection.direction or "inbound").strip().lower(),
                normalized_content,
            ]
        )

    def prefer_detection_result(
        self,
        current: InboundDetectionResult,
        candidate: InboundDetectionResult,
    ) -> InboundDetectionResult:
        current_priority = self.signal_source_priority(current.signal_source)
        candidate_priority = self.signal_source_priority(candidate.signal_source)
        if candidate_priority > current_priority:
            preferred, other = candidate, current
        elif candidate_priority < current_priority:
            preferred, other = current, candidate
        else:
            preferred, other = self.tie_break_detection_result(current, candidate)

        return InboundDetectionResult(
            customer_name=preferred.customer_name or other.customer_name,
            content=preferred.content or other.content,
            direction=preferred.direction or other.direction,
            is_new=bool(preferred.is_new or other.is_new),
            conversation_id=preferred.conversation_id or other.conversation_id,
            platform=preferred.platform or other.platform or "douyin",
            customer_id=preferred.customer_id or other.customer_id,
            msg_id=preferred.msg_id or other.msg_id,
            timestamp=preferred.timestamp if preferred.timestamp not in ("", None) else other.timestamp,
            signal_source=preferred.signal_source or other.signal_source,
            direction_confidence=preferred.direction_confidence if preferred.direction_confidence != "high" else other.direction_confidence,
            direction_source=preferred.direction_source or other.direction_source,
            sender_id=preferred.sender_id or other.sender_id,
            enterprise_id=preferred.enterprise_id or other.enterprise_id,
            preferred_schema_id=preferred.preferred_schema_id or other.preferred_schema_id,
            tenant_resolution_mode=preferred.tenant_resolution_mode or other.tenant_resolution_mode,
            schema_id=preferred.schema_id or other.schema_id,
        )

    @staticmethod
    def tie_break_detection_result(
        current: InboundDetectionResult,
        candidate: InboundDetectionResult,
    ) -> tuple[InboundDetectionResult, InboundDetectionResult]:
        if candidate.msg_id and not current.msg_id:
            return candidate, current
        if current.msg_id and not candidate.msg_id:
            return current, candidate
        current_ts = InboundDetector.normalize_sortable_timestamp(current.timestamp)
        candidate_ts = InboundDetector.normalize_sortable_timestamp(candidate.timestamp)
        if candidate_ts >= current_ts:
            return candidate, current
        return current, candidate

    @staticmethod
    def normalize_sortable_timestamp(value: Any) -> float:
        try:
            return float(value or 0)
        except Exception:
            return 0.0

    @staticmethod
    def signal_source_priority(signal_source: Any) -> int:
        normalized = str(signal_source or "").strip().lower()
        if normalized == "api_intercept":
            return 50
        if normalized.startswith("network_"):
            return 45
        if normalized.startswith("active_snapshot"):
            return 40
        if normalized == "dom_snapshot":
            return 35
        if normalized in {"active_preview_fallback", "non_active_preview_fallback"}:
            return 20
        if normalized in {"conversation_preview", "preview"}:
            return 10
        return 0
