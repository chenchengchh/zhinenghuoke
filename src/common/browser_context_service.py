import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.infrastructure.runtime_paths import (
    get_account_browser_user_data_dir,
    get_account_crawler_browser_user_data_dir,
    get_account_registry_path,
)
from src.common.crawler_session_state import delete_crawler_session_cookies


class BrowserContextService:
    """管理浏览器上下文注册表与上下文级浏览器 profile 目录。"""

    def __init__(self, registry_path: Optional[Path] = None):
        self._lock = threading.RLock()
        self._registry_path = Path(registry_path or get_account_registry_path())
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _default_state() -> Dict[str, Any]:
        return {
            "version": 1,
            "active_account_id": "",
            "accounts": [],
        }

    @staticmethod
    def _normalize_string_list(value: Any) -> List[str]:
        if isinstance(value, str):
            raw_items = value.replace("，", ",").split(",")
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = []
        normalized_items: List[str] = []
        seen = set()
        for item in raw_items:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized_items.append(text)
        return normalized_items

    @staticmethod
    def _normalize_int(value: Any, default: int = 0) -> int:
        try:
            return max(int(value or 0), 0)
        except (TypeError, ValueError):
            return default

    def _load_state_unlocked(self) -> Dict[str, Any]:
        if not self._registry_path.exists():
            return self._default_state()
        try:
            with self._registry_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return self._default_state()

        if not isinstance(payload, dict):
            return self._default_state()

        accounts = payload.get("accounts")
        if not isinstance(accounts, list):
            accounts = []

        return {
            "version": int(payload.get("version", 1) or 1),
            "active_account_id": str(payload.get("active_account_id", "") or "").strip(),
            "accounts": [self._normalize_account_record(item) for item in accounts if isinstance(item, dict)],
        }

    def _save_state_unlocked(self, state: Dict[str, Any]) -> None:
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._registry_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        temp_path.replace(self._registry_path)

    def _normalize_account_record(self, item: Dict[str, Any]) -> Dict[str, Any]:
        account_id = str(item.get("id", "") or "").strip()
        display_name = str(item.get("display_name", "") or "").strip() or account_id or "未命名账户"
        created_at = str(item.get("created_at", "") or "").strip() or self._utcnow()
        updated_at = str(item.get("updated_at", "") or "").strip() or created_at
        return {
            "id": account_id,
            "display_name": display_name,
            "note": str(item.get("note", "") or "").strip(),
            "group_name": str(item.get("group_name", "") or "").strip(),
            "tags": self._normalize_string_list(item.get("tags", [])),
            "status": str(item.get("status", "") or "").strip(),
            "health_score": self._normalize_int(item.get("health_score", 0)),
            "created_at": created_at,
            "updated_at": updated_at,
            "last_login_at": str(item.get("last_login_at", "") or "").strip(),
            "last_checked_at": str(item.get("last_checked_at", "") or "").strip(),
            "last_known_login": bool(item.get("last_known_login", False)),
            "resolved_account_id": str(item.get("resolved_account_id", "") or "").strip(),
            "resolved_account_source": str(item.get("resolved_account_source", "") or "").strip(),
            "last_error": str(item.get("last_error", "") or "").strip(),
            "risk_flags": self._normalize_string_list(item.get("risk_flags", [])),
            "daily_send_count": self._normalize_int(item.get("daily_send_count", 0)),
            "daily_comment_count": self._normalize_int(item.get("daily_comment_count", 0)),
            "daily_reply_count": self._normalize_int(item.get("daily_reply_count", 0)),
            "last_runtime_status": str(item.get("last_runtime_status", "") or "").strip(),
            "authorized_by": str(item.get("authorized_by", "") or "").strip(),
            "authorized_at": str(item.get("authorized_at", "") or "").strip(),
            "reauth_before_at": str(item.get("reauth_before_at", "") or "").strip(),
            "last_auth_method": str(item.get("last_auth_method", "") or "").strip(),
            "last_reauth_reason": str(item.get("last_reauth_reason", "") or "").strip(),
            "risk_level": str(item.get("risk_level", "") or "").strip(),
            "cooldown_until": str(item.get("cooldown_until", "") or "").strip(),
            "next_safe_run_at": str(item.get("next_safe_run_at", "") or "").strip(),
            "recommended_action": str(item.get("recommended_action", "") or "").strip(),
        }

    def _serialize_account(self, account: Dict[str, Any], active_account_id: str = "") -> Dict[str, Any]:
        normalized = self._normalize_account_record(account)
        normalized["is_active"] = bool(active_account_id and normalized["id"] == active_account_id)
        normalized["browser_user_data_dir"] = str(
            get_account_browser_user_data_dir(normalized["id"]).resolve()
        )
        normalized["crawler_user_data_dir"] = str(
            get_account_crawler_browser_user_data_dir(normalized["id"]).resolve()
        )
        return normalized

    def list_accounts(self) -> Dict[str, Any]:
        with self._lock:
            state = self._load_state_unlocked()
            active_account_id = str(state.get("active_account_id", "") or "").strip()
            accounts = [
                self._serialize_account(item, active_account_id=active_account_id)
                for item in state.get("accounts", [])
                if str(item.get("id", "") or "").strip()
            ]
            accounts.sort(key=lambda item: item.get("created_at", ""), reverse=False)
            return {
                "active_account_id": active_account_id,
                "accounts": accounts,
            }

    def get_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        target_id = str(account_id or "").strip()
        if not target_id:
            return None
        with self._lock:
            state = self._load_state_unlocked()
            active_account_id = str(state.get("active_account_id", "") or "").strip()
            for item in state.get("accounts", []):
                if str(item.get("id", "") or "").strip() == target_id:
                    return self._serialize_account(item, active_account_id=active_account_id)
        return None

    def get_active_account_id(self) -> str:
        with self._lock:
            return str(self._load_state_unlocked().get("active_account_id", "") or "").strip()

    def get_active_account(self) -> Optional[Dict[str, Any]]:
        active_account_id = self.get_active_account_id()
        if not active_account_id:
            return None
        return self.get_account(active_account_id)

    def create_account(
        self,
        display_name: str = "",
        note: str = "",
        group_name: str = "",
        tags: Optional[List[str]] = None,
        authorized_by: str = "",
        authorized_at: str = "",
        reauth_before_at: str = "",
        last_auth_method: str = "",
        last_reauth_reason: str = "",
        risk_level: str = "",
        cooldown_until: str = "",
        next_safe_run_at: str = "",
        recommended_action: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._load_state_unlocked()
            now = self._utcnow()
            next_index = len(state.get("accounts", [])) + 1
            account_id = f"acct_{uuid.uuid4().hex[:8]}"
            record = self._normalize_account_record(
                {
                    "id": account_id,
                    "display_name": str(display_name or "").strip() or f"抖音账号 {next_index}",
                    "note": str(note or "").strip(),
                    "group_name": str(group_name or "").strip(),
                    "tags": self._normalize_string_list(tags or []),
                    "authorized_by": str(authorized_by or "").strip(),
                    "authorized_at": str(authorized_at or "").strip(),
                    "reauth_before_at": str(reauth_before_at or "").strip(),
                    "last_auth_method": str(last_auth_method or "").strip(),
                    "last_reauth_reason": str(last_reauth_reason or "").strip(),
                    "risk_level": str(risk_level or "").strip(),
                    "cooldown_until": str(cooldown_until or "").strip(),
                    "next_safe_run_at": str(next_safe_run_at or "").strip(),
                    "recommended_action": str(recommended_action or "").strip(),
                    "created_at": now,
                    "updated_at": now,
                    "last_checked_at": now,
                }
            )
            state.setdefault("accounts", []).append(record)
            if not str(state.get("active_account_id", "") or "").strip():
                state["active_account_id"] = account_id
            self._save_state_unlocked(state)
            return self._serialize_account(record, active_account_id=str(state.get("active_account_id", "") or ""))

    def activate_account(self, account_id: str) -> Dict[str, Any]:
        target_id = str(account_id or "").strip()
        if not target_id:
            raise ValueError("账户ID不能为空")
        with self._lock:
            state = self._load_state_unlocked()
            matched = None
            for item in state.get("accounts", []):
                if str(item.get("id", "") or "").strip() == target_id:
                    matched = item
                    break
            if matched is None:
                raise KeyError(f"账户不存在: {target_id}")
            state["active_account_id"] = target_id
            matched["updated_at"] = self._utcnow()
            self._save_state_unlocked(state)
            return self._serialize_account(matched, active_account_id=target_id)

    def update_account(self, account_id: str, **updates: Any) -> Dict[str, Any]:
        target_id = str(account_id or "").strip()
        if not target_id:
            raise ValueError("账户ID不能为空")
        with self._lock:
            state = self._load_state_unlocked()
            matched = None
            for item in state.get("accounts", []):
                if str(item.get("id", "") or "").strip() == target_id:
                    matched = item
                    break
            if matched is None:
                raise KeyError(f"账户不存在: {target_id}")

            for key in (
                "display_name",
                "note",
                "group_name",
                "status",
                "last_login_at",
                "last_checked_at",
                "resolved_account_id",
                "resolved_account_source",
                "last_error",
                "last_runtime_status",
                "authorized_by",
                "authorized_at",
                "reauth_before_at",
                "last_auth_method",
                "last_reauth_reason",
                "risk_level",
                "cooldown_until",
                "next_safe_run_at",
                "recommended_action",
            ):
                if key in updates and updates.get(key) is not None:
                    matched[key] = str(updates.get(key) or "").strip()
            if "last_known_login" in updates and updates.get("last_known_login") is not None:
                matched["last_known_login"] = bool(updates.get("last_known_login"))
            for key in ("health_score", "daily_send_count", "daily_comment_count", "daily_reply_count"):
                if key in updates and updates.get(key) is not None:
                    matched[key] = self._normalize_int(updates.get(key))
            for key in ("tags", "risk_flags"):
                if key in updates and updates.get(key) is not None:
                    matched[key] = self._normalize_string_list(updates.get(key))
            matched["updated_at"] = self._utcnow()
            self._save_state_unlocked(state)
            return self._serialize_account(
                matched,
                active_account_id=str(state.get("active_account_id", "") or "").strip(),
            )

    def delete_accounts(self, account_ids: List[str]) -> Dict[str, Any]:
        normalized_ids = []
        seen_ids = set()
        for raw_id in list(account_ids or []):
            account_id = str(raw_id or "").strip()
            if not account_id or account_id in seen_ids:
                continue
            seen_ids.add(account_id)
            normalized_ids.append(account_id)

        if not normalized_ids:
            raise ValueError("请至少选择一个账户")

        browser_dirs_to_delete: List[Path] = []
        crawler_dirs_to_delete: List[Path] = []
        with self._lock:
            state = self._load_state_unlocked()
            accounts = list(state.get("accounts", []) or [])
            existing_ids = {
                str(item.get("id", "") or "").strip()
                for item in accounts
                if str(item.get("id", "") or "").strip()
            }
            missing_ids = [account_id for account_id in normalized_ids if account_id not in existing_ids]
            if missing_ids:
                raise KeyError(f"账户不存在: {missing_ids[0]}")

            deleted_accounts = []
            remaining_accounts = []
            for item in accounts:
                account_id = str(item.get("id", "") or "").strip()
                if account_id in seen_ids:
                    deleted_accounts.append(self._serialize_account(item))
                    browser_dirs_to_delete.append(get_account_browser_user_data_dir(account_id))
                    crawler_dirs_to_delete.append(get_account_crawler_browser_user_data_dir(account_id))
                else:
                    remaining_accounts.append(item)

            state["accounts"] = remaining_accounts
            active_account_id = str(state.get("active_account_id", "") or "").strip()
            if active_account_id in seen_ids:
                state["active_account_id"] = (
                    str(remaining_accounts[0].get("id", "") or "").strip()
                    if remaining_accounts
                    else ""
                )
            self._save_state_unlocked(state)
            next_active_account_id = str(state.get("active_account_id", "") or "").strip()
            next_active_account = (
                self.get_account(next_active_account_id)
                if next_active_account_id
                else None
            )

        for target_dir in browser_dirs_to_delete + crawler_dirs_to_delete:
            try:
                if target_dir.exists():
                    shutil.rmtree(target_dir, ignore_errors=True)
            except Exception:
                pass

        for account_id in normalized_ids:
            try:
                delete_crawler_session_cookies(account_id)
            except Exception:
                pass

        return {
            "deleted_ids": normalized_ids,
            "deleted_accounts": deleted_accounts,
            "active_account_id": next_active_account_id,
            "active_account": next_active_account,
            "remaining_count": len((self.list_accounts() or {}).get("accounts", [])),
        }

    def note_login_snapshot(
        self,
        account_id: str,
        *,
        is_logged_in: bool,
        resolved_account_id: str = "",
        resolved_account_source: str = "",
        last_error: str = "",
    ) -> Dict[str, Any]:
        updates: Dict[str, Any] = {
            "last_known_login": bool(is_logged_in),
            "last_checked_at": self._utcnow(),
            "resolved_account_id": str(resolved_account_id or "").strip(),
            "resolved_account_source": str(resolved_account_source or "").strip(),
            "last_error": str(last_error or "").strip(),
        }
        if is_logged_in:
            updates["last_login_at"] = self._utcnow()
        return self.update_account(account_id, **updates)

    def resolve_browser_user_data_dir(self, account_id: str = "") -> Path:
        target_id = str(account_id or "").strip() or self.get_active_account_id()
        return get_account_browser_user_data_dir(target_id) if target_id else get_account_browser_user_data_dir("default")

    def resolve_crawler_user_data_dir(self, account_id: str = "") -> Path:
        target_id = str(account_id or "").strip() or self.get_active_account_id()
        return (
            get_account_crawler_browser_user_data_dir(target_id)
            if target_id
            else get_account_crawler_browser_user_data_dir("default")
        )


_browser_context_service: Optional[BrowserContextService] = None
_browser_context_service_lock = threading.Lock()


def get_browser_context_service() -> BrowserContextService:
    global _browser_context_service
    if _browser_context_service is None:
        with _browser_context_service_lock:
            if _browser_context_service is None:
                _browser_context_service = BrowserContextService()
    return _browser_context_service
