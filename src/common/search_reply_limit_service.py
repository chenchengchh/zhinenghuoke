from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import warnings
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

from src.infrastructure.runtime_paths import get_data_dir

if os.name == "nt":
    import msvcrt
else:
    import fcntl


DEFAULT_SEARCH_REPLY_LIMIT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "rolling_10m_limit": 20,
    "rolling_1h_limit": 60,
    "rolling_24h_limit": 80,
    "event_retention_days": 3,
    "updated_at": "",
}

DEFAULT_SEARCH_REPLY_LIMIT_STATE: Dict[str, Any] = {
    "config": deepcopy(DEFAULT_SEARCH_REPLY_LIMIT_CONFIG),
    "events": [],
}


@dataclass
class SearchReplyLimitDecision:
    allowed: bool
    message: str
    status_code: str
    details: Dict[str, Any]


class SearchReplyLimitService:
    """评论下直接回复频控服务。

    .. deprecated::
        请使用 :class:`src.common.reply_management_service.ReplyManagementService` 替代。
    """
    FILE_LOCK_TIMEOUT_SECONDS = 10.0
    FILE_LOCK_RETRY_INTERVAL_SECONDS = 0.05

    def __init__(self, state_path: Path | None = None) -> None:
        warnings.warn(
            "SearchReplyLimitService 已弃用，请使用 ReplyManagementService",
            DeprecationWarning,
            stacklevel=2,
        )
        self._state_path = state_path or get_data_dir() / "search_reply_limit.json"
        self._lock_path = Path(f"{self._state_path}.lock")
        self._lock = threading.RLock()

    @staticmethod
    def _normalize_account_id(account_id: str = "") -> str:
        return str(account_id or "").strip()

    def _filter_items_by_account_id(self, items: List[Dict[str, Any]], account_id: str = "") -> List[Dict[str, Any]]:
        normalized_account_id = self._normalize_account_id(account_id)
        if not normalized_account_id:
            return list(items or [])
        return [
            item for item in (items or [])
            if self._normalize_account_id(item.get("account_id") or "") == normalized_account_id
        ]

    def get_status(self, now: datetime | None = None, *, account_id: str = "") -> Dict[str, Any]:
        with self._locked_state_context(current_time=now) as (state, current_time):
            return self._build_status_from_state(state, current_time=current_time, account_id=account_id)

    def load_config(self) -> Dict[str, Any]:
        with self._locked_state_context() as (state, _current_time):
            return deepcopy(state["config"])

    def save_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._locked_state_context() as (state, current_time):
            merged = self._merge_config(state["config"], payload or {})
            merged["updated_at"] = current_time.isoformat()
            state["config"] = merged
            self._persist_state_unlocked(state, current_time=current_time)
            return deepcopy(merged)

    def export_api_doc(self) -> Dict[str, Any]:
        return {
            "module": "search_reply_limit",
            "description": "评论下直接回复频控配置接口",
            "endpoints": [
                {
                    "method": "GET",
                    "path": "/api/search-reply-limit/config",
                    "description": "获取评论下直接回复频控配置与实时状态",
                },
                {
                    "method": "POST",
                    "path": "/api/search-reply-limit/config",
                    "description": "保存评论下直接回复频控配置",
                },
                {
                    "method": "GET",
                    "path": "/api/search-reply-limit/status",
                    "description": "获取评论下直接回复频控实时状态",
                },
            ],
        }

    def check_send_allowed(self, now: datetime | None = None, *, account_id: str = "") -> SearchReplyLimitDecision:
        current_time = now or datetime.now()
        with self._locked_state_context(current_time=current_time) as (state, locked_time):
            status = self._build_status_from_state(state, current_time=locked_time, account_id=account_id)

        config = status["config"]
        if not status["enabled"]:
            return SearchReplyLimitDecision(
                allowed=True,
                message="评论下直接回复频控已关闭，允许发送。",
                status_code="disabled",
                details=status,
            )

        limit_24h = int(config.get("rolling_24h_limit", 80) or 80)
        count_24h = int(status.get("rolling_24h_count", 0) or 0)
        if count_24h >= limit_24h:
            retry_after_seconds = max(int(status.get("retry_after_24h_seconds", 0) or 0), 0)
            return SearchReplyLimitDecision(
                allowed=False,
                message=(
                    f"评论下直接回复已触发 24 小时限制：过去 24 小时已发送 {count_24h} 条，"
                    f"当前上限 {limit_24h} 条。"
                ),
                status_code="rolling_24h_limit_exceeded",
                details={**status, "retry_after_seconds": retry_after_seconds},
            )

        limit_1h = int(config.get("rolling_1h_limit", 60) or 60)
        count_1h = int(status.get("rolling_1h_count", 0) or 0)
        if count_1h >= limit_1h:
            retry_after_seconds = max(int(status.get("retry_after_1h_seconds", 0) or 0), 0)
            return SearchReplyLimitDecision(
                allowed=False,
                message=(
                    f"评论下直接回复已触发 1 小时限制：最近 1 小时已发送 {count_1h} 条，"
                    f"当前上限 {limit_1h} 条。"
                ),
                status_code="rolling_1h_limit_exceeded",
                details={**status, "retry_after_seconds": retry_after_seconds},
            )

        limit_10m = int(config.get("rolling_10m_limit", 20) or 20)
        count_10m = int(status.get("rolling_10m_count", 0) or 0)
        if count_10m >= limit_10m:
            retry_after_seconds = max(int(status.get("retry_after_10m_seconds", 0) or 0), 0)
            return SearchReplyLimitDecision(
                allowed=False,
                message=(
                    f"评论下直接回复已触发 10 分钟限制：最近 10 分钟已发送 {count_10m} 条，"
                    f"当前上限 {limit_10m} 条。"
                ),
                status_code="rolling_10m_limit_exceeded",
                details={**status, "retry_after_seconds": retry_after_seconds},
            )

        return SearchReplyLimitDecision(
            allowed=True,
            message="评论下直接回复频控校验通过。",
            status_code="allowed",
            details=status,
        )

    def record_reply_event(
        self,
        *,
        target_id: str = "",
        reply_text: str = "",
        account_id: str = "",
        source: str = "search_original_comment_reply",
        now: datetime | None = None,
    ) -> Dict[str, Any]:
        current_time = now or datetime.now()
        normalized_account_id = self._normalize_account_id(account_id)
        with self._locked_state_context(current_time=current_time) as (state, locked_time):
            state["events"].append(
                {
                    "timestamp": locked_time.isoformat(),
                    "target_id": str(target_id or ""),
                    "account_id": normalized_account_id,
                    "source": str(source or "search_original_comment_reply"),
                    "reply_preview": str(reply_text or "")[:80],
                }
            )
            self._persist_state_unlocked(state, current_time=locked_time)
            return self._build_status_from_state(state, current_time=locked_time, account_id=normalized_account_id)

    def _load_state_unlocked(self, current_time: datetime | None = None) -> Dict[str, Any]:
        if not self._state_path.exists():
            initial = deepcopy(DEFAULT_SEARCH_REPLY_LIMIT_STATE)
            self._persist_state_unlocked(initial, current_time=current_time or datetime.now())
            return initial

        try:
            content = self._state_path.read_text(encoding="utf-8").strip()
            if not content:
                return self._normalize_state(deepcopy(DEFAULT_SEARCH_REPLY_LIMIT_STATE), current_time=current_time)
            loaded = json.loads(content)
            if not isinstance(loaded, dict):
                return self._normalize_state(deepcopy(DEFAULT_SEARCH_REPLY_LIMIT_STATE), current_time=current_time)
            return self._normalize_state(loaded, current_time=current_time)
        except Exception:
            return self._normalize_state(deepcopy(DEFAULT_SEARCH_REPLY_LIMIT_STATE), current_time=current_time)

    def _normalize_state(self, state: Dict[str, Any], current_time: datetime | None = None) -> Dict[str, Any]:
        normalized_time = current_time or datetime.now()
        config = self._merge_config(
            DEFAULT_SEARCH_REPLY_LIMIT_CONFIG,
            state.get("config", {}) if isinstance(state, dict) else {},
        )
        events = state.get("events", []) if isinstance(state, dict) else []
        if not isinstance(events, list):
            events = []
        normalized_events = self._normalize_events(events, config, current_time=normalized_time)
        return {"config": config, "events": normalized_events}

    def _persist_state_unlocked(self, state: Dict[str, Any], current_time: datetime | None = None) -> None:
        normalized = self._normalize_state(state, current_time=current_time)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(normalized, ensure_ascii=False, indent=2)
        fd, temp_path = tempfile.mkstemp(suffix=".tmp", dir=str(self._state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._state_path)
        except Exception:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except Exception:
                pass
            raise

    @contextmanager
    def _locked_state_context(
        self,
        current_time: datetime | None = None,
    ) -> Iterator[Tuple[Dict[str, Any], datetime]]:
        normalized_time = current_time or datetime.now()
        with self._lock:
            with self._acquire_process_file_lock():
                state = self._load_state_unlocked(current_time=normalized_time)
                normalized = self._normalize_state(state, current_time=normalized_time)
                if normalized != state:
                    self._persist_state_unlocked(normalized, current_time=normalized_time)
                yield normalized, normalized_time

    @contextmanager
    def _acquire_process_file_lock(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            self._lock_file_handle(lock_file)
            try:
                yield
            finally:
                self._unlock_file_handle(lock_file)

    def _lock_file_handle(self, lock_file) -> None:
        deadline = time.monotonic() + self.FILE_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                lock_file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("获取 search_reply_limit 文件锁超时")
                time.sleep(self.FILE_LOCK_RETRY_INTERVAL_SECONDS)

    @staticmethod
    def _unlock_file_handle(lock_file) -> None:
        lock_file.seek(0)
        if os.name == "nt":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _merge_config(self, base: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        merged = deepcopy(base)
        if not isinstance(payload, dict):
            payload = {}
        if "enabled" in payload:
            merged["enabled"] = bool(payload["enabled"])
        for key in {"rolling_10m_limit", "rolling_1h_limit", "rolling_24h_limit", "event_retention_days"}:
            if key not in payload:
                continue
            try:
                merged[key] = max(int(payload[key]), 1)
            except Exception:
                continue
        if "updated_at" in payload:
            merged["updated_at"] = str(payload.get("updated_at") or "")
        return merged

    def _normalize_events(
        self,
        events: List[Dict[str, Any]],
        config: Dict[str, Any],
        current_time: datetime | None = None,
    ) -> List[Dict[str, Any]]:
        normalized_time = current_time or datetime.now()
        retention_cutoff = normalized_time - timedelta(days=int(config.get("event_retention_days", 3) or 3))
        normalized_events: List[Dict[str, Any]] = []
        for raw_event in events:
            if not isinstance(raw_event, dict):
                continue
            parsed = self._parse_datetime(str(raw_event.get("timestamp") or "").strip())
            if not parsed or parsed < retention_cutoff:
                continue
            normalized_events.append(
                {
                    "timestamp": parsed.isoformat(),
                    "target_id": str(raw_event.get("target_id") or ""),
                    "account_id": self._normalize_account_id(raw_event.get("account_id") or ""),
                    "source": str(raw_event.get("source") or "search_original_comment_reply"),
                    "reply_preview": str(raw_event.get("reply_preview") or "")[:80],
                }
            )
        normalized_events.sort(key=lambda item: item["timestamp"])
        return normalized_events

    def _build_status_from_state(
        self,
        state: Dict[str, Any],
        *,
        current_time: datetime,
        account_id: str = "",
    ) -> Dict[str, Any]:
        config = deepcopy(state.get("config") or DEFAULT_SEARCH_REPLY_LIMIT_CONFIG)
        events = self._filter_items_by_account_id(list(state.get("events") or []), account_id)
        window_10m_start = current_time - timedelta(minutes=10)
        window_1h_start = current_time - timedelta(hours=1)
        window_24h_start = current_time - timedelta(hours=24)
        events_10m = self._events_since(events, window_10m_start)
        events_1h = self._events_since(events, window_1h_start)
        events_24h = self._events_since(events, window_24h_start)

        retry_after_10m_seconds, next_10m_slot_at = self._build_retry_state(events_10m, current_time, minutes=10)
        retry_after_1h_seconds, next_1h_slot_at = self._build_retry_state(events_1h, current_time, hours=1)
        retry_after_24h_seconds, next_24h_slot_at = self._build_retry_state(events_24h, current_time, hours=24)

        limit_10m = int(config.get("rolling_10m_limit", 20) or 20)
        limit_1h = int(config.get("rolling_1h_limit", 60) or 60)
        limit_24h = int(config.get("rolling_24h_limit", 80) or 80)
        blocked_window = ""
        if len(events_24h) >= limit_24h:
            blocked_window = "24h"
        elif len(events_1h) >= limit_1h:
            blocked_window = "1h"
        elif len(events_10m) >= limit_10m:
            blocked_window = "10m"

        return {
            "enabled": bool(config.get("enabled", True)),
            "config": config,
            "last_event_at": events[-1]["timestamp"] if events else "",
            "rolling_10m_count": len(events_10m),
            "rolling_1h_count": len(events_1h),
            "rolling_24h_count": len(events_24h),
            "remaining_10m_quota": max(limit_10m - len(events_10m), 0),
            "remaining_1h_quota": max(limit_1h - len(events_1h), 0),
            "remaining_24h_quota": max(limit_24h - len(events_24h), 0),
            "next_10m_slot_at": next_10m_slot_at,
            "next_1h_slot_at": next_1h_slot_at,
            "next_24h_slot_at": next_24h_slot_at,
            "retry_after_10m_seconds": retry_after_10m_seconds,
            "retry_after_1h_seconds": retry_after_1h_seconds,
            "retry_after_24h_seconds": retry_after_24h_seconds,
            "blocked_window": blocked_window,
            "toggle_forced_off": bool(config.get("enabled", True) and blocked_window),
            "account_scope": {"account_id": self._normalize_account_id(account_id)},
        }

    def _build_retry_state(
        self,
        events: List[Dict[str, Any]],
        current_time: datetime,
        *,
        minutes: int = 0,
        hours: int = 0,
    ) -> Tuple[int, str]:
        if not events:
            return 0, ""
        oldest_event = self._parse_datetime(events[0]["timestamp"])
        if not oldest_event:
            return 0, ""
        next_time = oldest_event + timedelta(minutes=minutes, hours=hours)
        if next_time <= current_time:
            return 0, ""
        return max(int((next_time - current_time).total_seconds() + 0.999), 0), next_time.isoformat()

    @staticmethod
    def _events_since(events: List[Dict[str, Any]], cutoff: datetime) -> List[Dict[str, Any]]:
        filtered: List[Dict[str, Any]] = []
        for item in events:
            parsed = SearchReplyLimitService._parse_datetime(str(item.get("timestamp") or "").strip())
            if parsed and parsed >= cutoff:
                filtered.append(item)
        return filtered

    @staticmethod
    def _parse_datetime(raw_value: str) -> datetime | None:
        text = str(raw_value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None


_search_reply_limit_service: SearchReplyLimitService | None = None


def get_search_reply_limit_service() -> SearchReplyLimitService:
    global _search_reply_limit_service
    if _search_reply_limit_service is None:
        _search_reply_limit_service = SearchReplyLimitService()
    return _search_reply_limit_service
