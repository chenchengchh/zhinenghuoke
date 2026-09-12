from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Optional

import requests

from src.licensing.machine_fingerprint import build_machine_fingerprint
from src.remote_control.policy_models import RemoteControlPolicy, RemoteControlSnapshot
from src.remote_control.policy_storage import RemotePolicyStorage
from src.remote_control.policy_validator import sign_remote_policy, validate_remote_policy


class RemoteControlService:
    def __init__(self, storage: Optional[RemotePolicyStorage] = None):
        self._storage = storage or RemotePolicyStorage()
        self._lock = threading.Lock()
        self._last_refresh_at: Optional[datetime] = None

    def get_instance_id(self) -> str:
        return os.getenv("HUOKE_INSTANCE_ID", "").strip() or build_machine_fingerprint()

    def get_policy_url(self) -> str:
        return os.getenv("HUOKE_REMOTE_POLICY_URL", "").strip()

    def get_refresh_interval_seconds(self) -> int:
        raw = os.getenv("HUOKE_REMOTE_POLICY_REFRESH_SECONDS", "60").strip() or "60"
        try:
            return max(15, int(raw))
        except ValueError:
            return 60

    def generate_signed_policy(
        self,
        *,
        app_enabled: bool = True,
        crawler_enabled: bool = True,
        monitor_enabled: bool = True,
        auto_reply_enabled: bool = True,
        message: str = "",
        instance_id: str = "",
        policy_id: str = "",
    ) -> dict:
        policy = RemoteControlPolicy(
            policy_id=policy_id or f"REMOTE-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            instance_id=instance_id,
            issued_at=datetime.now(timezone.utc),
            app_enabled=app_enabled,
            crawler_enabled=crawler_enabled,
            monitor_enabled=monitor_enabled,
            auto_reply_enabled=auto_reply_enabled,
            message=message,
            signature="",
        )
        payload = policy.model_dump(mode="json")
        payload["signature"] = sign_remote_policy(payload)
        return payload

    def apply_policy(self, payload: dict, source: str = "manual") -> RemoteControlSnapshot:
        valid, reason, parsed = validate_remote_policy(payload, self.get_instance_id())
        if not valid or parsed is None:
            return RemoteControlSnapshot(enabled=False, source=source, valid=False, reason=reason)

        serialized = parsed.model_dump(mode="json")
        with self._lock:
            self._storage.save(serialized)
            self._last_refresh_at = datetime.now(timezone.utc)
        return self.get_snapshot(source=source)

    def refresh_from_server(self) -> RemoteControlSnapshot:
        policy_url = self.get_policy_url()
        if not policy_url:
            return self.get_snapshot(source="default")

        response = requests.get(
            policy_url,
            params={"instance_id": self.get_instance_id()},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and "policy" in payload and isinstance(payload["policy"], dict):
            payload = payload["policy"]
        return self.apply_policy(payload, source="remote")

    def get_snapshot(self, source: Optional[str] = None) -> RemoteControlSnapshot:
        payload = self._storage.load()
        if not payload:
            return RemoteControlSnapshot(
                enabled=False,
                source=source or "default",
                valid=True,
                reason="policy_missing",
                last_refresh_at=self._last_refresh_at.isoformat() if self._last_refresh_at else None,
            )

        valid, reason, parsed = validate_remote_policy(payload, self.get_instance_id())
        if not valid or parsed is None:
            return RemoteControlSnapshot(
                enabled=True,
                source=source or "cache",
                valid=False,
                reason=reason,
                last_refresh_at=self._last_refresh_at.isoformat() if self._last_refresh_at else None,
            )

        return RemoteControlSnapshot(
            enabled=True,
            source=source or "cache",
            valid=True,
            reason="ok",
            app_enabled=parsed.app_enabled,
            crawler_enabled=parsed.crawler_enabled,
            monitor_enabled=parsed.monitor_enabled,
            auto_reply_enabled=parsed.auto_reply_enabled,
            policy_id=parsed.policy_id,
            instance_id=parsed.instance_id,
            message=parsed.message,
            expire_at=parsed.expire_at.isoformat() if parsed.expire_at else None,
            last_refresh_at=self._last_refresh_at.isoformat() if self._last_refresh_at else None,
        )

    def is_feature_enabled(self, feature_name: str) -> bool:
        snapshot = self.get_snapshot()
        if not snapshot.valid:
            return True
        if not snapshot.app_enabled:
            return False
        feature_map = {
            "crawler": snapshot.crawler_enabled,
            "monitor": snapshot.monitor_enabled,
            "auto_reply": snapshot.auto_reply_enabled,
        }
        return bool(feature_map.get(feature_name, True))

    def clear_policy(self) -> None:
        with self._lock:
            self._storage.clear()
            self._last_refresh_at = datetime.now(timezone.utc)

    def enforce_runtime_limits(self, bot_service=None, process_manager=None) -> None:
        snapshot = self.get_snapshot()
        if not snapshot.valid:
            return

        if process_manager is not None:
            if not snapshot.crawler_enabled:
                process_manager.stop_crawler_process()

        if bot_service is None:
            return

        if not snapshot.monitor_enabled:
            try:
                bot_service.stop_message_monitoring(reason="remote_control:disable_monitor")
            except Exception:
                pass

        if not snapshot.app_enabled:
            try:
                bot_service.stop_browser()
            except Exception:
                pass


_remote_control_service: Optional[RemoteControlService] = None
_remote_control_lock = threading.Lock()


def get_remote_control_service() -> RemoteControlService:
    global _remote_control_service
    if _remote_control_service is None:
        with _remote_control_lock:
            if _remote_control_service is None:
                _remote_control_service = RemoteControlService()
    return _remote_control_service
