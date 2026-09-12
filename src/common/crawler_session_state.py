import json
import time
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger

from src.infrastructure.runtime_paths import get_app_state_dir


CRAWLER_SESSION_STATE_FILE = "crawler_session_state.json"
CRAWLER_SESSION_STATE_DIR = "crawler_session_state"
DEFAULT_MAX_COOKIE_AGE_SECONDS = 12 * 60 * 60
COOKIE_FIELDS = {
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
}


def _normalize_context_state_name(browser_context_id: str) -> str:
    normalized = str(browser_context_id or "").strip()
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in normalized)
    return safe_name or "default"


def get_crawler_session_state_path(browser_context_id: str = "") -> Path:
    app_state_dir = get_app_state_dir()
    target_context_id = str(browser_context_id or "").strip()
    if not target_context_id:
        return app_state_dir / CRAWLER_SESSION_STATE_FILE
    return app_state_dir / CRAWLER_SESSION_STATE_DIR / f"{_normalize_context_state_name(target_context_id)}.json"


def _sanitize_cookie(cookie: Any) -> Dict[str, Any]:
    if not isinstance(cookie, dict):
        return {}

    sanitized = {key: cookie[key] for key in COOKIE_FIELDS if key in cookie}
    if not sanitized.get("name") or not sanitized.get("domain"):
        return {}
    return sanitized


def save_crawler_session_cookies(
    cookies: List[Dict[str, Any]],
    *,
    source_user_data_dir: str = "",
    browser_context_id: str = "",
    current_login_account_id: str = "",
    current_login_account_source: str = "",
    current_login_account_resolved: bool = False,
) -> bool:
    sanitized_cookies = []
    for item in cookies or []:
        sanitized = _sanitize_cookie(item)
        if sanitized:
            sanitized_cookies.append(sanitized)

    if not sanitized_cookies:
        return False

    payload = {
        "exported_at": int(time.time()),
        "source_user_data_dir": str(source_user_data_dir or "").strip(),
        "browser_context_id": str(browser_context_id or "").strip(),
        "current_login_account_id": str(current_login_account_id or "").strip(),
        "current_login_account_source": str(current_login_account_source or "").strip(),
        "current_login_account_resolved": bool(current_login_account_resolved),
        "cookies": sanitized_cookies,
    }

    state_path = get_crawler_session_state_path(browser_context_id)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        f"已导出 crawler 登录态 cookies: count={len(sanitized_cookies)}, "
        f"path={state_path}"
    )
    return True


def load_crawler_session_cookies(
    *,
    max_age_seconds: int = DEFAULT_MAX_COOKIE_AGE_SECONDS,
    expected_login_account_id: str = "",
) -> List[Dict[str, Any]]:
    state_path = get_crawler_session_state_path()
    fallback_path = get_crawler_session_state_path()
    if not state_path.exists() and fallback_path.exists():
        state_path = fallback_path
    if not state_path.exists():
        return []

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"读取 crawler 登录态 cookies 失败: {exc}")
        return []

    exported_at = int(payload.get("exported_at", 0) or 0)
    if exported_at <= 0:
        return []

    if max_age_seconds > 0 and (time.time() - exported_at) > max_age_seconds:
        logger.info("crawler 登录态 cookies 已过期，跳过导入")
        return []

    cookies = []
    for item in payload.get("cookies", []) or []:
        sanitized = _sanitize_cookie(item)
        if sanitized:
            cookies.append(sanitized)
    return cookies


def delete_crawler_session_cookies(browser_context_id: str = "") -> bool:
    state_path = get_crawler_session_state_path(browser_context_id)
    if not state_path.exists():
        return False
    try:
        state_path.unlink()
        logger.info(f"已删除 crawler 登录态 cookies: path={state_path}")
        return True
    except Exception as exc:
        logger.warning(f"删除 crawler 登录态 cookies 失败: {exc}")
        return False
