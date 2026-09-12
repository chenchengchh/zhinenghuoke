from __future__ import annotations

from typing import Any, Dict, Mapping


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def build_browser_runtime_status(
    *,
    bot_runtime: Mapping[str, Any] | None,
    crawler_snapshot: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    bot_runtime_data = _as_dict(bot_runtime)
    browser_context = _as_dict(bot_runtime_data.get("browser_context"))
    browser_context_id = str(
        bot_runtime_data.get("browser_context_id") or browser_context.get("id") or ""
    ).strip()
    crawler_snapshot_data = _as_dict(crawler_snapshot)
    return {
        "browser_context_id": browser_context_id,
        "browser_context": browser_context,
        "bot_runtime": bot_runtime_data,
        "crawler_runtime": {
            "current_login_account_id": str(
                crawler_snapshot_data.get("current_login_account_id") or ""
            ).strip(),
            "current_login_account_source": str(
                crawler_snapshot_data.get("current_login_account_source") or ""
            ).strip(),
            "current_login_account_resolved": bool(
                crawler_snapshot_data.get("current_login_account_resolved", False)
            ),
            "expected_login_account_id": str(
                crawler_snapshot_data.get("expected_login_account_id") or ""
            ).strip(),
            "expected_login_account_source": str(
                crawler_snapshot_data.get("expected_login_account_source") or ""
            ).strip(),
            "expected_login_account_resolved": bool(
                crawler_snapshot_data.get("expected_login_account_resolved", False)
            ),
            "login_transfer_matched": bool(
                crawler_snapshot_data.get("login_transfer_matched", False)
            ),
            "browser_ready": bool(crawler_snapshot_data.get("browser_ready", False)),
            "alive": bool(crawler_snapshot_data.get("alive", False)),
            "busy": bool(crawler_snapshot_data.get("busy", False)),
            "current_task": str(crawler_snapshot_data.get("current_task") or "Idle"),
            "stalled": bool(crawler_snapshot_data.get("stalled", False)),
            "stall_reason": str(crawler_snapshot_data.get("stall_reason") or "").strip(),
            "stall_duration_seconds": float(
                crawler_snapshot_data.get("stall_duration_seconds") or 0
            ),
            "force_reset_recommended": bool(
                crawler_snapshot_data.get("force_reset_recommended", False)
            ),
        },
    }


def get_active_browser_name(runtime: Mapping[str, Any] | None) -> str:
    runtime_data = _as_dict(runtime)
    browser_context = _as_dict(runtime_data.get("browser_context"))
    browser_context_id = str(runtime_data.get("browser_context_id") or "").strip()
    return str(browser_context.get("display_name") or browser_context_id or "当前浏览器").strip()


def crawler_runtime_has_usable_login(runtime: Mapping[str, Any] | None) -> bool:
    runtime_data = _as_dict(runtime)
    crawler_runtime = _as_dict(runtime_data.get("crawler_runtime"))
    return bool(
        crawler_runtime.get("browser_ready", False)
        and crawler_runtime.get("current_login_account_resolved", False)
        and str(crawler_runtime.get("current_login_account_id") or "").strip()
    )


def runtime_has_usable_login(runtime: Mapping[str, Any] | None) -> bool:
    runtime_data = _as_dict(runtime)
    bot_runtime = _as_dict(runtime_data.get("bot_runtime"))
    return bool(bot_runtime.get("is_logged_in", False)) or crawler_runtime_has_usable_login(
        runtime_data
    )


def build_runtime_readiness(
    runtime: Mapping[str, Any] | None,
    *,
    action_label: str = "开始运行",
    require_browser: bool = True,
    require_login: bool = True,
) -> Dict[str, Any]:
    runtime_data = _as_dict(runtime)
    bot_runtime = _as_dict(runtime_data.get("bot_runtime"))
    browser_running = bool(bot_runtime.get("browser_running", False))
    login_ready = runtime_has_usable_login(runtime_data)
    browser_name = get_active_browser_name(runtime_data)

    reason_code = ""
    message = ""
    ok = True
    if require_browser and not browser_running:
        ok = False
        reason_code = "browser_not_ready"
        message = f"当前浏览器尚未准备好，请先打开网页并完成登录后再{action_label}"
    elif require_login and not login_ready:
        ok = False
        reason_code = "web_login_not_ready"
        message = f"{browser_name}尚未检测到可用网页登录态，请先扫码登录或导入 Cookies 后再{action_label}"

    return {
        "ok": ok,
        "reason_code": reason_code,
        "message": message,
        "browser_name": browser_name,
        "browser_running": browser_running,
        "login_ready": login_ready,
        "runtime": runtime_data,
    }
