from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
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


DEFAULT_PRIVATE_MESSAGE_LIMIT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "first_installed_at": "",
    "increment_cycle_days": 1,
    "initial_hourly_limit": 20,
    "initial_daily_limit": 30,
    "hourly_increment": 1,
    "daily_increment": 10,
    "max_hourly_limit": 20,
    "max_daily_limit": 80,
    "event_retention_days": 30,
    "updated_at": "",
}

DEFAULT_PRIVATE_MESSAGE_LIMIT_STATE: Dict[str, Any] = {
    "config": deepcopy(DEFAULT_PRIVATE_MESSAGE_LIMIT_CONFIG),
    "events": [],
    "reservations": [],
}


@dataclass
class PrivateMessageLimitDecision:
    allowed: bool
    message: str
    status_code: str
    details: Dict[str, Any]


class PrivateMessageLimitService:
    FILE_LOCK_TIMEOUT_SECONDS = 10.0
    FILE_LOCK_RETRY_INTERVAL_SECONDS = 0.05
    # Reservation is only an in-flight guard for one send attempt, not an hourly hold.
    # Keep it short so app reloads / crashes do not block the account for a full hour.
    RESERVATION_TTL_SECONDS = 5 * 60

    def __init__(self, state_path: Path | None = None) -> None:
        self._state_path = state_path or get_data_dir() / "private_message_limit.json"
        self._lock_path = Path(f"{self._state_path}.lock")
        self._lock = threading.RLock()

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

    def get_status(
        self,
        now: datetime | None = None,
        *,
        account_id: str = "",
    ) -> Dict[str, Any]:
        with self._locked_state_context(current_time=now) as (normalized, current_time):
            return self._build_status_from_state(
                normalized,
                current_time=current_time,
                account_id=account_id,
            )

    def check_send_allowed(
        self,
        requested_count: int = 1,
        now: datetime | None = None,
        *,
        account_id: str = "",
    ) -> PrivateMessageLimitDecision:
        current_time = now or datetime.now()
        requested_count = max(int(requested_count or 0), 0)
        if requested_count <= 0:
            return PrivateMessageLimitDecision(
                allowed=False,
                message="发送数量必须大于 0",
                status_code="invalid_count",
                details={"requested_count": requested_count},
            )

        with self._locked_state_context(current_time=current_time) as (state, locked_time):
            scoped_events = self._filter_items_by_account_id(state["events"], account_id)
            scoped_reservations = self._filter_items_by_account_id(state.get("reservations", []), account_id)
            status = self._build_status_from_state(
                state,
                current_time=locked_time,
                account_id=account_id,
            )
            return self._evaluate_send_allowance(
                status,
                requested_count,
                events=scoped_events,
                reservations=scoped_reservations,
                current_time=locked_time,
            )

    def acquire_send_permit(
        self,
        *,
        count: int = 1,
        message: str = "",
        user_id: str = "",
        account_id: str = "",
        source: str = "auto_send",
        now: datetime | None = None,
    ) -> PrivateMessageLimitDecision:
        current_time = now or datetime.now()
        requested_count = max(int(count or 0), 0)
        if requested_count <= 0:
            return PrivateMessageLimitDecision(
                allowed=False,
                message="发送数量必须大于 0",
                status_code="invalid_count",
                details={"requested_count": requested_count},
            )

        with self._locked_state_context(current_time=current_time) as (state, locked_time):
            normalized_account_id = self._normalize_account_id(account_id)
            scoped_events = self._filter_items_by_account_id(state["events"], normalized_account_id)
            scoped_reservations = self._filter_items_by_account_id(state.get("reservations", []), normalized_account_id)
            status = self._build_status_from_state(
                state,
                current_time=locked_time,
                account_id=normalized_account_id,
            )
            decision = self._evaluate_send_allowance(
                status,
                requested_count,
                events=scoped_events,
                reservations=scoped_reservations,
                current_time=locked_time,
            )
            if not decision.allowed or decision.status_code == "disabled":
                return decision

            reservation_token = uuid.uuid4().hex
            state["reservations"].append(
                {
                    "token": reservation_token,
                    "timestamp": locked_time.isoformat(),
                    "count": requested_count,
                    "source": str(source or "auto_send"),
                    "user_id": str(user_id or ""),
                    "account_id": normalized_account_id,
                    "message_preview": str(message or "")[:80],
                }
            )
            self._persist_state_unlocked(state, current_time=locked_time)
            updated_status = self._build_status_from_state(
                state,
                current_time=locked_time,
                account_id=normalized_account_id,
            )
            return PrivateMessageLimitDecision(
                allowed=True,
                message="发送额度预占用成功。",
                status_code="reserved",
                details={
                    **updated_status,
                    "requested_count": requested_count,
                    "reservation_token": reservation_token,
                },
            )

    def finalize_send_attempt(
        self,
        *,
        reservation_token: str = "",
        success: bool,
        count: int = 1,
        message: str = "",
        user_id: str = "",
        account_id: str = "",
        source: str = "auto_send",
        now: datetime | None = None,
    ) -> Dict[str, Any]:
        current_time = now or datetime.now()
        normalized_count = max(int(count or 0), 0)
        token = str(reservation_token or "").strip()

        with self._locked_state_context(current_time=current_time) as (state, locked_time):
            reservation = None
            if token:
                for item in state["reservations"]:
                    if item.get("token") == token:
                        reservation = item
                        break
                if reservation is not None:
                    state["reservations"] = [
                        item for item in state["reservations"] if item.get("token") != token
                    ]

            if success and normalized_count > 0:
                effective_count = int((reservation or {}).get("count", normalized_count) or normalized_count)
                state["events"].append(
                    {
                        "timestamp": locked_time.isoformat(),
                        "count": effective_count,
                        "source": str((reservation or {}).get("source") or source or "auto_send"),
                        "user_id": str((reservation or {}).get("user_id") or user_id or ""),
                        "account_id": self._normalize_account_id(
                            (reservation or {}).get("account_id") or account_id or ""
                        ),
                        "message_preview": str((reservation or {}).get("message_preview") or message or "")[:80],
                    }
                )

            self._persist_state_unlocked(state, current_time=locked_time)
            effective_account_id = self._normalize_account_id(
                (reservation or {}).get("account_id") or account_id or ""
            )
            return self._build_status_from_state(
                state,
                current_time=locked_time,
                account_id=effective_account_id,
            )

    def _evaluate_send_allowance(
        self,
        status: Dict[str, Any],
        requested_count: int,
        *,
        events: List[Dict[str, Any]] | None = None,
        reservations: List[Dict[str, Any]] | None = None,
        current_time: datetime | None = None,
    ) -> PrivateMessageLimitDecision:
        normalized_now = current_time or datetime.now()
        events = list(events or [])
        reservations = list(reservations or [])

        if not status["enabled"]:
            return PrivateMessageLimitDecision(
                allowed=True,
                message="发送量限制已关闭，允许发送。",
                status_code="disabled",
                details=status,
            )

        projected_hour_count = status["occupied_hourly_quota"] + requested_count
        projected_daily_count = status["occupied_daily_quota"] + requested_count
        hourly_limit = status["effective_hourly_limit"]
        daily_limit = status["effective_daily_limit"]

        if projected_hour_count > hourly_limit:
            remaining = max(hourly_limit - status["occupied_hourly_quota"], 0)
            next_available_at, retry_after_seconds = self._estimate_next_available_time(
                events,
                reservations,
                limit=hourly_limit,
                requested_count=requested_count,
                window_seconds=60 * 60,
                current_time=normalized_now,
            )
            return PrivateMessageLimitDecision(
                allowed=False,
                message=(
                    f"自动私信发送已触发每小时限制：当前 1 小时内已占用 {status['occupied_hourly_quota']} 条额度，"
                    f"当前上限 {hourly_limit} 条，本次最多还能发送 {remaining} 条。"
                ),
                status_code="hourly_limit_exceeded",
                details={
                    **status,
                    "requested_count": requested_count,
                    "remaining_quota": remaining,
                    "projected_hour_count": projected_hour_count,
                    "blocking_window": "hourly",
                    "next_send_available_at": next_available_at,
                    "retry_after_seconds": retry_after_seconds,
                },
            )

        if projected_daily_count > daily_limit:
            remaining = max(daily_limit - status["occupied_daily_quota"], 0)
            next_available_at, retry_after_seconds = self._estimate_next_available_time(
                events,
                reservations,
                limit=daily_limit,
                requested_count=requested_count,
                window_seconds=24 * 60 * 60,
                current_time=normalized_now,
            )
            return PrivateMessageLimitDecision(
                allowed=False,
                message=(
                    f"自动私信发送已触发 24 小时限制：过去 24 小时已占用 {status['occupied_daily_quota']} 条额度，"
                    f"当前上限 {daily_limit} 条，本次最多还能发送 {remaining} 条。"
                ),
                status_code="daily_limit_exceeded",
                details={
                    **status,
                    "requested_count": requested_count,
                    "remaining_quota": remaining,
                    "projected_daily_count": projected_daily_count,
                    "blocking_window": "daily",
                    "next_send_available_at": next_available_at,
                    "retry_after_seconds": retry_after_seconds,
                },
            )

        return PrivateMessageLimitDecision(
            allowed=True,
            message="发送量校验通过。",
            status_code="allowed",
            details={
                **status,
                "requested_count": requested_count,
                "projected_hour_count": projected_hour_count,
                "projected_daily_count": projected_daily_count,
            },
        )

    def _estimate_next_available_time(
        self,
        events: List[Dict[str, Any]],
        reservations: List[Dict[str, Any]],
        *,
        limit: int,
        requested_count: int,
        window_seconds: int,
        current_time: datetime,
    ) -> Tuple[str, int]:
        occupied_timestamps: List[datetime] = []
        cutoff = current_time - timedelta(seconds=max(int(window_seconds or 0), 1))
        for item in [*(events or []), *(reservations or [])]:
            if not isinstance(item, dict):
                continue
            parsed = self._parse_datetime(str(item.get("timestamp") or ""))
            if not parsed or parsed < cutoff:
                continue
            count = max(int(item.get("count", 1) or 1), 1)
            occupied_timestamps.extend([parsed] * count)

        occupied_timestamps.sort()
        slots_to_release = len(occupied_timestamps) + max(int(requested_count or 0), 0) - max(int(limit or 0), 1)
        if slots_to_release <= 0:
            return current_time.isoformat(), 0

        if not occupied_timestamps:
            fallback_time = current_time + timedelta(seconds=max(int(window_seconds or 0), 1))
            return fallback_time.isoformat(), max(int((fallback_time - current_time).total_seconds()), 1)

        release_index = min(slots_to_release - 1, len(occupied_timestamps) - 1)
        release_time = occupied_timestamps[release_index] + timedelta(seconds=max(int(window_seconds or 0), 1))
        retry_after_seconds = max(int((release_time - current_time).total_seconds()), 0)
        return release_time.isoformat(), retry_after_seconds

    def record_send_event(
        self,
        *,
        count: int = 1,
        message: str = "",
        user_id: str = "",
        account_id: str = "",
        source: str = "auto_send",
        now: datetime | None = None,
    ) -> Dict[str, Any]:
        current_time = now or datetime.now()
        normalized_count = max(int(count or 0), 0)
        if normalized_count <= 0:
            return self.get_status(now=current_time)

        with self._locked_state_context(current_time=current_time) as (normalized, locked_time):
            normalized["events"].append(
                {
                    "timestamp": locked_time.isoformat(),
                    "count": normalized_count,
                    "source": str(source or "auto_send"),
                    "user_id": str(user_id or ""),
                    "account_id": self._normalize_account_id(account_id),
                    "message_preview": str(message or "")[:80],
                }
            )
            self._persist_state_unlocked(normalized, current_time=locked_time)
            return self._build_status_from_state(
                normalized,
                current_time=locked_time,
                account_id=account_id,
            )

    def get_switch_status(self) -> Dict[str, Any]:
        config = self.load_config()
        return {
            "enabled": bool(config.get("enabled", True)),
            "updated_at": str(config.get("updated_at") or ""),
        }

    def release_reservation(
        self,
        reservation_token: str = "",
        *,
        account_id: str = "",
        now: datetime | None = None,
    ) -> bool:
        """
        阶段A·F-6：显式释放 reservation。

        适用于 `send_private_message` 期间发生未捕获异常、调用方无法保证
        `finalize_send_attempt` 被执行时使用；该方法不会把 reservation 转为 event，
        因此不会消耗实际额度。

        Returns:
            是否实际删除了某个 reservation
        """
        current_time = now or datetime.now()
        token = str(reservation_token or "").strip()
        if not token:
            return False
        try:
            with self._locked_state_context(current_time=current_time) as (state, locked_time):
                before = len(state.get("reservations", []))
                state["reservations"] = [
                    item for item in state.get("reservations", [])
                    if str(item.get("token") or "") != token
                ]
                removed = before - len(state["reservations"])
                if removed > 0:
                    self._persist_state_unlocked(state, current_time=locked_time)
                return removed > 0
        except (KeyError, AttributeError, TypeError, ValueError, OSError) as exc:
            logger.warning(f"释放 reservation 失败 token={token[:8]}... err={exc}")
            return False

    def export_api_doc(self) -> Dict[str, Any]:
        return {
            "module": "private_message_limit",
            "description": "自动私信发送量控制接口",
            "endpoints": [
                {
                    "method": "GET",
                    "path": "/api/send-limit/config",
                    "description": "获取发送限制配置与实时状态",
                },
                {
                    "method": "POST",
                    "path": "/api/send-limit/config",
                    "description": "保存发送限制配置",
                },
                {
                    "method": "GET",
                    "path": "/api/send-limit/status",
                    "description": "获取开关状态、有效限制与实时计数",
                },
            ],
        }

    def _load_state_unlocked(self, current_time: datetime | None = None) -> Dict[str, Any]:
        if not self._state_path.exists():
            initial = deepcopy(DEFAULT_PRIVATE_MESSAGE_LIMIT_STATE)
            normalized_time = current_time or datetime.now()
            initial["config"]["first_installed_at"] = normalized_time.isoformat()
            self._persist_state_unlocked(initial, current_time=normalized_time)
            return initial

        try:
            content = self._state_path.read_text(encoding="utf-8").strip()
            if not content:
                return self._normalize_state(
                    deepcopy(DEFAULT_PRIVATE_MESSAGE_LIMIT_STATE),
                    current_time=current_time,
                )
            loaded = json.loads(content)
            if not isinstance(loaded, dict):
                return self._normalize_state(
                    deepcopy(DEFAULT_PRIVATE_MESSAGE_LIMIT_STATE),
                    current_time=current_time,
                )
            return self._normalize_state(loaded, current_time=current_time)
        except Exception:
            return self._normalize_state(
                deepcopy(DEFAULT_PRIVATE_MESSAGE_LIMIT_STATE),
                current_time=current_time,
            )

    def _normalize_state(
        self,
        state: Dict[str, Any],
        current_time: datetime | None = None,
    ) -> Dict[str, Any]:
        normalized_time = current_time or datetime.now()
        config = self._merge_config(
            DEFAULT_PRIVATE_MESSAGE_LIMIT_CONFIG,
            state.get("config", {}) if isinstance(state, dict) else {},
        )
        if not str(config.get("first_installed_at") or "").strip():
            config["first_installed_at"] = normalized_time.isoformat()

        events = state.get("events", []) if isinstance(state, dict) else []
        if not isinstance(events, list):
            events = []
        events = self._normalize_events(events, config, current_time=normalized_time)
        reservations = state.get("reservations", []) if isinstance(state, dict) else []
        if not isinstance(reservations, list):
            reservations = []
        reservations = self._normalize_reservations(
            reservations,
            current_time=normalized_time,
        )
        return {"config": config, "events": events, "reservations": reservations}

    def _persist_state_unlocked(
        self,
        state: Dict[str, Any],
        current_time: datetime | None = None,
    ) -> None:
        normalized = self._normalize_state(state, current_time=current_time)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(normalized, ensure_ascii=False, indent=2)
        fd, temp_path = tempfile.mkstemp(
            suffix=".tmp",
            dir=str(self._state_path.parent),
        )
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
                    raise TimeoutError("获取 private_message_limit 文件锁超时")
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
        for key in DEFAULT_PRIVATE_MESSAGE_LIMIT_CONFIG:
            if key not in payload:
                continue
            value = payload[key]
            if key == "enabled":
                merged[key] = bool(value)
                continue
            if key in {
                "increment_cycle_days",
                "initial_hourly_limit",
                "initial_daily_limit",
                "hourly_increment",
                "daily_increment",
                "max_hourly_limit",
                "max_daily_limit",
                "event_retention_days",
            }:
                try:
                    merged[key] = max(int(value), 1)
                except Exception:
                    continue
                continue
            if key in {"first_installed_at", "updated_at"}:
                merged[key] = str(value or "")
        if merged["initial_hourly_limit"] > merged["max_hourly_limit"]:
            merged["initial_hourly_limit"] = merged["max_hourly_limit"]
        if merged["initial_daily_limit"] > merged["max_daily_limit"]:
            merged["initial_daily_limit"] = merged["max_daily_limit"]
        return merged

    def _normalize_events(
        self,
        events: List[Dict[str, Any]],
        config: Dict[str, Any],
        current_time: datetime | None = None,
    ) -> List[Dict[str, Any]]:
        normalized_time = current_time or datetime.now()
        retention_cutoff = normalized_time - timedelta(days=int(config.get("event_retention_days", 30)))
        normalized_events: List[Dict[str, Any]] = []
        for raw_event in events:
            if not isinstance(raw_event, dict):
                continue
            timestamp_text = str(raw_event.get("timestamp") or "").strip()
            parsed = self._parse_datetime(timestamp_text)
            if not parsed or parsed < retention_cutoff:
                continue
            count = max(int(raw_event.get("count", 1) or 1), 1)
            normalized_events.append(
                {
                    "timestamp": parsed.isoformat(),
                    "count": count,
                    "source": str(raw_event.get("source") or "auto_send"),
                    "user_id": str(raw_event.get("user_id") or ""),
                    "account_id": self._normalize_account_id(raw_event.get("account_id") or ""),
                    "message_preview": str(raw_event.get("message_preview") or "")[:80],
                }
            )
        normalized_events.sort(key=lambda item: item["timestamp"])
        return normalized_events

    def _normalize_reservations(
        self,
        reservations: List[Dict[str, Any]],
        current_time: datetime,
    ) -> List[Dict[str, Any]]:
        retention_cutoff = current_time - timedelta(seconds=self.RESERVATION_TTL_SECONDS)
        normalized_reservations: List[Dict[str, Any]] = []
        for raw_reservation in reservations:
            if not isinstance(raw_reservation, dict):
                continue
            token = str(raw_reservation.get("token") or "").strip()
            timestamp_text = str(raw_reservation.get("timestamp") or "").strip()
            parsed = self._parse_datetime(timestamp_text)
            if not token or not parsed or parsed < retention_cutoff:
                continue
            count = max(int(raw_reservation.get("count", 1) or 1), 1)
            normalized_reservations.append(
                {
                    "token": token,
                    "timestamp": parsed.isoformat(),
                    "count": count,
                    "source": str(raw_reservation.get("source") or "auto_send"),
                    "user_id": str(raw_reservation.get("user_id") or ""),
                    "account_id": self._normalize_account_id(raw_reservation.get("account_id") or ""),
                    "message_preview": str(raw_reservation.get("message_preview") or "")[:80],
                }
            )
        normalized_reservations.sort(key=lambda item: item["timestamp"])
        return normalized_reservations

    def _build_status_from_state(
        self,
        state: Dict[str, Any],
        *,
        current_time: datetime,
        account_id: str = "",
    ) -> Dict[str, Any]:
        return self._build_status_snapshot(
            state["config"],
            state["events"],
            state.get("reservations", []),
            current_time=current_time,
            account_id=account_id,
        )

    def _build_status_snapshot(
        self,
        config: Dict[str, Any],
        events: List[Dict[str, Any]],
        reservations: List[Dict[str, Any]],
        *,
        current_time: datetime,
        account_id: str = "",
    ) -> Dict[str, Any]:
        normalized_account_id = self._normalize_account_id(account_id)
        scoped_events = self._filter_items_by_account_id(events, normalized_account_id)
        scoped_reservations = self._filter_items_by_account_id(reservations, normalized_account_id)
        effective_limits = self._calculate_effective_limits(config, current_time=current_time)
        hour_window_start = current_time - timedelta(hours=1)
        day_window_start = current_time - timedelta(hours=24)
        current_hour_count = self._sum_events_since(scoped_events, hour_window_start)
        rolling_daily_count = self._sum_events_since(scoped_events, day_window_start)
        current_hour_reserved_count = self._sum_events_since(scoped_reservations, hour_window_start)
        rolling_daily_reserved_count = self._sum_events_since(scoped_reservations, day_window_start)
        occupied_hourly_quota = current_hour_count + current_hour_reserved_count
        occupied_daily_quota = rolling_daily_count + rolling_daily_reserved_count
        next_increment_at, days_until_next_increment = self._get_next_increment_time(
            config,
            current_time=current_time,
        )

        return {
            "enabled": bool(config.get("enabled", True)),
            "switch": {
                "enabled": bool(config.get("enabled", True)),
                "updated_at": str(config.get("updated_at") or ""),
            },
            "account_scope": {
                "account_id": normalized_account_id,
                "resolved": bool(normalized_account_id),
            },
            "config": deepcopy(config),
            "usage_days": effective_limits["usage_days"],
            "increment_cycles": effective_limits["increment_cycles"],
            "effective_hourly_limit": effective_limits["effective_hourly_limit"],
            "effective_daily_limit": effective_limits["effective_daily_limit"],
            "current_hour_count": current_hour_count,
            "rolling_24h_count": rolling_daily_count,
            "current_hour_reserved_count": current_hour_reserved_count,
            "rolling_24h_reserved_count": rolling_daily_reserved_count,
            "occupied_hourly_quota": occupied_hourly_quota,
            "occupied_daily_quota": occupied_daily_quota,
            "remaining_hourly_quota": max(effective_limits["effective_hourly_limit"] - occupied_hourly_quota, 0),
            "remaining_daily_quota": max(effective_limits["effective_daily_limit"] - occupied_daily_quota, 0),
            "first_installed_at": str(config.get("first_installed_at") or ""),
            "next_increment_at": next_increment_at,
            "days_until_next_increment": days_until_next_increment,
            "recent_events": deepcopy(scoped_events[-20:]),
            "active_reservations": deepcopy(scoped_reservations[-20:]),
        }

    @staticmethod
    def _normalize_account_id(account_id: Any) -> str:
        return str(account_id or "").strip()

    def _filter_items_by_account_id(
        self,
        items: List[Dict[str, Any]],
        account_id: str = "",
    ) -> List[Dict[str, Any]]:
        normalized_account_id = self._normalize_account_id(account_id)
        if not normalized_account_id:
            return list(items or [])
        return [
            item
            for item in (items or [])
            if self._normalize_account_id((item or {}).get("account_id") or "") == normalized_account_id
        ]

    def _calculate_effective_limits(
        self,
        config: Dict[str, Any],
        *,
        current_time: datetime,
    ) -> Dict[str, Any]:
        installed_at = self._parse_datetime(str(config.get("first_installed_at") or "")) or current_time
        usage_days = max((current_time.date() - installed_at.date()).days, 0)
        increment_cycle_days = max(int(config.get("increment_cycle_days", 7) or 7), 1)
        increment_cycles = usage_days // increment_cycle_days

        effective_hourly_limit = min(
            int(config.get("max_hourly_limit", 20) or 20),
            int(config.get("initial_hourly_limit", 10) or 10)
            + increment_cycles * int(config.get("hourly_increment", 2) or 2),
        )
        effective_daily_limit = min(
            int(config.get("max_daily_limit", 100) or 100),
            int(config.get("initial_daily_limit", 50) or 50)
            + increment_cycles * int(config.get("daily_increment", 10) or 10),
        )

        return {
            "usage_days": usage_days,
            "increment_cycles": increment_cycles,
            "effective_hourly_limit": effective_hourly_limit,
            "effective_daily_limit": effective_daily_limit,
        }

    def _get_next_increment_time(
        self,
        config: Dict[str, Any],
        *,
        current_time: datetime,
    ) -> Tuple[str, int]:
        installed_at = self._parse_datetime(str(config.get("first_installed_at") or "")) or current_time
        cycle_days = max(int(config.get("increment_cycle_days", 7) or 7), 1)
        current_cycle = max((current_time.date() - installed_at.date()).days, 0) // cycle_days
        next_increment = installed_at + timedelta(days=(current_cycle + 1) * cycle_days)
        delta_days = max((next_increment.date() - current_time.date()).days, 0)
        return next_increment.isoformat(), delta_days

    @staticmethod
    def _sum_events_since(events: List[Dict[str, Any]], cutoff: datetime) -> int:
        total = 0
        for event in events:
            parsed = PrivateMessageLimitService._parse_datetime(str(event.get("timestamp") or ""))
            if parsed and parsed >= cutoff:
                total += max(int(event.get("count", 1) or 1), 1)
        return total

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None


_private_message_limit_service: PrivateMessageLimitService | None = None
_private_message_limit_service_lock = threading.Lock()


def get_private_message_limit_service() -> PrivateMessageLimitService:
    global _private_message_limit_service
    if _private_message_limit_service is None:
        with _private_message_limit_service_lock:
            if _private_message_limit_service is None:
                _private_message_limit_service = PrivateMessageLimitService()
    return _private_message_limit_service
