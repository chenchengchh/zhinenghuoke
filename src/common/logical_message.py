from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_inbound_time_bucket(value: Any, bucket_seconds: int = 10) -> str:
    """将入站事件时间规整到短时间桶，兼容跨入口重复采集。"""
    if value in (None, ""):
        return ""

    timestamp_value = None
    try:
        timestamp_value = float(value)
    except (TypeError, ValueError):
        text = _normalize_text(value)
        if text:
            try:
                timestamp_value = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                timestamp_value = None

    if timestamp_value is None:
        return ""

    bucket = int(timestamp_value // max(bucket_seconds, 1))
    return str(bucket)


def build_logical_message_id(
    *,
    platform: str,
    conversation_id: str,
    customer_name: str,
    direction: str,
    content: str,
    source_message_id: str = "",
    source_timestamp: Any = "",
) -> str:
    """生成跨通道稳定逻辑消息 ID。"""
    normalized_direction = _normalize_text(direction or "inbound")
    normalized_parts = [
        _normalize_text(platform or "unknown"),
        _normalize_text(conversation_id),
        _normalize_text(customer_name),
        normalized_direction,
        _normalize_text(content),
    ]

    if normalized_direction == "inbound":
        normalized_parts.append(_normalize_inbound_time_bucket(source_timestamp))
    else:
        normalized_parts.extend([
            _normalize_text(source_message_id),
            _normalize_text(source_timestamp),
        ])

    digest = hashlib.md5("|".join(normalized_parts).encode("utf-8")).hexdigest()[:24]
    direction_prefix = normalized_direction[:3] or "msg"
    return f"lmsg_{direction_prefix}_{digest}"
