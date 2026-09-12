from __future__ import annotations

import re
import threading
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List

from src.common.monitoring import get_metrics


def _strip_template_prefix(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    stripped = re.sub(r"^.{1,10}您好[！!～~]", "", normalized).strip()
    return stripped if stripped else normalized


class OutboundRecentReplyStore:
    """按会话维护最近回复窗口，避免与出站幂等缓存混用。"""

    def __init__(
        self,
        *,
        max_conversations: int = 500,
        max_entries_per_conversation: int = 20,
        entry_ttl: int = 3600,
        duplicate_window_seconds: int = 300,
    ):
        self._entries: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._max_conversations = max_conversations
        self._max_entries_per_conversation = max_entries_per_conversation
        self._entry_ttl = entry_ttl
        self._duplicate_window_seconds = duplicate_window_seconds

    @staticmethod
    def _build_key(*, conversation_id: str, platform: str = "douyin") -> str:
        normalized_platform = str(platform or "douyin").strip() or "douyin"
        normalized_conversation_id = str(conversation_id or "").strip()
        if not normalized_conversation_id:
            return ""
        return f"recent_reply:{normalized_platform}:{normalized_conversation_id}"

    def record_reply(
        self,
        *,
        conversation_id: str,
        content: str,
        logical_message_id: str = "",
        trace_id: str = "",
        platform: str = "douyin",
        customer_name: str = "",
    ) -> None:
        key = self._build_key(conversation_id=conversation_id, platform=platform)
        normalized_content = str(content or "").strip()
        if not key or not normalized_content:
            return

        now = time.time()
        entry = {
            "content": normalized_content,
            "content_hash": hash(normalized_content),
            "logical_message_id": str(logical_message_id or "").strip(),
            "trace_id": str(trace_id or "").strip(),
            "time": now,
            "customer_name": str(customer_name or "").strip(),
        }
        with self._lock:
            conversation_entries = list(self._entries.get(key, []))
            conversation_entries.append(entry)
            conversation_entries = [
                item
                for item in conversation_entries
                if isinstance(item, dict) and now - float(item.get("time", 0) or 0) <= self._entry_ttl
            ]
            if len(conversation_entries) > self._max_entries_per_conversation:
                conversation_entries = conversation_entries[-self._max_entries_per_conversation :]
            self._entries[key] = conversation_entries
            self._evict_if_needed(now)
            self._update_metrics_locked()

    def is_recent_duplicate(
        self,
        *,
        conversation_id: str,
        content: str,
        platform: str = "douyin",
        window_seconds: int | None = None,
    ) -> bool:
        key = self._build_key(conversation_id=conversation_id, platform=platform)
        normalized_content = str(content or "").strip()
        if not key or not normalized_content:
            return False

        now = time.time()
        window = int(window_seconds or self._duplicate_window_seconds)
        candidate_core = _strip_template_prefix(normalized_content)
        with self._lock:
            entries = list(self._entries.get(key, []))
            matched = False
            filtered_entries: List[Dict[str, Any]] = []
            for item in entries:
                if not isinstance(item, dict):
                    continue
                item_time = float(item.get("time", 0) or 0)
                if now - item_time > self._entry_ttl:
                    continue
                filtered_entries.append(item)
                if now - item_time > window:
                    continue
                sent_content = str(item.get("content", "") or "").strip()
                if not sent_content:
                    continue
                sent_core = _strip_template_prefix(sent_content)
                if candidate_core == sent_core:
                    matched = True
                    break
                min_len = min(len(candidate_core), len(sent_core))
                if min_len > 10:
                    similarity = SequenceMatcher(None, sent_core[:150], candidate_core[:150]).ratio()
                    if similarity > 0.97:
                        matched = True
                        break
            if filtered_entries:
                self._entries[key] = filtered_entries[-self._max_entries_per_conversation :]
            elif key in self._entries:
                del self._entries[key]
            self._update_metrics_locked()

        if matched:
            get_metrics().record_outbound_idempotency_hit("recent_duplicate_reply")
        return matched

    def get_snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        with self._lock:
            return {key: [dict(item) for item in value] for key, value in self._entries.items()}

    def restore_snapshot(self, snapshot: Dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            return

        now = time.time()
        validated: Dict[str, List[Dict[str, Any]]] = {}
        for key, value in snapshot.items():
            normalized_key = str(key or "").strip()
            if normalized_key.startswith("reply:"):
                conversation_id = normalized_key.split(":", 1)[1].strip()
                normalized_key = self._build_key(conversation_id=conversation_id, platform="douyin")
            if not normalized_key.startswith("recent_reply:"):
                continue
            if not isinstance(value, list):
                continue
            restored_items: List[Dict[str, Any]] = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                ts = float(item.get("time", 0) or 0)
                if now - ts > self._entry_ttl:
                    continue
                restored_items.append(dict(item))
            if restored_items:
                validated[normalized_key] = restored_items[-self._max_entries_per_conversation :]

        with self._lock:
            self._entries.update(validated)
            self._evict_if_needed(now)
            self._update_metrics_locked()

    def _evict_if_needed(self, now: float) -> None:
        expired_keys = []
        for key, value in self._entries.items():
            fresh_items = [
                item
                for item in value
                if isinstance(item, dict) and now - float(item.get("time", 0) or 0) <= self._entry_ttl
            ]
            if fresh_items:
                self._entries[key] = fresh_items[-self._max_entries_per_conversation :]
            else:
                expired_keys.append(key)
        for key in expired_keys:
            self._entries.pop(key, None)

        if len(self._entries) <= self._max_conversations:
            return

        sorted_items = sorted(
            self._entries.items(),
            key=lambda item: item[1][-1].get("time", 0) if item[1] else 0,
        )
        remove_count = len(self._entries) - self._max_conversations
        for key, _ in sorted_items[:remove_count]:
            self._entries.pop(key, None)

    def _update_metrics_locked(self) -> None:
        metrics = get_metrics()
        entry_count = sum(len(items) for items in self._entries.values())
        metrics.update_outbound_recent_reply_store(
            conversations=len(self._entries),
            entries=entry_count,
        )
