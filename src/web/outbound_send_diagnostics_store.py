from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, Optional


class OutboundSendDiagnosticsStore:
    """保存最近发送链诊断快照，避免并发场景下只剩“最后一次状态”覆盖。"""

    def __init__(self, *, max_entries: int = 100):
        self._lock = threading.RLock()
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self._order: Deque[str] = deque()
        self._max_entries = max(int(max_entries or 0), 10)
        self._latest_trace_id = ""

    def begin_attempt(
        self,
        *,
        customer_name: str,
        content: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
        trace_id: str = "",
        outbound_source: str = "",
        outbound_trigger: str = "",
    ) -> str:
        normalized_trace_id = str(trace_id or "").strip() or uuid.uuid4().hex[:12]
        snapshot = {
            "trace_id": normalized_trace_id,
            "customer_name": str(customer_name or "").strip(),
            "content_preview": str(content or "")[:120],
            "conversation_id": str(conversation_id or "").strip(),
            "customer_id": str(customer_id or "").strip(),
            "platform": str(platform or "douyin").strip() or "douyin",
            "outbound_source": str(outbound_source or "").strip(),
            "outbound_trigger": str(outbound_trigger or "").strip(),
            "resolution_level": "conversation"
            if str(conversation_id or "").strip()
            else ("customer" if str(customer_id or "").strip() else "name"),
            "status": "started",
            "channel": "",
            "reason": "",
            "last_failure_reason": "",
            "last_send_error": "",
            "target_meta": {},
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with self._lock:
            if normalized_trace_id not in self._snapshots:
                self._order.append(normalized_trace_id)
            self._snapshots[normalized_trace_id] = snapshot
            self._latest_trace_id = normalized_trace_id
            self._evict_if_needed_locked()
        return normalized_trace_id

    def update_target_meta(self, trace_id: str, target_meta: Dict[str, Any]) -> None:
        if not trace_id:
            return
        with self._lock:
            snapshot = self._snapshots.get(trace_id)
            if not snapshot:
                return
            snapshot["target_meta"] = dict(target_meta or {})
            snapshot["updated_at"] = time.time()
            if not snapshot.get("conversation_id"):
                snapshot["conversation_id"] = str((target_meta or {}).get("conversation_id", "") or "").strip()
            if not snapshot.get("customer_id"):
                snapshot["customer_id"] = str((target_meta or {}).get("customer_id", "") or "").strip()
            if not snapshot.get("platform"):
                snapshot["platform"] = str((target_meta or {}).get("platform", "douyin") or "douyin").strip() or "douyin"

    def record_failure(self, trace_id: str, reason: str, *, channel: str = "", send_error: str = "") -> None:
        if not trace_id:
            return
        normalized_reason = str(reason or "").strip()[:200]
        with self._lock:
            snapshot = self._snapshots.get(trace_id)
            if not snapshot:
                return
            snapshot["status"] = "failed"
            snapshot["reason"] = normalized_reason
            snapshot["last_failure_reason"] = normalized_reason
            if channel:
                snapshot["channel"] = str(channel or "").strip()[:40]
            if send_error:
                snapshot["last_send_error"] = str(send_error or "").strip()[:200]
            snapshot["updated_at"] = time.time()

    def record_success(self, trace_id: str, *, channel: str, reason: str = "") -> None:
        if not trace_id:
            return
        with self._lock:
            snapshot = self._snapshots.get(trace_id)
            if not snapshot:
                return
            snapshot["status"] = "sent"
            snapshot["channel"] = str(channel or "").strip()[:40]
            snapshot["reason"] = str(reason or "").strip()[:200]
            snapshot["updated_at"] = time.time()

    def record_dispatch(self, trace_id: str, *, queue_status: str, conversation_key: str = "", task_id: str = "") -> None:
        if not trace_id:
            return
        with self._lock:
            snapshot = self._snapshots.get(trace_id)
            if not snapshot:
                return
            snapshot["dispatch_status"] = str(queue_status or "").strip()[:40]
            if conversation_key:
                snapshot["dispatch_conversation_key"] = str(conversation_key or "").strip()[:160]
            if task_id:
                snapshot["dispatch_task_id"] = str(task_id or "").strip()[:80]
            snapshot["updated_at"] = time.time()

    def get_snapshot(self, trace_id: str = "") -> Dict[str, Any]:
        with self._lock:
            selected_trace_id = str(trace_id or "").strip() or self._latest_trace_id
            snapshot = self._snapshots.get(selected_trace_id, {})
            return dict(snapshot) if isinstance(snapshot, dict) else {}

    def list_recent(self, limit: int = 20) -> list[Dict[str, Any]]:
        with self._lock:
            ordered_trace_ids = list(self._order)[-max(int(limit or 0), 0):]
            return [
                dict(self._snapshots[trace_id])
                for trace_id in reversed(ordered_trace_ids)
                if trace_id in self._snapshots
            ]

    def _evict_if_needed_locked(self) -> None:
        while len(self._order) > self._max_entries:
            oldest_trace_id = self._order.popleft()
            self._snapshots.pop(oldest_trace_id, None)
