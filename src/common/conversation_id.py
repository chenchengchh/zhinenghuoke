from __future__ import annotations

import hashlib
from typing import Any, Optional

from src.common.chat_store import ChatStoreFacade


def build_conversation_id(
    customer_name: str,
    platform: str = "douyin",
    customer_id: str = "",
) -> str:
    """生成稳定会话 ID，优先使用平台 customer_id。"""
    normalized_platform = (platform or "unknown").strip() or "unknown"
    normalized_customer_id = (customer_id or "").strip()
    if normalized_customer_id:
        return f"{normalized_platform}_{normalized_customer_id}"

    normalized_name = (customer_name or "anonymous").strip() or "anonymous"
    return (
        f"{normalized_platform}_nickname_"
        f"{hashlib.md5(normalized_name.encode('utf-8')).hexdigest()[:12]}"
    )


def resolve_conversation_id(
    *,
    db: Optional[Any] = None,
    customer_name: str,
    platform: str = "douyin",
    customer_id: str = "",
    explicit_conversation_id: str = "",
) -> str:
    """兼容历史数据，优先显式 ID，其次使用稳定 ID，必要时回落旧 ID。"""
    explicit_conversation_id = (explicit_conversation_id or "").strip()
    if explicit_conversation_id:
        return explicit_conversation_id

    stable_conversation_id = build_conversation_id(customer_name, platform, customer_id)
    if not customer_id or db is None:
        return stable_conversation_id

    try:
        find_by_customer_id = getattr(db, "find_best_conversation_id_by_customer_id", None)
        if callable(find_by_customer_id):
            customer_primary_id = find_by_customer_id(
                customer_id=customer_id,
                platform=platform,
            )
            if customer_primary_id:
                return customer_primary_id
    except Exception:
        pass

    legacy_conversation_id = build_conversation_id(customer_name, platform, "")
    if legacy_conversation_id == stable_conversation_id:
        return stable_conversation_id

    try:
        chat_store = ChatStoreFacade(db)
        legacy_messages = chat_store.get_recent_messages_dicts(legacy_conversation_id, limit=1)
        if legacy_messages:
            return legacy_conversation_id
    except Exception:
        pass

    return stable_conversation_id
