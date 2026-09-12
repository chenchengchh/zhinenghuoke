"""聊天数据访问层骨架。

当前阶段先提供统一的数据模型和仓储接口，把上层模块与 JSON/SQLite 细节解耦。
后续阶段可在不改调用方契约的前提下，将底层实现切换到 SQLite 主读。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.common.database import DatabaseManager


@dataclass
class MessageRecord:
    message_id: str = ""
    logical_message_id: str = ""
    source_message_id: str = ""
    conversation_id: str = ""
    customer_id: str = ""
    platform: str = "douyin"
    direction: str = "inbound"
    message_type: str = "text"
    content: str = ""
    sender_id: str = ""
    sender_name: str = ""
    is_read: bool = False
    is_processed: bool = False
    ai_reply_content: str = ""
    created_at: str = ""
    customer_name: str = ""
    processed_status: str = ""
    processed_at: str = ""
    processed_reason: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MessageRecord":
        payload = dict(data or {})
        return cls(
            message_id=str(payload.get("message_id", "") or ""),
            logical_message_id=str(payload.get("logical_message_id", "") or ""),
            source_message_id=str(payload.get("source_message_id", "") or ""),
            conversation_id=str(payload.get("conversation_id", "") or ""),
            customer_id=str(payload.get("customer_id", "") or ""),
            platform=str(payload.get("platform", "douyin") or "douyin"),
            direction=str(payload.get("direction", "inbound") or "inbound"),
            message_type=str(payload.get("message_type", "text") or "text"),
            content=str(payload.get("content", "") or ""),
            sender_id=str(payload.get("sender_id", "") or ""),
            sender_name=str(payload.get("sender_name", "") or ""),
            is_read=bool(payload.get("is_read", False)),
            is_processed=bool(payload.get("is_processed", False)),
            ai_reply_content=str(payload.get("ai_reply_content", "") or ""),
            created_at=str(payload.get("created_at", "") or ""),
            customer_name=str(payload.get("customer_name", "") or ""),
            processed_status=str(payload.get("processed_status", "") or ""),
            processed_at=str(payload.get("processed_at", "") or ""),
            processed_reason=str(payload.get("processed_reason", "") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "logical_message_id": self.logical_message_id,
            "source_message_id": self.source_message_id,
            "conversation_id": self.conversation_id,
            "customer_id": self.customer_id,
            "platform": self.platform,
            "direction": self.direction,
            "message_type": self.message_type,
            "content": self.content,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "is_read": self.is_read,
            "is_processed": self.is_processed,
            "ai_reply_content": self.ai_reply_content,
            "created_at": self.created_at,
            "customer_name": self.customer_name,
            "processed_status": self.processed_status,
            "processed_at": self.processed_at,
            "processed_reason": self.processed_reason,
        }


@dataclass
class ConversationSummaryRecord:
    conversation_id: str = ""
    platform: str = "douyin"
    customer_id: str = ""
    customer_name: str = ""
    customer_avatar: str = ""
    status: str = "active"
    last_message_time: str = ""
    last_message_content: str = ""
    unread_count: int = 0
    intent_level: str = ""
    purchase_intent_score: float = 0.0
    lead_score: str = "cold"
    follow_up_priority: str = "low"
    purchase_probability: float = 0.0
    estimated_deal_size: float = 0.0
    lifecycle_stage: str = "prospect"
    buying_role: str = "unknown"
    signals_detected: str = "[]"
    risk_factors: str = "[]"
    opportunity_factors: str = "[]"
    intent_history: str = "[]"
    intent_trend: str = "{}"
    last_analysis_time: str = ""
    next_follow_up_time: str = ""
    last_completed_follow_up_signature: str = ""
    last_completed_follow_up_at: str = ""
    last_dingtalk_notification_signature: str = ""
    last_dingtalk_notification_at: str = ""
    last_dingtalk_notification_status: str = ""
    last_dingtalk_notification_error: str = ""
    last_dingtalk_notification_task_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    _KNOWN_KEYS = frozenset({
        "conversation_id", "platform", "customer_id", "customer_name", "customer_avatar",
        "status", "last_message_time", "last_message_content", "unread_count",
        "intent_level", "purchase_intent_score", "lead_score", "follow_up_priority",
        "purchase_probability", "estimated_deal_size", "lifecycle_stage", "buying_role",
        "signals_detected", "risk_factors", "opportunity_factors",
        "intent_history", "intent_trend",
        "last_analysis_time", "next_follow_up_time",
        "last_completed_follow_up_signature", "last_completed_follow_up_at",
        "last_dingtalk_notification_signature", "last_dingtalk_notification_at",
        "last_dingtalk_notification_status", "last_dingtalk_notification_error",
        "last_dingtalk_notification_task_id",
        "created_at", "updated_at",
    })

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ConversationSummaryRecord":
        payload = dict(data or {})
        return cls(
            conversation_id=str(payload.get("conversation_id", "") or ""),
            platform=str(payload.get("platform", "douyin") or "douyin"),
            customer_id=str(payload.get("customer_id", "") or ""),
            customer_name=str(payload.get("customer_name", "") or ""),
            customer_avatar=str(payload.get("customer_avatar", "") or ""),
            status=str(payload.get("status", "active") or "active"),
            last_message_time=str(payload.get("last_message_time", "") or ""),
            last_message_content=str(payload.get("last_message_content", "") or ""),
            unread_count=int(payload.get("unread_count", 0) or 0),
            intent_level=str(payload.get("intent_level", "") or ""),
            purchase_intent_score=float(payload.get("purchase_intent_score", 0) or 0),
            lead_score=str(payload.get("lead_score", "cold") or "cold"),
            follow_up_priority=str(payload.get("follow_up_priority", "low") or "low"),
            purchase_probability=float(payload.get("purchase_probability", 0) or 0),
            estimated_deal_size=float(payload.get("estimated_deal_size", 0) or 0),
            lifecycle_stage=str(payload.get("lifecycle_stage", "prospect") or "prospect"),
            buying_role=str(payload.get("buying_role", "unknown") or "unknown"),
            signals_detected=str(payload.get("signals_detected", "[]") or "[]"),
            risk_factors=str(payload.get("risk_factors", "[]") or "[]"),
            opportunity_factors=str(payload.get("opportunity_factors", "[]") or "[]"),
            intent_history=str(payload.get("intent_history", "[]") or "[]"),
            intent_trend=str(payload.get("intent_trend", "{}") or "{}"),
            last_analysis_time=str(payload.get("last_analysis_time", "") or ""),
            next_follow_up_time=str(payload.get("next_follow_up_time", "") or ""),
            last_completed_follow_up_signature=str(payload.get("last_completed_follow_up_signature", "") or ""),
            last_completed_follow_up_at=str(payload.get("last_completed_follow_up_at", "") or ""),
            last_dingtalk_notification_signature=str(payload.get("last_dingtalk_notification_signature", "") or ""),
            last_dingtalk_notification_at=str(payload.get("last_dingtalk_notification_at", "") or ""),
            last_dingtalk_notification_status=str(payload.get("last_dingtalk_notification_status", "") or ""),
            last_dingtalk_notification_error=str(payload.get("last_dingtalk_notification_error", "") or ""),
            last_dingtalk_notification_task_id=str(payload.get("last_dingtalk_notification_task_id", "") or ""),
            created_at=str(payload.get("created_at", "") or ""),
            updated_at=str(payload.get("updated_at", "") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "platform": self.platform,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "customer_avatar": self.customer_avatar,
            "status": self.status,
            "last_message_time": self.last_message_time,
            "last_message_content": self.last_message_content,
            "unread_count": self.unread_count,
            "intent_level": self.intent_level,
            "purchase_intent_score": self.purchase_intent_score,
            "lead_score": self.lead_score,
            "follow_up_priority": self.follow_up_priority,
            "purchase_probability": self.purchase_probability,
            "estimated_deal_size": self.estimated_deal_size,
            "lifecycle_stage": self.lifecycle_stage,
            "buying_role": self.buying_role,
            "signals_detected": self.signals_detected,
            "risk_factors": self.risk_factors,
            "opportunity_factors": self.opportunity_factors,
            "intent_history": self.intent_history,
            "intent_trend": self.intent_trend,
            "last_analysis_time": self.last_analysis_time,
            "next_follow_up_time": self.next_follow_up_time,
            "last_completed_follow_up_signature": self.last_completed_follow_up_signature,
            "last_completed_follow_up_at": self.last_completed_follow_up_at,
            "last_dingtalk_notification_signature": self.last_dingtalk_notification_signature,
            "last_dingtalk_notification_at": self.last_dingtalk_notification_at,
            "last_dingtalk_notification_status": self.last_dingtalk_notification_status,
            "last_dingtalk_notification_error": self.last_dingtalk_notification_error,
            "last_dingtalk_notification_task_id": self.last_dingtalk_notification_task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ChatStoreFacade:
    """统一聊天访问层的第一阶段骨架。

    当前默认仍委托给 `DatabaseManager` 的 JSON 主链；后续可把内部实现切到 SQLite，
    并保持上层模块调用契约不变。
    """

    def __init__(self, database: Optional[DatabaseManager] = None):
        self.database = database or DatabaseManager()

    @staticmethod
    def _prefer_sqlite_reads() -> bool:
        return os.getenv("HUOKE_MESSAGE_STORE_READ_SOURCE", "json").strip().lower() == "sqlite"

    @staticmethod
    def _call_get_conversation_messages(getter: Any, conversation_id: str, limit: int) -> List[Dict[str, Any]]:
        try:
            return getter(conversation_id, limit=limit) or []
        except TypeError:
            return getter(conversation_id) or []

    @staticmethod
    def _call_get_all_messages(getter: Any, limit: Optional[int]) -> List[Dict[str, Any]]:
        try:
            return getter(limit=limit) or []
        except TypeError:
            return getter() or []

    def get_recent_messages(self, conversation_id: str, limit: int = 20) -> List[MessageRecord]:
        if self._prefer_sqlite_reads() and hasattr(self.database, "get_conversation_messages_from_sqlite"):
            messages = self.database.get_conversation_messages_from_sqlite(conversation_id, limit=limit)
        else:
            get_conversation_messages = getattr(self.database, "get_conversation_messages", None)
            messages = (
                self._call_get_conversation_messages(get_conversation_messages, conversation_id, limit)
                if callable(get_conversation_messages)
                else []
            )
        return [MessageRecord.from_dict(message) for message in messages]

    def get_recent_messages_dicts(self, conversation_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        if self._prefer_sqlite_reads() and hasattr(self.database, "get_conversation_messages_from_sqlite"):
            messages = self.database.get_conversation_messages_from_sqlite(conversation_id, limit=limit) or []
        else:
            get_conversation_messages = getattr(self.database, "get_conversation_messages", None)
            messages = (
                self._call_get_conversation_messages(get_conversation_messages, conversation_id, limit)
                if callable(get_conversation_messages)
                else []
            )
        return [dict(message) for message in messages]

    def get_all_messages_dicts(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if self._prefer_sqlite_reads() and hasattr(self.database, "get_all_messages_from_sqlite"):
            messages = self.database.get_all_messages_from_sqlite(limit=limit)
            return [dict(message) for message in messages]

        get_all_messages = getattr(self.database, "get_all_messages", None)
        if callable(get_all_messages):
            messages = self._call_get_all_messages(get_all_messages, limit)
            return [dict(message) for message in (messages or [])]

        load_messages_data = getattr(self.database, "_load_messages_data", None)
        if callable(load_messages_data):
            data = load_messages_data() or {}
            messages = list(data.get("messages", []))
            messages.sort(key=lambda item: item.get("created_at", ""), reverse=True)
            if limit:
                messages = messages[:limit]
            return [dict(message) for message in messages]
        return []

    def get_conversation_summary(self, conversation_id: str) -> Optional[ConversationSummaryRecord]:
        if self._prefer_sqlite_reads() and hasattr(self.database, "get_conversation_by_id_from_sqlite"):
            conversation = self.database.get_conversation_by_id_from_sqlite(conversation_id)
            if conversation:
                return ConversationSummaryRecord.from_dict(conversation)
        if self._prefer_sqlite_reads() and hasattr(self.database, "get_all_conversations_from_sqlite"):
            conversations = self.database.get_all_conversations_from_sqlite()
        else:
            get_all_conversations = getattr(self.database, "get_all_conversations", None)
            conversations = get_all_conversations() if callable(get_all_conversations) else []
        for conversation in conversations:
            if str(conversation.get("conversation_id", "") or "") == str(conversation_id or ""):
                return ConversationSummaryRecord.from_dict(conversation)
        return None

    def get_conversation_summary_dict(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        if self._prefer_sqlite_reads() and hasattr(self.database, "get_conversation_by_id_from_sqlite"):
            conversation = self.database.get_conversation_by_id_from_sqlite(conversation_id)
            if conversation:
                return dict(conversation)
        if self._prefer_sqlite_reads() and hasattr(self.database, "get_all_conversations_from_sqlite"):
            conversations = self.database.get_all_conversations_from_sqlite()
        else:
            get_all_conversations = getattr(self.database, "get_all_conversations", None)
            conversations = get_all_conversations() if callable(get_all_conversations) else []
        for conversation in conversations:
            if str(conversation.get("conversation_id", "") or "") == str(conversation_id or ""):
                return dict(conversation)
        return None

    def get_all_conversations_dicts(self) -> List[Dict[str, Any]]:
        if self._prefer_sqlite_reads() and hasattr(self.database, "get_all_conversations_from_sqlite"):
            conversations = self.database.get_all_conversations_from_sqlite()
        else:
            get_all_conversations = getattr(self.database, "get_all_conversations", None)
            conversations = get_all_conversations() if callable(get_all_conversations) else []
        return [dict(conversation) for conversation in (conversations or [])]

    def save_inbound_message(self, message: MessageRecord) -> None:
        self.database.save_message(message.to_dict())

    def save_outbound_message(self, message: MessageRecord) -> None:
        self.database.save_message(message.to_dict())

    def upsert_conversation_summary(self, summary: ConversationSummaryRecord) -> None:
        self.database.save_conversation(summary.to_dict())
