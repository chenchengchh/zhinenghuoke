from __future__ import annotations

import json
import os
import tempfile
import threading
import time
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


DEFAULT_INTERACT_COMMENT_LIMIT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "min_interval_seconds": 15,
    "rolling_10m_limit": 10,
    "rolling_24h_limit": 100,
    "event_retention_days": 3,
    "updated_at": "",
}

DEFAULT_INTERACT_COMMENT_LIMIT_STATE: Dict[str, Any] = {
    "config": deepcopy(DEFAULT_INTERACT_COMMENT_LIMIT_CONFIG),
    "events": [],
}


@dataclass
class InteractCommentLimitDecision:
    allowed: bool
    message: str
    status_code: str
    details: Dict[str, Any]


class InteractCommentLimitService:
    FILE_LOCK_TIMEOUT_SECONDS = 10.0
    FILE_LOCK_RETRY_INTERVAL_SECONDS = 0.05

    def __init__(self, state_path: Path | None = None) -> None:
        self._state_path = state_path or get_data_dir() / "interact_comment_limit.json"
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
            "module": "interact_comment_limit",
            "description": "一键互动评论频控配置接口",
            "endpoints": [
                {
                    "method": "GET",
                    "path": "/api/interact-limit/config",
                    "description": "获取一键互动频控配置与实时状态",
                },
                {
                    "method": "POST",
                    "path": "/api/interact-limit/config",
                    "description": "保存一键互动频控配置",
                },
                {
                    "method": "GET",
                    "path": "/api/interact-limit/status",
                    "description": "获取一键互动频控实时状态",
                },
            ],
        }

    def check_send_allowed(self, now: datetime | None = None, *, account_id: str = "") -> InteractCommentLimitDecision:
        current_time = now or datetime.now()
        with self._locked_state_context(current_time=current_time) as (state, locked_time):
            status = self._build_status_from_state(state, current_time=locked_time, account_id=account_id)

        config = status["config"]
        if not status["enabled"]:
            return InteractCommentLimitDecision(
                allowed=True,
                message="一键互动评论频控已关闭，允许发送。",
                status_code="disabled",
                details=status,
            )

        daily_limit = int(config.get("rolling_24h_limit", 100) or 100)
        rolling_daily_count = int(status.get("rolling_24h_count", 0) or 0)
        if rolling_daily_count >= daily_limit:
            retry_after_seconds = max(int(status.get("retry_after_daily_seconds", 0) or 0), 0)
            remaining = max(daily_limit - rolling_daily_count, 0)
            return InteractCommentLimitDecision(
                allowed=False,
                message=(
                    f"一键互动评论已触发 24 小时限制：过去 24 小时已发送 {rolling_daily_count} 条，"
                    f"当前上限 {daily_limit} 条，剩余可发送 {remaining} 条。"
                ),
                status_code="daily_limit_exceeded",
                details={
                    **status,
                    "remaining_quota": remaining,
                    "retry_after_seconds": retry_after_seconds,
                },
            )

        ten_min_limit = int(config.get("rolling_10m_limit", 10) or 10)
        rolling_10m_count = int(status.get("rolling_10m_count", 0) or 0)
        if rolling_10m_count >= ten_min_limit:
            retry_after_seconds = max(int(status.get("retry_after_10m_seconds", 0) or 0), 0)
            return InteractCommentLimitDecision(
                allowed=False,
                message=(
                    f"一键互动评论已触发 10 分钟频控：最近 10 分钟已发送 {rolling_10m_count} 条，"
                    f"当前上限 {ten_min_limit} 条。"
                ),
                status_code="rolling_10m_limit_exceeded",
                details={
                    **status,
                    "retry_after_seconds": retry_after_seconds,
                },
            )

        min_interval_seconds = int(config.get("min_interval_seconds", 15) or 15)
        seconds_since_last = float(status.get("seconds_since_last_comment") or 0.0)
        if status.get("last_event_at") and seconds_since_last < min_interval_seconds:
            retry_after_seconds = max(int(status.get("retry_after_interval_seconds", 0) or 0), 0)
            return InteractCommentLimitDecision(
                allowed=False,
                message=(
                    f"一键互动评论发送间隔不足 {min_interval_seconds} 秒，"
                    f"请等待 {retry_after_seconds} 秒后继续。"
                ),
                status_code="min_interval_not_elapsed",
                details={
                    **status,
                    "retry_after_seconds": retry_after_seconds,
                },
            )

        return InteractCommentLimitDecision(
            allowed=True,
            message="一键互动评论频控校验通过。",
            status_code="allowed",
            details=status,
        )

    def record_comment_event(
        self,
        *,
        user_id: str = "",
        comment: str = "",
        account_id: str = "",
        source: str = "one_click_interact",
        now: datetime | None = None,
    ) -> Dict[str, Any]:
        current_time = now or datetime.now()
        normalized_account_id = self._normalize_account_id(account_id)
        with self._locked_state_context(current_time=current_time) as (state, locked_time):
            state["events"].append(
                {
                    "timestamp": locked_time.isoformat(),
                    "user_id": str(user_id or ""),
                    "account_id": normalized_account_id,
                    "source": str(source or "one_click_interact"),
                    "comment_preview": str(comment or "")[:80],
                }
            )
            self._persist_state_unlocked(state, current_time=locked_time)
            return self._build_status_from_state(state, current_time=locked_time, account_id=normalized_account_id)

    def _load_state_unlocked(self, current_time: datetime | None = None) -> Dict[str, Any]:
        if not self._state_path.exists():
            initial = deepcopy(DEFAULT_INTERACT_COMMENT_LIMIT_STATE)
            self._persist_state_unlocked(initial, current_time=current_time or datetime.now())
            return initial

        try:
            content = self._state_path.read_text(encoding="utf-8").strip()
            if not content:
                return self._normalize_state(
                    deepcopy(DEFAULT_INTERACT_COMMENT_LIMIT_STATE),
                    current_time=current_time,
                )
            loaded = json.loads(content)
            if not isinstance(loaded, dict):
                return self._normalize_state(
                    deepcopy(DEFAULT_INTERACT_COMMENT_LIMIT_STATE),
                    current_time=current_time,
                )
            return self._normalize_state(loaded, current_time=current_time)
        except Exception:
            return self._normalize_state(
                deepcopy(DEFAULT_INTERACT_COMMENT_LIMIT_STATE),
                current_time=current_time,
            )

    def _normalize_state(
        self,
        state: Dict[str, Any],
        current_time: datetime | None = None,
    ) -> Dict[str, Any]:
        normalized_time = current_time or datetime.now()
        config = self._merge_config(
            DEFAULT_INTERACT_COMMENT_LIMIT_CONFIG,
            state.get("config", {}) if isinstance(state, dict) else {},
        )
        events = state.get("events", []) if isinstance(state, dict) else []
        if not isinstance(events, list):
            events = []
        normalized_events = self._normalize_events(events, config, current_time=normalized_time)
        return {"config": config, "events": normalized_events}

    def _persist_state_unlocked(
        self,
        state: Dict[str, Any],
        current_time: datetime | None = None,
    ) -> None:
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
                    raise TimeoutError("获取 interact_comment_limit 文件锁超时")
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
        for key in {"min_interval_seconds", "rolling_10m_limit", "rolling_24h_limit", "event_retention_days"}:
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
                    "user_id": str(raw_event.get("user_id") or ""),
                    "account_id": self._normalize_account_id(raw_event.get("account_id") or ""),
                    "source": str(raw_event.get("source") or "one_click_interact"),
                    "comment_preview": str(raw_event.get("comment_preview") or "")[:80],
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
        config = deepcopy(state.get("config") or DEFAULT_INTERACT_COMMENT_LIMIT_CONFIG)
        events = self._filter_items_by_account_id(list(state.get("events") or []), account_id)
        window_10m_start = current_time - timedelta(minutes=10)
        window_24h_start = current_time - timedelta(hours=24)
        events_10m = self._events_since(events, window_10m_start)
        events_24h = self._events_since(events, window_24h_start)
        last_event_at = events[-1]["timestamp"] if events else ""
        last_event_time = self._parse_datetime(last_event_at) if last_event_at else None

        min_interval_seconds = int(config.get("min_interval_seconds", 15) or 15)
        next_interval_allowed_at = ""
        retry_after_interval_seconds = 0
        seconds_since_last_comment = None
        if last_event_time:
            next_interval_time = last_event_time + timedelta(seconds=min_interval_seconds)
            seconds_since_last_comment = max((current_time - last_event_time).total_seconds(), 0.0)
            if next_interval_time > current_time:
                next_interval_allowed_at = next_interval_time.isoformat()
                retry_after_interval_seconds = max(
                    int((next_interval_time - current_time).total_seconds() + 0.999),
                    0,
                )

        next_10m_slot_at = ""
        retry_after_10m_seconds = 0
        rolling_10m_limit = int(config.get("rolling_10m_limit", 10) or 10)
        if len(events_10m) >= rolling_10m_limit:
            oldest_10m = self._parse_datetime(events_10m[0]["timestamp"])
            if oldest_10m:
                next_10m_time = oldest_10m + timedelta(minutes=10)
                next_10m_slot_at = next_10m_time.isoformat()
                retry_after_10m_seconds = max(
                    int((next_10m_time - current_time).total_seconds() + 0.999),
                    0,
                )

        next_24h_slot_at = ""
        retry_after_daily_seconds = 0
        rolling_24h_limit = int(config.get("rolling_24h_limit", 100) or 100)
        if len(events_24h) >= rolling_24h_limit:
            oldest_24h = self._parse_datetime(events_24h[0]["timestamp"])
            if oldest_24h:
                next_24h_time = oldest_24h + timedelta(hours=24)
                next_24h_slot_at = next_24h_time.isoformat()
                retry_after_daily_seconds = max(
                    int((next_24h_time - current_time).total_seconds() + 0.999),
                    0,
                )

        return {
            "enabled": bool(config.get("enabled", True)),
            "config": config,
            "last_event_at": last_event_at,
            "seconds_since_last_comment": seconds_since_last_comment,
            "rolling_10m_count": len(events_10m),
            "rolling_24h_count": len(events_24h),
            "remaining_10m_quota": max(rolling_10m_limit - len(events_10m), 0),
            "remaining_24h_quota": max(rolling_24h_limit - len(events_24h), 0),
            "next_interval_allowed_at": next_interval_allowed_at,
            "next_10m_slot_at": next_10m_slot_at,
            "next_24h_slot_at": next_24h_slot_at,
            "retry_after_interval_seconds": retry_after_interval_seconds,
            "retry_after_10m_seconds": retry_after_10m_seconds,
            "retry_after_daily_seconds": retry_after_daily_seconds,
            "account_scope": {"account_id": self._normalize_account_id(account_id)},
        }

    @staticmethod
    def _events_since(events: List[Dict[str, Any]], cutoff: datetime) -> List[Dict[str, Any]]:
        filtered: List[Dict[str, Any]] = []
        for item in events:
            parsed = InteractCommentLimitService._parse_datetime(str(item.get("timestamp") or "").strip())
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


_interact_comment_limit_service: InteractCommentLimitService | None = None


def get_interact_comment_limit_service() -> InteractCommentLimitService:
    global _interact_comment_limit_service
    if _interact_comment_limit_service is None:
        _interact_comment_limit_service = InteractCommentLimitService()
    return _interact_comment_limit_service
