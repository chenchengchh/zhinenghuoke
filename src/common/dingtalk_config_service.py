from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from src.infrastructure.runtime_paths import get_data_dir


DEFAULT_DINGTALK_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "app_key": "",
    "app_secret": "",
    "agent_id": "",
    "receiver_name": "",
    "receiver_mobile": "",
    "receiver_userid": "",
    "notify_contact_provided_only": True,
    "last_bind_at": "",
    "last_bind_status": "not_bound",
    "last_bind_error": "",
    "last_test_send_at": "",
    "last_test_send_status": "never",
    "last_test_send_error": "",
    "updated_at": "",
}


class DingTalkConfigService:
    def __init__(self, config_path: Path | None = None) -> None:
        self._config_path = config_path or get_data_dir() / "dingtalk_config.json"
        self._lock = threading.Lock()

    def load_config(self) -> Dict[str, Any]:
        with self._lock:
            return self._load_config_unlocked()

    def save_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            current = self._load_config_unlocked()
            merged = self._merge_config(current, payload or {})
            merged["updated_at"] = datetime.now().isoformat()
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return deepcopy(merged)

    def update_binding_result(
        self,
        *,
        receiver_mobile: str,
        receiver_userid: str = "",
        status: str,
        error: str = "",
    ) -> Dict[str, Any]:
        payload = {
            "receiver_mobile": receiver_mobile,
            "receiver_userid": receiver_userid,
            "last_bind_status": status,
            "last_bind_error": error,
            "last_bind_at": datetime.now().isoformat(),
        }
        return self.save_config(payload)

    def update_test_send_result(self, *, status: str, error: str = "") -> Dict[str, Any]:
        payload = {
            "last_test_send_status": status,
            "last_test_send_error": error,
            "last_test_send_at": datetime.now().isoformat(),
        }
        return self.save_config(payload)

    def mask_config_for_client(self, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
        snapshot = deepcopy(config or self.load_config())
        app_key = str(snapshot.get("app_key") or "")
        receiver_mobile = str(snapshot.get("receiver_mobile") or "")
        return {
            "enabled": bool(snapshot.get("enabled", False)),
            "app_key_masked": self._mask_value(app_key, prefix=4, suffix=4),
            "app_secret_configured": bool(snapshot.get("app_secret")),
            "agent_id": str(snapshot.get("agent_id") or ""),
            "receiver_name": str(snapshot.get("receiver_name") or ""),
            "receiver_mobile_masked": self._mask_mobile(receiver_mobile),
            "receiver_mobile": receiver_mobile,
            "receiver_userid": str(snapshot.get("receiver_userid") or ""),
            "notify_contact_provided_only": bool(snapshot.get("notify_contact_provided_only", False)),
            "last_bind_at": str(snapshot.get("last_bind_at") or ""),
            "last_bind_status": str(snapshot.get("last_bind_status") or "not_bound"),
            "last_bind_error": str(snapshot.get("last_bind_error") or ""),
            "last_test_send_at": str(snapshot.get("last_test_send_at") or ""),
            "last_test_send_status": str(snapshot.get("last_test_send_status") or "never"),
            "last_test_send_error": str(snapshot.get("last_test_send_error") or ""),
            "updated_at": str(snapshot.get("updated_at") or ""),
        }

    def validate_config(self, config: Dict[str, Any] | None = None) -> list[str]:
        snapshot = config or self.load_config()
        if not snapshot.get("enabled"):
            return []

        errors = []
        if not str(snapshot.get("app_key") or "").strip():
            errors.append("缺少 AppKey")
        if not str(snapshot.get("app_secret") or "").strip():
            errors.append("缺少 AppSecret")
        if not str(snapshot.get("agent_id") or "").strip():
            errors.append("缺少 AgentId")
        return errors

    def can_send(self, config: Dict[str, Any] | None = None) -> tuple[bool, str]:
        snapshot = config or self.load_config()
        errors = self.validate_config(snapshot)
        if errors:
            return False, "；".join(errors)
        if not str(snapshot.get("receiver_userid") or "").strip():
            return False, "未绑定固定接收人"
        return True, ""

    def _load_config_unlocked(self) -> Dict[str, Any]:
        if not self._config_path.exists():
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(
                json.dumps(DEFAULT_DINGTALK_CONFIG, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return deepcopy(DEFAULT_DINGTALK_CONFIG)

        try:
            content = self._config_path.read_text(encoding="utf-8").strip()
            if not content:
                return deepcopy(DEFAULT_DINGTALK_CONFIG)
            loaded = json.loads(content)
            if not isinstance(loaded, dict):
                return deepcopy(DEFAULT_DINGTALK_CONFIG)
            return self._merge_config(DEFAULT_DINGTALK_CONFIG, loaded)
        except Exception:
            return deepcopy(DEFAULT_DINGTALK_CONFIG)

    def _merge_config(self, base: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        merged = deepcopy(base)
        for key in DEFAULT_DINGTALK_CONFIG:
            if key not in payload:
                continue
            value = payload[key]
            if key in {"app_key", "app_secret"} and value in (None, ""):
                continue
            merged[key] = value
        return merged

    @staticmethod
    def _mask_value(value: str, *, prefix: int = 0, suffix: int = 0) -> str:
        if not value:
            return ""
        if len(value) <= prefix + suffix:
            return "*" * len(value)
        return f"{value[:prefix]}{'*' * max(4, len(value) - prefix - suffix)}{value[-suffix:] if suffix else ''}"

    @staticmethod
    def _mask_mobile(mobile: str) -> str:
        normalized = str(mobile or "").strip()
        if len(normalized) < 7:
            return normalized
        return f"{normalized[:3]}****{normalized[-4:]}"


_dingtalk_config_service: DingTalkConfigService | None = None
_dingtalk_config_service_lock = threading.Lock()


def get_dingtalk_config_service() -> DingTalkConfigService:
    global _dingtalk_config_service
    if _dingtalk_config_service is None:
        with _dingtalk_config_service_lock:
            if _dingtalk_config_service is None:
                _dingtalk_config_service = DingTalkConfigService()
    return _dingtalk_config_service
