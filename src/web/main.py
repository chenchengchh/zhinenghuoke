# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false, reportMissingTypeArgument=false
# 抑制 Pyright 在 main.py 中报"无法访问属性 / 未知成员类型"等警告。
# 根因：Crawler (8485 行) 和 DouYinRPAEngine (7626 行) 超过 Pyright class body
# length limit（默认 30 行），Pyright 停止解析类体内方法/属性，导致 main.py 中
# `bot_service.rpa_launcher.rpa_engine._states` 等链式访问被推为 Any/Unknown。
# 这些 Pylance 警告不影响运行时（Crawler/DouYinRPAEngine 的方法在运行时正常存在），
# 且拆分这两个 8000+ 行大类是独立重构工程，故在 main.py 顶部用 Pyright 指令抑制。
from fastapi import FastAPI, Request, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import TYPE_CHECKING, List, Optional, Dict, Any, cast
import uvicorn
import os
import sys
import asyncio
import concurrent.futures
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

# Pylance 静态分析需要的类型别名（仅在类型检查时导入，运行时不执行）
# 用途：让 Pylance 把 `bot_service` 推断为 `BotService` 而非 `_LazyServiceProxy`，
#       从而看到 .db / .is_running / .browser_manager 等真实属性（PEP 562 + TYPE_CHECKING）
if TYPE_CHECKING:
    from src.common.marketing_tracking_service import MarketingTrackingService
    from src.common.private_message_limit_service import PrivateMessageLimitService
    from src.web.bot_service import BotService

    bot_service: BotService
    marketing_tracking_service: MarketingTrackingService
    private_message_limit_service: PrivateMessageLimitService
# 运行时：bot_service / marketing_tracking_service / private_message_limit_service
# 通过 _LazyServiceProxy 包装（保留运行时惰性解析）。
# Pylance 通过 TYPE_CHECKING 注解看到真实 BotService 类型（消除"无法访问属性"警告）。
# 注意：纯 PEP 562 模块级 __getattr__ 方案有局限——函数内 global 查找
# （如 get_initial_intent_data 内 `bot_service.db`）不会触发模块 __getattr__，
# 会抛 NameError。所以必须保留 _LazyServiceProxy 实例。

PROMETHEUS_METRICS_PORT = int(os.getenv("PROMETHEUS_METRICS_PORT", "9090"))

from src.common.utils import setup_logger
from src.config.settings import (
    CRAWLER_QUEUE_PRIORITY_DEFAULT,
    CRAWLER_SKIP_EXISTING_VIDEOS_DEFAULT,
    CRAWLER_WORKER_THREADS_DEFAULT,
    DOUYIN_HOME_URL,
)
from src.common.follow_up_reminder_service import get_follow_up_reminder_service
from src.common.private_message_limit_service import get_private_message_limit_service
from src.common.crawler_session_state import save_crawler_session_cookies
from src.infrastructure.runtime_paths import (
    get_base_dir,
    get_data_dir,
    get_log_dir,
    get_knowledge_base_path,
    get_static_dir,
    get_templates_dir,
)
from src.infrastructure.config import get_config
from src.infrastructure.middleware import (
    ContentLengthFixMiddleware,
    ErrorHandlingMiddleware,
    LoggingMiddleware,
    RequestMiddleware,
    SecurityHeadersMiddleware,
)
from src.licensing.license_service import get_license_service
from src.remote_control import get_remote_control_service
from src.web.license_admin_auth import require_license_admin
from src.web.routes import get_all_routers
from src.web.crawler_task_payloads import (
    build_search_command_payload,
    build_send_messages_payload,
)
from src.web.runtime_readiness import (
    build_browser_runtime_status as build_runtime_status_payload,
    build_runtime_readiness,
    crawler_runtime_has_usable_login,
    runtime_has_usable_login,
)

logger = setup_logger("web")
_status_cache_lock = threading.Lock()
_status_cache_ttl_seconds = 2.0
_status_cache: Dict[str, Any] = {"timestamp": 0.0, "payload": None}
_model_warmup_state_lock = threading.Lock()
_model_warmup_state: Dict[str, Any] = {
    "llm": {"status": "pending"},
    "embedding": {"status": "pending"},
}
_crawler_init_timeout_seconds = max(
    float(os.getenv("CRAWLER_INIT_TIMEOUT_SECONDS", "90") or 90.0),
    20.0,
)
_crawler_init_poll_interval_seconds = 0.1


def _record_task_startup_trace(event: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """
    向 `logs/task_startup_trace.jsonl` 追加一条任务启动追踪记录。

    阶段A·F-4：将 send/search/interact 三类任务的启动追踪统一到同一个文件，
    便于按 trace_id 关联前端→后端→子进程的完整链路。
    """
    try:
        log_dir = get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        trace_path = log_dir / "task_startup_trace.jsonl"
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event": str(event or "").strip() or "unknown",
            "payload": dict(payload or {}),
        }
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug(f"写入 task_startup_trace 失败: {exc}")


def _build_license_block_message(snapshot) -> str:
    # type: ignore[union-attr] 抑制 Pylance 把 status 推断为 str（getattr 默认值是 str）
    # 后报告"无法访问类 str 的属性 value"。snapshot 是 Any 类型，status 可能是
    # Enum 实例（有 .value）或字符串（无 .value），用 hasattr 兜底。
    status: Any = getattr(snapshot, "status", "") or ""
    if hasattr(status, "value"):
        # status 可能是 Enum 实例，提取 .value；str 实例没有 .value。
        status = status.value  # type: ignore[attr-defined]
    status = str(status or "")
    if status == "expired":
        return "授权已到期，请和供应商联系获取使用权限"
    if status == "clock_rollback":
        return "检测到系统时间被回拨，请校准电脑时间后重试，或联系供应商获取使用权限"
    if status == "missing":
        return "软件尚未激活，请先激活"
    if status in {"invalid", "machine_mismatch"}:
        return "授权无效，请和供应商联系获取使用权限"
    return "软件尚未激活或授权已失效，请先激活"


def _require_license_for_feature(feature_name: str) -> Optional[JSONResponse]:
    license_service = get_license_service()
    snapshot = license_service.get_state_snapshot()
    if not snapshot.valid:
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "message": _build_license_block_message(snapshot),
                "license_status": snapshot.model_dump(mode="json"),
            },
        )
    if not license_service.is_feature_enabled(feature_name):
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "message": f"当前授权未启用功能: {feature_name}",
                "license_status": snapshot.model_dump(mode="json"),
            },
        )
    return None


def _require_runtime_feature(feature_name: str) -> Optional[JSONResponse]:
    blocked = _require_license_for_feature(feature_name)
    if blocked:
        return blocked

    remote_control_service = get_remote_control_service()
    snapshot = remote_control_service.get_snapshot()
    if not remote_control_service.is_feature_enabled(feature_name):
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "message": snapshot.message or f"远程策略已禁用功能: {feature_name}",
                "remote_control_status": snapshot.model_dump(mode="json"),
            },
        )
    return None


def _get_bot_service():
    from src.web.bot_service import get_bot_service

    return get_bot_service()


def _get_process_manager():
    from src.common.process_manager import get_process_manager

    return get_process_manager()


def _get_marketing_tracking_service():
    from src.common.marketing_tracking_service import marketing_tracking_service

    return marketing_tracking_service


def _get_private_message_limit_runtime_service():
    return get_private_message_limit_service()


async def _await_future(future, timeout: float = 60):
    """将阻塞的Future.result()转为异步等待，避免阻塞事件循环"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: future.result(timeout=timeout))


class _LazyServiceProxy:
    """[PERF-FIX] 延迟解析服务实例，避免模块导入阶段触发重初始化。

    Pylance 通过顶部的 `if TYPE_CHECKING:` 类型注解看到真实 BotService 类型
    （消除"无法访问属性"警告）。本类仅作为运行时惰性包装，`__getattr__`
    转发所有属性访问到实际服务实例。

    注意：纯 PEP 562 模块级 __getattr__ 方案有局限——函数内 global 查找
    （如 get_initial_intent_data 内 `bot_service.db`）不会触发模块 __getattr__，
    会抛 NameError。所以必须保留本类包装。
    """

    def __init__(self, resolver):
        object.__setattr__(self, "_resolver", resolver)

    def _resolve(self):
        return object.__getattribute__(self, "_resolver")()

    def __getattr__(self, item):
        return getattr(self._resolve(), item)

    def __setattr__(self, key, value):
        if key == "_resolver" or key.startswith("_"):
            object.__setattr__(self, key, value)
            return
        setattr(self._resolve(), key, value)


def _invalidate_status_cache():
    with _status_cache_lock:
        _status_cache["timestamp"] = 0.0
        _status_cache["payload"] = None


def _set_model_warmup_state(kind: str, payload: Dict[str, Any]) -> None:
    with _model_warmup_state_lock:
        _model_warmup_state[kind] = dict(payload or {})
    _invalidate_status_cache()


def _get_model_warmup_snapshot() -> Dict[str, Any]:
    with _model_warmup_state_lock:
        return {
            "llm": dict(_model_warmup_state.get("llm") or {}),
            "embedding": dict(_model_warmup_state.get("embedding") or {}),
        }


def _get_crawler_process_snapshot() -> Dict[str, Any]:
    try:
        return dict(((_get_process_manager().get_status() or {}).get("crawler", {}) or {}))
    except Exception:
        return {}


def _get_runtime_quota_scope() -> Dict[str, Any]:
    return {
        "account_id": "",
        "resolved": False,
        "source": "",
        "send_scope_id": "__unresolved_current_login__",
        "interact_scope_id": "",
        "reply_scope_id": "",
    }


def _get_send_limit_scope() -> Dict[str, Any]:
    crawler_status = _get_crawler_process_snapshot()
    account_id = str(crawler_status.get("current_login_account_id") or "").strip()
    resolved = bool(crawler_status.get("current_login_account_resolved", False) and account_id)
    if resolved:
        effective_account_id = account_id
        effective_account_source = "current_login"
    else:
        effective_account_id = "__unresolved_current_login__"
        effective_account_source = "unresolved"
    return {
        "account_id": account_id,
        "resolved": resolved,
        "source": str(crawler_status.get("current_login_account_source") or "").strip(),
        "effective_account_id": effective_account_id,
        "effective_account_source": effective_account_source,
    }

def _normalize_text_list(values: Any) -> List[str]:
    if isinstance(values, str):
        raw_items = values.replace("，", ",").split(",")
    elif isinstance(values, (list, tuple, set)):
        raw_items = list(values)
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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return default


def _build_crawler_login_transfer_payload(*, force_scope_refresh: bool = False) -> Dict[str, Any]:
    return {}


def _crawler_runtime_has_usable_login(runtime: Dict[str, Any]) -> bool:
    return crawler_runtime_has_usable_login(runtime)


def _runtime_has_usable_login(runtime: Dict[str, Any]) -> bool:
    return runtime_has_usable_login(runtime)


def _validate_browser_context_for_task(
    *,
    force_refresh: bool = True,
    action_label: str = "开始运行",
) -> tuple[bool, str, Dict[str, Any]]:
    runtime = _build_browser_runtime_status(force_refresh=force_refresh)
    readiness = build_runtime_readiness(
        runtime,
        action_label=action_label,
        require_browser=True,
        require_login=False,
    )
    return bool(readiness.get("ok", False)), str(readiness.get("message") or ""), runtime


def _validate_browser_runtime_for_task(
    *,
    force_refresh: bool = True,
    action_label: str = "开始运行",
    require_resolved_login: bool = True,
) -> tuple[bool, str, Dict[str, Any]]:
    runtime = _build_browser_runtime_status(force_refresh=force_refresh)
    readiness = build_runtime_readiness(
        runtime,
        action_label=action_label,
        require_browser=True,
        require_login=True,
    )
    return bool(readiness.get("ok", False)), str(readiness.get("message") or ""), runtime


def _build_browser_runtime_status(force_refresh: bool = False) -> Dict[str, Any]:
    bot_runtime = _get_bot_service().get_browser_runtime_snapshot(
        force_login_check=force_refresh,
        force_scope_refresh=force_refresh,
        persist_registry=force_refresh,
    )
    crawler_snapshot = _get_crawler_process_snapshot() or {}
    return build_runtime_status_payload(
        bot_runtime=bot_runtime,
        crawler_snapshot=crawler_snapshot,
    )


def _apply_effective_login_state(status: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(status or {})
    crawler_runtime = dict(normalized.get("crawler_process") or {})
    crawler_has_login = bool(
        crawler_runtime.get("browser_ready", False)
        and crawler_runtime.get("current_login_account_resolved", False)
        and str(crawler_runtime.get("current_login_account_id") or "").strip()
    )
    if not crawler_has_login:
        return normalized

    normalized["is_logged_in"] = True
    if not str(normalized.get("current_login_account_id") or "").strip():
        normalized["current_login_account_id"] = str(crawler_runtime.get("current_login_account_id") or "").strip()
        normalized["current_login_account_source"] = str(crawler_runtime.get("current_login_account_source") or "").strip()
        normalized["current_login_account_resolved"] = bool(crawler_runtime.get("current_login_account_resolved", False))
    return normalized


def _merge_crawler_process_status(status: Dict[str, Any]) -> Dict[str, Any]:
    crawler_status = _get_crawler_process_snapshot()
    status["crawler_process"] = crawler_status
    status["agent_mode"] = True
    status["crawl_agent"] = {
        "running": bool(crawler_status.get("alive", False)),
        "task": crawler_status.get("current_task") or None,
        "last_activity": crawler_status.get("last_heartbeat"),
        "status": crawler_status,
    }
    status["monitor_agent"] = {
        "running": bool(status.get("effective_monitoring", False)),
        "task": "bot_service" if status.get("effective_monitoring", False) else None,
        "last_activity": status.get("last_message_time"),
        "status": {
            "effective_monitoring": bool(status.get("effective_monitoring", False)),
            "monitoring_state": status.get("monitoring_state"),
        },
    }

    crawler_task = str(crawler_status.get("current_task") or "Idle")
    if bool(crawler_status.get("alive", False)) and crawler_task and crawler_task != "Idle":
        status["current_task"] = crawler_task
        status["progress"] = dict(
            crawler_status.get("progress", {"total": 0, "current": 0, "detail": ""})
            or {"total": 0, "current": 0, "detail": ""}
        )
    
    if "last_search_summary" in crawler_status:
        status["last_search_summary"] = crawler_status["last_search_summary"]

    if "scheduled_send_resume" in crawler_status:
        status["scheduled_send_resume"] = crawler_status.get("scheduled_send_resume")

    return status


def _crawler_process_is_ready(status: Dict[str, Any]) -> bool:
    return (
        bool(status.get("alive", False))
        and bool(status.get("browser_ready", False))
        and bool(status.get("initialized", False))
        and not bool(status.get("busy", False))
        and str(status.get("current_task") or "Idle") == "Idle"
    )


def _get_crawler_busy_message() -> str:
    return "独立爬取/发送进程正在运行中，请先停止当前任务"


def _validate_crawler_idle_for_new_task() -> Optional[JSONResponse]:
    crawler_status = _get_crawler_process_snapshot()
    crawler_task = str(crawler_status.get("current_task") or "Idle")
    if crawler_task == "Idle":
        return None
    return JSONResponse(status_code=400, content={"message": _get_crawler_busy_message()})


def _wait_for_crawler_process_ready(timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(float(timeout_seconds or 0), 1.0)
    while time.monotonic() < deadline:
        if _crawler_process_is_ready(_get_crawler_process_snapshot()):
            return True
        time.sleep(_crawler_init_poll_interval_seconds)
    return _crawler_process_is_ready(_get_crawler_process_snapshot())


def _ensure_crawler_process_ready(*, source: str = "", trace_id: str = "") -> tuple[bool, str]:
    trace_payload = {"source": source, "trace_id": trace_id}
    login_transfer = _build_crawler_login_transfer_payload(force_scope_refresh=True)
    try:
        active_bot_service = _get_bot_service()
        browser_manager = getattr(active_bot_service, "browser_manager", None)
        if browser_manager and getattr(browser_manager, "context", None):
            cookies = []
            if hasattr(browser_manager, "export_cookies"):
                cookies = list(browser_manager.export_cookies() or [])
            else:
                cookies = list(browser_manager.context.cookies() or [])
            if cookies:
                save_crawler_session_cookies(
                    cookies,
                    source_user_data_dir=str(getattr(browser_manager, "user_data_dir", "") or ""),
                )
                _record_task_startup_trace(
                    "ensure_ready_exported_cookies",
                    {
                        **trace_payload,
                        "cookie_count": len(cookies),
                        "source_user_data_dir": str(getattr(browser_manager, "user_data_dir", "") or ""),
                    },
                )
    except Exception as exc:
        logger.warning(f"导出 crawler 登录态失败，继续尝试启动独立进程: {exc}")
        _record_task_startup_trace(
            "ensure_ready_export_cookies_failed",
            {
                **trace_payload,
                "error": str(exc),
            },
        )

    process_manager = _get_process_manager()
    if hasattr(process_manager, "ensure_crawler_process_stopped_for_restart"):
        cleanup_result = process_manager.ensure_crawler_process_stopped_for_restart()
        _record_task_startup_trace(
            "ensure_ready_cleanup_checked",
            {
                **trace_payload,
                "cleanup_result": dict(cleanup_result or {}),
            },
        )
        if cleanup_result.get("cleaned") is False and cleanup_result.get("reason"):
            _record_task_startup_trace(
                "ensure_ready_cleanup_blocked",
                {
                    **trace_payload,
                    "reason": str(cleanup_result.get("reason") or ""),
                },
            )
            return False, f"{cleanup_result.get('reason')}，但回收旧进程失败"
    started = process_manager.start_crawler_process()
    _record_task_startup_trace(
        "ensure_ready_start_process_result",
        {
            **trace_payload,
            "started": bool(started),
        },
    )
    if not started:
        return False, "独立爬取/发送进程启动失败"

    initial_status = _get_crawler_process_snapshot()
    _record_task_startup_trace(
        "ensure_ready_initial_status",
        {
            **trace_payload,
            "status": dict(initial_status or {}),
        },
    )
    if _crawler_process_is_ready(initial_status):
        _record_task_startup_trace(
            "ensure_ready_already_ready",
            {
                **trace_payload,
                "status": dict(initial_status or {}),
            },
        )
        return True, ""

    current_task = str(initial_status.get("current_task") or "Idle")
    init_in_progress = (
        bool(initial_status.get("alive", False))
        and not bool(initial_status.get("browser_ready", False))
        and (
            bool(initial_status.get("busy", False))
            or current_task == "Initializing browser"
        )
    )

    if not init_in_progress:
        init_sent = process_manager.send_crawler_command(
            "init_browser",
            dict(login_transfer),
        )
        _record_task_startup_trace(
            "ensure_ready_init_browser_sent",
            {
                **trace_payload,
                "sent": bool(init_sent),
            },
        )
        if not init_sent:
            return False, "独立爬取/发送进程浏览器初始化失败"
    else:
        _record_task_startup_trace(
            "ensure_ready_init_already_in_progress",
            {
                **trace_payload,
                "status": dict(initial_status or {}),
            },
        )

    # 新机器首次冷启动会包含 profile 初始化、首页导航、cookie 导入等步骤，
    # 需要显著长于单次页面导航的预算，避免“页面已打开但请求先超时”。
    wait_ok = _wait_for_crawler_process_ready(_crawler_init_timeout_seconds)
    final_status = _get_crawler_process_snapshot()
    _record_task_startup_trace(
        "ensure_ready_wait_finished",
        {
            **trace_payload,
            "wait_ok": bool(wait_ok),
            "final_status": dict(final_status or {}),
            "timeout_seconds": _crawler_init_timeout_seconds,
        },
    )
    if wait_ok:
        return True, ""
    detail = str((final_status.get("progress") or {}).get("detail") or "").strip()
    current_task = str(final_status.get("current_task") or "Idle")
    if detail:
        return False, f"独立爬取/发送进程浏览器初始化超时: {detail}"
    if current_task and current_task != "Idle":
        return False, f"独立爬取/发送进程浏览器初始化超时，当前状态: {current_task}"
    return False, "独立爬取/发送进程浏览器初始化超时，请确认浏览器已成功启动并完成登录"


def _kickoff_crawler_process_browser_init() -> tuple[bool, str, Dict[str, Any]]:
    process_manager = _get_process_manager()
    login_transfer = _build_crawler_login_transfer_payload(force_scope_refresh=True)

    if hasattr(process_manager, "ensure_crawler_process_stopped_for_restart"):
        process_manager.ensure_crawler_process_stopped_for_restart()

    if not process_manager.start_crawler_process():
        return False, "独立爬取/发送进程启动失败", _get_crawler_process_snapshot()

    initial_status = _get_crawler_process_snapshot()
    if _crawler_process_is_ready(initial_status):
        return True, "", initial_status

    current_task = str(initial_status.get("current_task") or "Idle")
    init_in_progress = (
        bool(initial_status.get("alive", False))
        and not bool(initial_status.get("browser_ready", False))
        and (
            bool(initial_status.get("busy", False))
            or current_task == "Initializing browser"
        )
    )
    if not init_in_progress:
        if not process_manager.send_crawler_command(
            "init_browser",
            dict(login_transfer),
        ):
            return False, "独立爬取/发送进程浏览器初始化失败", _get_crawler_process_snapshot()
        initial_status = _get_crawler_process_snapshot()

    return True, "", initial_status


def _stop_crawler_task_with_recovery(
    close_process: bool = False,
    *,
    force_reset_on_timeout: bool = False,
) -> Dict[str, Any]:
    process_manager = _get_process_manager()
    if hasattr(process_manager, "stop_crawler_task"):
        result = process_manager.stop_crawler_task(
            graceful_timeout=3.0,
            force_reset_on_timeout=force_reset_on_timeout,
        )
    else:
        success = process_manager.send_crawler_command("stop_task")
        result = {
            "success": bool(success),
            "forced": False,
            "pending": bool(success),
            "message": "独立爬取/发送进程停止中..." if success else "独立爬取/发送进程停止失败",
        }

    if (
        close_process
        and bool(result.get("success", False))
        and hasattr(process_manager, "stop_crawler_process")
    ):
        crawler_status = _get_crawler_process_snapshot()
        if bool(crawler_status.get("alive", False)):
            stopped = process_manager.stop_crawler_process(join_timeout=2.0, terminate_timeout=2.0)
            result = dict(result)
            result["process_stopped"] = bool(stopped)
            if stopped:
                result["message"] = "独立爬取/发送任务已停止，后台进程已关闭"
            else:
                result["success"] = False
                result["message"] = "独立爬取/发送任务已停止，但后台进程关闭失败"
    _invalidate_status_cache()
    return result


def _parse_analytics_datetime(raw_value: Any) -> Optional[datetime]:
    if not raw_value:
        return None
    if isinstance(raw_value, datetime):
        return raw_value
    if isinstance(raw_value, (int, float)):
        try:
            return datetime.fromtimestamp(raw_value)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(raw_value, str):
        try:
            return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _resolve_customer_display_name(customer: Dict[str, Any]) -> str:
    return (
        customer.get("name")
        or customer.get("nickname")
        or customer.get("customer_name")
        or customer.get("unique_id")
        or customer.get("sec_uid")
        or ""
    ).strip()


def _collect_marketing_fallback_records(days: int = 30) -> List[Dict[str, Any]]:
    customers = list(bot_service.db.get_all_customers() or [])
    safe_days = max(int(days or 30), 1)
    cutoff = datetime.now() - timedelta(days=safe_days)

    recent_records: List[Dict[str, Any]] = []
    eligible_records: List[Dict[str, Any]] = []
    for customer in customers:
        status = (customer.get("status") or "").strip().lower()
        if status not in {"sent", "replied", "delivered", "read", "failed"}:
            continue
        eligible_records.append(customer)
        event_time = _parse_analytics_datetime(customer.get("updated_at") or customer.get("created_at"))
        if event_time and event_time < cutoff:
            continue
        recent_records.append(customer)

    return recent_records or eligible_records

def _build_marketing_tracking_fallback(days: int = 30) -> Dict[str, Any]:
    safe_days = max(int(days or 30), 1)
    recent_records = _collect_marketing_fallback_records(safe_days)

    total = len(recent_records)
    
    replied = 0
    read = 0
    delivered = 0
    failed = 0
    
    for item in recent_records:
        status = (item.get("status") or "").strip().lower()
        if status == "replied":
            replied += 1
            read += 1
            delivered += 1
        elif status == "read":
            read += 1
            delivered += 1
        elif status == "delivered":
            delivered += 1
        elif status == "failed":
            failed += 1
        elif status == "sent":
            delivered += 1
            
    if total > 0 and delivered == 0 and failed < total:
        delivered = total - failed

    unique_customers = len(
        {
            _resolve_customer_display_name(item)
            for item in recent_records
            if _resolve_customer_display_name(item)
        }
    )

    daily_stats: Dict[str, Dict[str, int]] = {}
    for item in recent_records:
        event_time = _parse_analytics_datetime(item.get("updated_at") or item.get("created_at")) or datetime.now()
        date_key = event_time.strftime("%Y-%m-%d")
        stats = daily_stats.setdefault(date_key, {"sent": 0, "replied": 0})
        stats["sent"] += 1
        if (item.get("status") or "").strip().lower() == "replied":
            stats["replied"] += 1

    return {
        "period_days": safe_days,
        "total_messages": total,
        "unique_customers": unique_customers,
        "delivered": delivered,
        "read": read,
        "replied": replied,
        "failed": failed,
        "delivered_rate": round(delivered / max(total, 1) * 100, 1),
        "read_rate": round(read / max(delivered, 1) * 100, 1),
        "reply_rate": round(replied / max(delivered, 1) * 100, 1),
        "daily_stats": daily_stats,
    }


def _build_marketing_top_customers_fallback(limit: int = 10, sort_by: str = "replied") -> List[Dict[str, Any]]:
    customers = list(bot_service.db.get_all_customers() or [])
    grouped: Dict[str, Dict[str, Any]] = {}
    for customer in customers:
        status = (customer.get("status") or "").strip().lower()
        if status not in {"sent", "replied"}:
            continue
        customer_name = _resolve_customer_display_name(customer)
        if not customer_name:
            continue

        stats = grouped.setdefault(
            customer_name,
            {
                "customer_name": customer_name,
                "total": 0,
                "delivered": 0,
                "read": 0,
                "replied": 0,
                "_last_time": datetime.min,
            },
        )
        stats["total"] += 1
        stats["delivered"] += 1
        if status == "replied":
            stats["read"] += 1
            stats["replied"] += 1

        event_time = _parse_analytics_datetime(customer.get("updated_at") or customer.get("created_at"))
        if event_time and event_time > stats["_last_time"]:
            stats["_last_time"] = event_time

    sort_key = sort_by if sort_by in {"replied", "read", "delivered", "total"} else "replied"
    ranked = sorted(
        grouped.values(),
        key=lambda item: (item.get(sort_key, 0), item.get("_last_time", datetime.min)),
        reverse=True,
    )

    return [
        {
            "customer_name": item["customer_name"],
            "total": item["total"],
            "delivered": item["delivered"],
            "read": item["read"],
            "replied": item["replied"],
            "reply_rate": round(item["replied"] / max(item["delivered"], 1) * 100, 1),
        }
        for item in ranked[: max(int(limit or 10), 1)]
    ]

# WebSocket 连接管理器
class ConnectionManager:
    """WebSocket 连接管理器 - 用于实时推送"""

    MAX_CONNECTIONS = 50

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._lock = threading.Lock()

    async def connect(self, websocket: WebSocket):
        """接受新的 WebSocket 连接，限制最大连接数"""
        if len(self.active_connections) >= self.MAX_CONNECTIONS:
            await websocket.close(code=1013, reason="连接数已达上限")
            logger.warning(f"WebSocket 连接被拒绝，已达上限: {self.MAX_CONNECTIONS}")
            return False
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket 连接建立，当前连接数: {len(self.active_connections)}")
        return True

    def disconnect(self, websocket: WebSocket):
        """断开 WebSocket 连接"""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"WebSocket 连接断开，当前连接数: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """广播消息给所有连接，自动清理断开的连接（使用快照避免遍历中修改）"""
        disconnected = []
        connections_snapshot = list(self.active_connections)
        for connection in connections_snapshot:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"广播消息失败: {e}")
                disconnected.append(connection)
        for conn in disconnected:
            if conn in self.active_connections:
                self.active_connections.remove(conn)
        if disconnected:
            logger.info(f"清理了 {len(disconnected)} 个断开的WebSocket连接")

    async def send_to_client(self, websocket: WebSocket, message: dict):
        """发送消息给指定客户端，失败时自动清理断开的连接"""
        try:
            await websocket.send_json(message)
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

# 全局连接管理器
manager = ConnectionManager()

app = FastAPI(title="Douyin Bot Manager")


@app.on_event("startup")
async def _warmup_ollama_on_startup():
    """[PERF-INST:reply-latency] 启动时预热 Ollama。

    实测 4b 模型在 CPU 上首次推理耗时 31~47s（冷启动 + 1577 字符 prompt）。
    启动时跑一次 5 字符"hi"推理，**消除冷启动开销**，让首条业务 LLM 调用降到 10~25s。
    失败不影响服务启动。
    """
    import asyncio
    try:
        from src.common.utils import call_llm_safe
        from src.common.llm_service import get_default_llm_provider

        def _do_warmup():
            try:
                provider = get_default_llm_provider()
                warmup_timeout = 90.0
                result = call_llm_safe(
                    provider,
                    "hi",
                    timeout=warmup_timeout,
                )
                logger.info(f"[PERF-WARMUP] Ollama 预热完成: result_len={len(str(result or ''))}")
            except Exception as warmup_exc:
                logger.warning(f"[PERF-WARMUP] Ollama 预热失败（不影响启动）: {warmup_exc}")

        loop = asyncio.get_event_loop()
        # 用 executor 跑，避免阻塞事件循环；最长 90s
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _do_warmup),
                timeout=90,
            )
        except asyncio.TimeoutError:
            logger.warning("[PERF-WARMUP] Ollama 预热超时（>90s），后台继续")
        except Exception as e:
            logger.debug(f"[PERF-WARMUP] 预热调度失败: {e}")
    except Exception as outer_exc:
        logger.debug(f"[PERF-WARMUP] 预热模块未就绪: {outer_exc}")


def _configure_enterprise_http_middlewares(target_app: FastAPI):
    config = get_config()

    # Keep the existing main page behavior stable: avoid strict CSP by default
    # because the current templates still rely on inline scripts/styles.
    target_app.add_middleware(
        SecurityHeadersMiddleware,
        enable_csp=False,
        x_frame_options="SAMEORIGIN",
    )
    target_app.add_middleware(
        LoggingMiddleware,
        exclude_paths=[
            "/health",
            "/metrics",
            "/favicon.ico",
            "/api/knowledge/upload",
            "/api/rag/upload",
            "/api/enterprise/upload",
        ],
    )
    target_app.add_middleware(ContentLengthFixMiddleware)
    target_app.add_middleware(RequestMiddleware)
    target_app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


_configure_enterprise_http_middlewares(app)

for router in get_all_routers():
    app.include_router(router)

# Mount static files
static_dir = str(get_static_dir())
if not Path(static_dir).exists():
    Path(static_dir).mkdir(parents=True, exist_ok=True)
    logger.warning(f"[启动] 静态文件目录不存在，已自动创建: {static_dir}")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Templates
templates_dir = str(get_templates_dir())
if not Path(templates_dir).exists():
    Path(templates_dir).mkdir(parents=True, exist_ok=True)
    logger.warning(f"[启动] 模板目录不存在，已自动创建: {templates_dir}")
templates = Jinja2Templates(directory=templates_dir)

# Initialize heavy services lazily to keep module import side-effect free.
# Pylance 通过顶部的 `if TYPE_CHECKING:` 类型注解看到真实 BotService 类型
# （消除"无法访问属性"警告）。运行时通过 _LazyServiceProxy 包装做惰性解析。
# 下面的 type: ignore[assignment] 是必须的：赋值语句右侧是 _LazyServiceProxy 实例，
# 不用 ignore 会让 Pylance 把 bot_service 推为 _LazyServiceProxy，覆盖上面的类型注解。
bot_service = _LazyServiceProxy(_get_bot_service)  # type: ignore[assignment]
marketing_tracking_service = _LazyServiceProxy(_get_marketing_tracking_service)  # type: ignore[assignment]
private_message_limit_service = _LazyServiceProxy(_get_private_message_limit_runtime_service)  # type: ignore[assignment]
setup_logger("web")

class SearchRequest(BaseModel):
    keyword: str
    lead_quota: Optional[int] = None
    max_videos: Optional[int] = None
    comment_keywords: str = ""
    auto_reply_enabled: bool = False
    reply_quota: Optional[int] = None
    reply_templates: Optional[List[str]] = None
    auto_comment_enabled: bool = False
    comment_templates: Optional[List[str]] = None
    platform: str = "douyin"
    comment_time_preset: str = ""
    comment_time_start: str = ""
    comment_time_end: str = ""
    skip_crawled: bool = True
    skip_existing_videos: bool = CRAWLER_SKIP_EXISTING_VIDEOS_DEFAULT
    crawl_priority: str = CRAWLER_QUEUE_PRIORITY_DEFAULT
    worker_threads: int = CRAWLER_WORKER_THREADS_DEFAULT
    reply_diagnostic_mode: bool = False
    def resolve_lead_quota(self) -> int:
        raw_value = self.lead_quota if self.lead_quota is not None else self.max_videos
        try:
            return max(int(raw_value or 5), 1)
        except (TypeError, ValueError):
            return 5

    def resolve_max_videos(self) -> int:
        try:
            return max(int(self.max_videos or 0), 0)
        except (TypeError, ValueError):
            return 0

    def resolve_reply_templates(self) -> List[str]:
        return [
            str(item or "").strip()
            for item in (self.reply_templates or self.comment_templates or [])
            if str(item or "").strip()
        ]

    def resolve_reply_quota(self) -> int:
        try:
            return max(int(self.reply_quota or 10), 1)
        except (TypeError, ValueError):
            return 10

    def resolve_auto_reply_enabled(self) -> bool:
        return bool(self.auto_reply_enabled or self.auto_comment_enabled)

class DebugCrawlVideoRequest(BaseModel):
    video_url: str
    comment_keywords: str = ""
    platform: str = "douyin"
    comment_time_start: str = ""
    comment_time_end: str = ""
    skip_crawled: bool = False

class SendRequest(BaseModel):
    message: Optional[str] = None
    messages: Optional[List[str]] = None
    count: int = 10


class InteractRequest(BaseModel):
    uids: List[str]
    content: Optional[str] = None
    comments: Optional[List[str]] = None
    platform: str = "douyin"


class PrivateMessageLimitConfigRequest(BaseModel):
    enabled: bool
    increment_cycle_days: Optional[int] = None
    initial_hourly_limit: Optional[int] = None
    initial_daily_limit: Optional[int] = None
    hourly_increment: Optional[int] = None
    daily_increment: Optional[int] = None
    max_hourly_limit: Optional[int] = None
    max_daily_limit: Optional[int] = None
    event_retention_days: Optional[int] = None
    first_installed_at: Optional[str] = None


class InteractLimitConfigRequest(BaseModel):
    enabled: bool
    min_interval_seconds: Optional[int] = None
    rolling_10m_limit: Optional[int] = None
    rolling_24h_limit: Optional[int] = None
    event_retention_days: Optional[int] = None


class SearchReplyLimitConfigRequest(BaseModel):
    enabled: bool
    rolling_10m_limit: Optional[int] = None
    rolling_1h_limit: Optional[int] = None
    rolling_24h_limit: Optional[int] = None
    event_retention_days: Optional[int] = None


class BrowserOpenUrlRequest(BaseModel):
    url: str


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return JSONResponse(status_code=204, content={})


async def _background_warmup_knowledge_retrieval(delay_seconds: float = 1.0):
    """服务启动后仅做轻量知识服务准备，不主动拉起向量/模型初始化。"""
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)

    def _warmup_sync():
        from src.common.unified_knowledge_service import get_unified_knowledge_service

        service = get_unified_knowledge_service()
        return {
            "success": True,
            "status": "prepared",
            "knowledge_count": len(getattr(service, "_items", []) or []),
            "vector_init_deferred": True,
        }

    try:
        result = await asyncio.to_thread(_warmup_sync)
        logger.info(f"知识服务轻量准备结果: {result}")
    except Exception as exc:
        logger.warning(f"知识服务轻量准备异常: {exc}")


async def _background_warmup_llm(delay_seconds: float = 2.0):
    """服务启动后后台预热本地 LLM，降低首条真实消息的冷启动耗时。"""
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)

    def _warmup_sync():
        from src.config.settings import (
            OLLAMA_NUM_CTX,
            OLLAMA_NUM_PREDICT,
            OLLAMA_TOP_K,
            OLLAMA_TOP_P,
            REPLY_LLM_TIMEOUT_SECONDS,
        )

        import requests

        warmup_enabled = str(os.getenv("ENABLE_LLM_STARTUP_WARMUP", "true") or "true").strip().lower() == "true"
        if not warmup_enabled:
            return {"success": False, "reason": "warmup_disabled"}

        ollama_host = str(os.getenv("OLLAMA_HOST", "http://localhost:11434") or "http://localhost:11434").strip()
        model_name = str(os.getenv("OLLAMA_MODEL", "qwen3.5:4b") or "qwen3.5:4b").strip()
        keep_alive = str(os.getenv("OLLAMA_KEEP_ALIVE", "24h") or "24h").strip()
        warmup_timeout = max(
            float(os.getenv("OLLAMA_WARMUP_TIMEOUT_SECONDS", "0") or 0.0),
            float(REPLY_LLM_TIMEOUT_SECONDS),
            20.0,
        )
        warmup_retries = max(1, int(os.getenv("OLLAMA_WARMUP_RETRIES", "2") or 2))
        num_predict = max(8, min(32, int(OLLAMA_NUM_PREDICT)))

        def _get_resident_models() -> list[str]:
            try:
                response = requests.get(f"{ollama_host}/api/ps", timeout=3)
                response.raise_for_status()
                payload = response.json() if response.content else {}
                models = payload.get("models") if isinstance(payload, dict) else []
                resident = []
                for item in models or []:
                    if not isinstance(item, dict):
                        continue
                    candidate = str(item.get("model") or item.get("name") or "").strip()
                    if candidate:
                        resident.append(candidate)
                return resident
            except Exception:
                return []

        resident_models = _get_resident_models()
        if model_name in resident_models:
            return {
                "success": True,
                "status": "already_warm",
                "model": model_name,
                "resident_models": resident_models,
            }

        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "Reply with OK only."}],
            "stream": False,
            "keep_alive": keep_alive,
            "think": False,
            "options": {
                "num_ctx": int(OLLAMA_NUM_CTX),
                "num_predict": num_predict,
                "temperature": 0.0,
                "top_p": float(OLLAMA_TOP_P),
                "top_k": int(OLLAMA_TOP_K),
                "enable_thinking": False,
                "thinking_depth": 0,
            },
        }

        last_error = ""
        for attempt in range(1, warmup_retries + 1):
            started_at = time.perf_counter()
            try:
                response = requests.post(
                    f"{ollama_host}/api/chat",
                    json=payload,
                    timeout=warmup_timeout,
                )
                response.raise_for_status()
                result = response.json() if response.content else {}
                resident_models = _get_resident_models()
                reply = ""
                if isinstance(result, dict):
                    reply = str(((result.get("message") or {}).get("content") or "")).strip()
                warm_elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
                load_duration_ms = round(float(result.get("load_duration", 0) or 0) / 1_000_000, 1) if isinstance(result, dict) else 0.0
                total_duration_ms = round(float(result.get("total_duration", 0) or 0) / 1_000_000, 1) if isinstance(result, dict) else 0.0
                if model_name in resident_models or reply:
                    return {
                        "success": True,
                        "status": "warmed",
                        "model": model_name,
                        "attempt": attempt,
                        "elapsed_ms": warm_elapsed_ms,
                        "load_duration_ms": load_duration_ms,
                        "total_duration_ms": total_duration_ms,
                        "reply": reply[:32],
                        "resident_models": resident_models,
                    }
                last_error = "warmup_completed_but_model_not_resident"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(min(2.0 * attempt, 5.0))

        return {
            "success": False,
            "status": "failed",
            "model": model_name,
            "error": last_error,
            "resident_models": _get_resident_models(),
            "timeout_seconds": warmup_timeout,
            "retries": warmup_retries,
        }

    try:
        result = await asyncio.to_thread(_warmup_sync)
        _set_model_warmup_state("llm", result)
        logger.info(f"LLM后台预热结果: {result}")
    except Exception as exc:
        _set_model_warmup_state("llm", {"success": False, "status": "exception", "error": str(exc)})
        logger.warning(f"LLM后台预热异常: {exc}")


async def _background_warmup_embedding(delay_seconds: float = 2.5):
    """服务启动后后台预热 Embedding，降低首条向量检索/索引的冷启动耗时。"""
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)

    def _warmup_sync():
        warmup_enabled = str(os.getenv("ENABLE_EMBEDDING_STARTUP_WARMUP", "true") or "true").strip().lower() == "true"
        if not warmup_enabled:
            return {"success": False, "reason": "warmup_disabled"}

        warmup_text = str(os.getenv("EMBEDDING_WARMUP_TEXT", "embedding warmup probe") or "embedding warmup probe").strip()
        warmup_timeout = max(float(os.getenv("EMBEDDING_WARMUP_TIMEOUT_SECONDS", "20") or 20.0), 5.0)
        warmup_retries = max(1, int(os.getenv("EMBEDDING_WARMUP_RETRIES", "1") or 1))

        from src.rag.embedding_service import get_embedding_service

        last_error = ""
        for attempt in range(1, warmup_retries + 1):
            started_at = time.perf_counter()
            try:
                service = get_embedding_service()

                def _embed_once():
                    return service.embedSingle(warmup_text)

                vector = concurrent.futures.ThreadPoolExecutor(max_workers=1).submit(_embed_once).result(timeout=warmup_timeout)
                runtime_contract = service.get_runtime_contract()
                elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
                vector_dim = int(getattr(vector, "shape", [len(vector)])[0]) if vector is not None else 0
                return {
                    "success": True,
                    "status": "warmed",
                    "attempt": attempt,
                    "elapsed_ms": elapsed_ms,
                    "vector_dimension": vector_dim,
                    "contract": runtime_contract,
                }
            except Exception as exc:
                last_error = str(exc)
                time.sleep(min(1.5 * attempt, 4.0))

        return {
            "success": False,
            "status": "failed",
            "error": last_error,
            "timeout_seconds": warmup_timeout,
            "retries": warmup_retries,
        }

    try:
        result = await asyncio.to_thread(_warmup_sync)
        _set_model_warmup_state("embedding", result)
        logger.info(f"Embedding后台预热结果: {result}")
    except Exception as exc:
        _set_model_warmup_state("embedding", {"success": False, "status": "exception", "error": str(exc)})
        logger.warning(f"Embedding后台预热异常: {exc}")


async def periodic_remote_policy_refresh():
    service = get_remote_control_service()
    while True:
        try:
            await asyncio.sleep(service.get_refresh_interval_seconds())
            if service.get_policy_url():
                snapshot = await asyncio.to_thread(service.refresh_from_server)
                await asyncio.to_thread(
                    service.enforce_runtime_limits,
                    bot_service,
                    _get_process_manager(),
                )
                if snapshot.enabled:
                    logger.info(
                        "远程策略刷新成功: "
                        f"app={snapshot.app_enabled}, crawler={snapshot.crawler_enabled}, "
                        f"monitor={snapshot.monitor_enabled}, auto_reply={snapshot.auto_reply_enabled}"
                    )
        except Exception as exc:
            logger.warning(f"远程策略刷新失败: {exc}")

@app.on_event("startup")
async def startup_event():
    # Auto start browser on startup - DISABLED
    # bot_service.start_browser()
    # 启动后台任务：定期推送意向客户更新
    asyncio.create_task(periodic_intent_push())
    asyncio.create_task(_background_warmup_knowledge_retrieval())
    asyncio.create_task(_background_warmup_llm())
    asyncio.create_task(_background_warmup_embedding())
    asyncio.create_task(periodic_remote_policy_refresh())

    # 初始化监控模块
    try:
        from src.common.monitoring import init_metrics
        metrics = init_metrics()
        metrics.start_metrics_server(port=PROMETHEUS_METRICS_PORT)
        logger.info(f"监控模块初始化成功，Prometheus端口: {PROMETHEUS_METRICS_PORT}")
    except Exception as e:
        logger.warning(f"监控模块初始化失败: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    """关闭时清理所有资源"""
    try:
        from src.common.process_manager import shutdown_process_manager_for_exit

        shutdown_process_manager_for_exit()
    except Exception:
        pass
    bot_service.stop_browser(preserve_monitoring_intent=True)
    try:
        import src.common.enterprise.message_bus as mb
        _bus_getter = getattr(mb, 'get_message_bus', None)
        if _bus_getter:
            _bus_getter().stop()
    except Exception:
        pass
    try:
        import src.common.enterprise.config_manager as cm
        _mgr_getter = getattr(cm, 'get_config_manager', None)
        if _mgr_getter:
            _mgr_getter().stop()
    except Exception:
        pass

# ========== WebSocket 实时推送 ==========

@app.websocket("/ws/intent-updates")
async def websocket_intent_updates(websocket: WebSocket):
    """
    WebSocket 端点 - 实时推送意向客户更新
    
    推送内容：
    - 新增高意向客户通知
    - 客户意向等级变化
    - 待跟进提醒
    """
    connected = await manager.connect(websocket)
    if not connected:
        return
    try:
        # 发送初始数据
        initial_data = await get_initial_intent_data()
        await websocket.send_json({
            "type": "initial",
            "data": initial_data,
            "timestamp": datetime.now().isoformat()
        })
        
        while True:
            # 接收客户端消息（心跳或请求）
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                message = json.loads(data)
                
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": datetime.now().isoformat()})
                elif message.get("type") == "refresh":
                    # 客户端请求刷新数据
                    fresh_data = await get_initial_intent_data()
                    await websocket.send_json({
                        "type": "refresh",
                        "data": fresh_data,
                        "timestamp": datetime.now().isoformat()
                    })
            except asyncio.TimeoutError:
                # 超时，发送心跳
                await websocket.send_json({"type": "heartbeat", "timestamp": datetime.now().isoformat()})
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
        manager.disconnect(websocket)

async def get_initial_intent_data() -> dict:
    """获取初始意向数据"""
    try:
        # 获取高意向客户
        db = bot_service.db
        chat_store = bot_service._get_chat_store()
        high_intent = db.get_high_intent_conversations(min_score=50)
        
        # 获取热线索
        hot_leads = bot_service.get_hot_leads()
        
        # 获取待办事项
        conversations = list(chat_store.get_all_conversations_dicts() or [])
        all_messages = list(chat_store.get_all_messages_dicts() or [])
        messages_by_conversation: Dict[str, List[Dict[str, Any]]] = {}
        for message in all_messages:
            conversation_id = str(message.get("conversation_id") or "").strip()
            if not conversation_id:
                continue
            messages_by_conversation.setdefault(conversation_id, []).append(message)
        action_items_result = get_follow_up_reminder_service().build_action_items(
            conversations,
            messages_by_conversation,
            notify=False,
        )
        
        return {
            "high_intent_customers": [
                {
                    "customer_name": conv.get("customer_name", ""),
                    "purchase_intent_score": conv.get("purchase_intent_score", 0),
                    "lead_score": conv.get("lead_score", "cold"),
                    "purchase_probability": conv.get("purchase_probability", 0),
                    "last_message_time": conv.get("last_message_time", "")
                }
                for conv in high_intent[:10]
            ],
            "hot_leads": hot_leads.get("leads", [])[:5],
            "pending_follow_ups_count": len(action_items_result),
            "summary": {
                "high_intent_count": len(high_intent),
                "hot_leads_count": hot_leads.get("count", 0)
            }
        }
    except Exception as e:
        logger.error(f"获取初始意向数据失败: {e}")
        return {}

async def periodic_intent_push():
    """
    定期推送意向客户更新
    
    每30秒检查一次，有变化时推送
    """
    last_state = {}
    
    while True:
        try:
            await asyncio.sleep(30)  # 每30秒检查一次
            
            if not manager.active_connections:
                continue
            
            # 获取当前状态
            db = bot_service.db
            high_intent = await asyncio.to_thread(db.get_high_intent_conversations, min_score=50)
            
            current_state = {
                "high_intent_count": len(high_intent),
                "top_customers": [
                    (c.get("customer_name"), c.get("purchase_intent_score", 0))
                    for c in high_intent[:5]
                ]
            }
            
            # 检查是否有变化
            if current_state != last_state:
                # 有变化，推送更新
                update_data = {
                    "type": "intent_update",
                    "data": await get_initial_intent_data(),
                    "changes": detect_changes(last_state, current_state),
                    "timestamp": datetime.now().isoformat()
                }
                
                await manager.broadcast(update_data)
                last_state = current_state
                
        except Exception as e:
            logger.error(f"定期推送失败: {e}")


def detect_changes(old_state: dict, new_state: dict) -> dict:
    """检测状态变化"""
    changes = {
        "new_high_intent": [],
        "score_increased": [],
        "score_decreased": []
    }
    
    old_customers = {c[0]: c[1] for c in old_state.get("top_customers", [])}
    new_customers = {c[0]: c[1] for c in new_state.get("top_customers", [])}
    
    # 检测新增的高意向客户
    for name, score in new_customers.items():
        if name not in old_customers:
            changes["new_high_intent"].append({"name": name, "score": score})
        elif score > old_customers.get(name, 0):
            changes["score_increased"].append({"name": name, "old_score": old_customers[name], "new_score": score})
        elif score < old_customers.get(name, 0):
            changes["score_decreased"].append({"name": name, "old_score": old_customers[name], "new_score": score})
    
    return changes

# ========== 实时通知 API ==========

@app.post("/api/notify/high-intent")
async def notify_high_intent_customer(customer_name: str, score: float):
    """
    通知高意向客户（内部调用）
    
    当检测到高意向客户时，通过 WebSocket 推送通知
    """
    notification = {
        "type": "high_intent_alert",
        "data": {
            "customer_name": customer_name,
            "score": score,
            "timestamp": datetime.now().isoformat(),
            "message": f"发现高意向客户: {customer_name} (评分: {score:.1f})"
        }
    }
    
    await manager.broadcast(notification)
    return {"success": True, "message": "通知已发送"}

@app.get("/api/realtime/intent-summary")
async def get_realtime_intent_summary():
    """
    获取实时意向客户摘要
    
    返回当前意向客户的实时状态
    """
    try:
        db = bot_service.db
        
        # 获取各等级客户数量
        hot_count = len(db.get_conversations_by_lead_score("hot"))
        warm_count = len(db.get_conversations_by_lead_score("warm"))
        cool_count = len(db.get_conversations_by_lead_score("cool"))
        cold_count = len(db.get_conversations_by_lead_score("cold"))
        
        # 获取高意向客户列表
        high_intent = db.get_high_intent_conversations(min_score=70)
        
        # 获取待跟进数量
        pending = db.get_pending_follow_ups()
        
        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "summary": {
                "hot": hot_count,
                "warm": warm_count,
                "cool": cool_count,
                "cold": cold_count,
                "total": hot_count + warm_count + cool_count + cold_count
            },
            "high_intent_customers": [
                {
                    "customer_name": c.get("customer_name", ""),
                    "score": c.get("purchase_intent_score", 0),
                    "probability": c.get("purchase_probability", 0),
                    "lead_score": c.get("lead_score", "cold"),
                    "last_message": c.get("last_message_content", "")[:50],
                    "signals": c.get("signals_detected", [])
                }
                for c in high_intent
            ],
            "pending_follow_ups": len(pending)
        }
        
    except Exception as e:
        logger.error(f"获取实时摘要失败: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})

@app.get("/")
async def read_root(request: Request):
    logger.debug(f"Root route called, request: {request}")
    license_payload = json.dumps(
        get_license_service().get_state_snapshot().model_dump(mode="json"),
        ensure_ascii=False,
    )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "license_payload_json": license_payload,
        },
    )

@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/license-generator")
async def license_generator_page(request: Request):
    """火客验证码生成器页面"""
    return templates.TemplateResponse(
        request=request,
        name="license_generator.html",
        context={},
    )


@app.get("/license-manager")
async def license_manager_page(request: Request):
    """火客授权验证码管理系统"""
    return templates.TemplateResponse(
        request=request,
        name="license_manager.html",
        context={},
    )

@app.get("/api/status")
async def get_status():
    now = time.monotonic()
    with _status_cache_lock:
        cached_payload = _status_cache.get("payload")
        cached_timestamp = _status_cache.get("timestamp", 0.0)
        if cached_payload is not None and (now - cached_timestamp) < _status_cache_ttl_seconds:
            return cached_payload

    loop = asyncio.get_event_loop()
    status = await loop.run_in_executor(None, bot_service.get_status)
    status = _merge_crawler_process_status(status)
    status = _apply_effective_login_state(status)
    status["model_warmup"] = _get_model_warmup_snapshot()
    try:
        from src.common.search_reply_limit_service import get_search_reply_limit_service

        status["search_reply_limit"] = get_search_reply_limit_service().get_status()
    except Exception as exc:
        logger.warning(f"获取评论下回复频控状态失败: {exc}")
        status["search_reply_limit"] = {}

    with _status_cache_lock:
        _status_cache["timestamp"] = now
        _status_cache["payload"] = status
    return status


@app.get("/api/agent/status")
async def get_agent_status():
    """获取双Agent进程状态"""
    try:
        return {"success": True, "data": _apply_effective_login_state(_merge_crawler_process_status(bot_service.get_status()))}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/sync/progress")
async def get_sync_progress():
    """
    获取同步进度信息
    
    Returns:
        dict: {
            is_syncing: bool,  # 是否正在同步
            current_task: str, # 当前任务名称
            progress: {        # 进度信息
                total: int,
                current: int,
                detail: str
            }
        }
    """
    status = bot_service.get_status()
    return {
        "is_syncing": status.get("current_task", "").startswith("同步") or status.get("current_task", "") == "全量同步中",
        "current_task": status.get("current_task", ""),
        "progress": status.get("progress", {"total": 0, "current": 0, "detail": ""})
    }

@app.post("/api/search")
async def run_search(request: SearchRequest):
    # 阶段A·F-4：与 /api/send 对齐，生成 trace_id 并贯穿到 startup_trace
    trace_id = datetime.now().strftime("search_%Y%m%d_%H%M%S_%f")
    _record_task_startup_trace(
        "search_request_received",
        {
            "trace_id": trace_id,
            "keyword": str(request.keyword or ""),
            "lead_quota": int(request.resolve_lead_quota() or 0),
            "platform": str(request.platform or ""),
            "auto_reply_enabled": bool(request.resolve_auto_reply_enabled()),
        },
    )
    blocked = _require_runtime_feature("crawler")
    if blocked:
        _record_task_startup_trace(
            "search_request_blocked_by_runtime_feature",
            {"trace_id": trace_id, "status_code": 403},
        )
        return blocked

    ready, error_message = _ensure_crawler_process_ready(source="search")
    if not ready:
        _record_task_startup_trace(
            "search_request_ready_failed",
            {"trace_id": trace_id, "error_message": error_message},
        )
        return JSONResponse(status_code=400, content={"message": error_message})

    busy_response = _validate_crawler_idle_for_new_task()
    if busy_response is not None:
        _record_task_startup_trace(
            "search_request_rejected_busy",
            {
                "trace_id": trace_id,
                "crawler_status": dict(_get_crawler_process_snapshot() or {}),
            },
        )
        return busy_response

    lead_quota = request.resolve_lead_quota()
    reply_templates = request.resolve_reply_templates()
    reply_quota = request.resolve_reply_quota()
    auto_reply_requested = request.resolve_auto_reply_enabled()
    auto_reply_enabled = bool(auto_reply_requested and reply_templates)
    if auto_reply_requested and not reply_templates:
        _record_task_startup_trace(
            "search_request_rejected_no_reply_templates",
            {"trace_id": trace_id},
        )
        return JSONResponse(status_code=400, content={"message": "已开启在评论下直接回复，但未提供可用回复内容"})
    if auto_reply_enabled:
        from src.common.search_reply_limit_service import get_search_reply_limit_service

        quota_scope = _get_runtime_quota_scope()
        reply_limit_decision = get_search_reply_limit_service().check_send_allowed(
            account_id=str(quota_scope.get("reply_scope_id") or "").strip()
        )
        if not reply_limit_decision.allowed:
            _invalidate_status_cache()
            _record_task_startup_trace(
                "search_request_rejected_reply_limit",
                {
                    "trace_id": trace_id,
                    "message": reply_limit_decision.message,
                    "status_code": str(reply_limit_decision.status_code or ""),
                },
            )
            return JSONResponse(
                status_code=400,
                content={
                    "message": reply_limit_decision.message,
                    "reason": str(reply_limit_decision.status_code or ""),
                    "data": {
                        "search_reply_limit": dict(getattr(reply_limit_decision, "details", {}) or {}),
                    },
                },
            )
    login_transfer = _build_crawler_login_transfer_payload(force_scope_refresh=False)
    success = _get_process_manager().send_crawler_command(
        "search",
        build_search_command_payload(
            keyword=request.keyword,
            lead_quota=lead_quota,
            max_videos=request.resolve_max_videos(),
            comment_keywords=request.comment_keywords,
            auto_reply_enabled=auto_reply_enabled,
            reply_quota=reply_quota,
            reply_templates=reply_templates,
            platform=request.platform,
            comment_time_preset=request.comment_time_preset,
            comment_time_start=request.comment_time_start,
            comment_time_end=request.comment_time_end,
            skip_crawled=request.skip_crawled,
            skip_existing_videos=request.skip_existing_videos,
            crawl_priority=request.crawl_priority,
            worker_threads=request.worker_threads,
            reply_diagnostic_mode=bool(request.reply_diagnostic_mode),
            login_transfer=login_transfer,
        ),
    )
    _record_task_startup_trace(
        "search_request_command_sent",
        {
            "trace_id": trace_id,
            "success": bool(success),
            "max_count": int(lead_quota or 0),
        },
    )
    if not success:
        return JSONResponse(status_code=400, content={"message": "搜索任务启动失败，独立爬取/发送进程未接收命令"})
    _invalidate_status_cache()
    return {
        "success": True,
        "message": "搜索任务已在独立进程中开始",
        "data": {"trace_id": trace_id},
    }

@app.get("/api/crawl-limit/status", summary="查询每日爬取限制状态")
async def get_crawl_limit_status():
    from src.common.crawl_limit_service import get_crawl_limit_service
    service = get_crawl_limit_service()
    return service.check_limit()

@app.get("/api/crawl-limit/config", summary="获取每日爬取限制配置")
async def get_crawl_limit_config(request: Request):
    require_license_admin(request)
    from src.common.crawl_limit_service import get_crawl_limit_service
    service = get_crawl_limit_service()
    return service.get_config()

@app.post("/api/crawl-limit/config", summary="更新每日爬取限制配置")
async def update_crawl_limit_config(request: Request):
    require_license_admin(request)
    try:
        data = await request.json()
        from src.common.crawl_limit_service import get_crawl_limit_service
        service = get_crawl_limit_service()
        success = service.update_config(data)
        if success:
            return {"success": True, "config": service.get_config()}
        return {"success": False, "error": "配置保存失败"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/send-limit/config", summary="获取自动私信发送限制配置")
async def get_send_limit_config(request: Request):
    require_license_admin(request)
    scope = _get_send_limit_scope()
    status = private_message_limit_service.get_status(account_id=scope["effective_account_id"])
    return {
        "success": True,
        "message": "自动私信发送限制配置获取成功",
        "data": {
            **status,
            "account_scope": {
                **dict(status.get("account_scope") or {}),
                "account_id": scope["account_id"],
                "resolved": scope["resolved"],
                "source": scope["source"],
            },
        },
        "doc": private_message_limit_service.export_api_doc(),
    }


@app.post("/api/send-limit/config", summary="保存自动私信发送限制配置")
async def save_send_limit_config(config_request: Request, request: PrivateMessageLimitConfigRequest):
    require_license_admin(config_request)
    saved = private_message_limit_service.save_config(request.model_dump())
    scope = _get_send_limit_scope()
    status = private_message_limit_service.get_status(account_id=scope["effective_account_id"])
    return {
        "success": True,
        "message": "自动私信发送限制配置已保存",
        "data": {
            "config": saved,
            "status": {
                **status,
                "account_scope": {
                    **dict(status.get("account_scope") or {}),
                    "account_id": scope["account_id"],
                    "resolved": scope["resolved"],
                    "source": scope["source"],
                },
            },
        },
    }


@app.get("/api/send-limit/status", summary="查询自动私信发送限制状态")
async def get_send_limit_status():
    scope = _get_send_limit_scope()
    return {
        "success": True,
        "message": "自动私信发送限制状态获取成功",
        "data": {
            **private_message_limit_service.get_status(account_id=scope["effective_account_id"]),
            "account_scope": {
                "account_id": scope["account_id"],
                "resolved": scope["resolved"],
                "source": scope["source"],
            },
        },
    }


@app.get("/api/interact-limit/config", summary="获取一键互动限制配置")
async def get_interact_limit_config(request: Request):
    require_license_admin(request)
    from src.common.interact_comment_limit_service import get_interact_comment_limit_service

    service = get_interact_comment_limit_service()
    scope = _get_runtime_quota_scope()
    status = service.get_status(account_id=str(scope.get("interact_scope_id") or "").strip())
    return {
        "success": True,
        "message": "一键互动限制配置获取成功",
        "data": {
            "config": service.load_config(),
            "status": {
                **status,
                "account_scope": {
                    **dict(status.get("account_scope") or {}),
                    "resolved": bool(scope.get("resolved", False)),
                    "source": str(scope.get("source") or "").strip(),
                },
            },
        },
        "doc": service.export_api_doc(),
    }


@app.post("/api/interact-limit/config", summary="保存一键互动限制配置")
async def save_interact_limit_config(config_request: Request, request: InteractLimitConfigRequest):
    require_license_admin(config_request)
    from src.common.interact_comment_limit_service import get_interact_comment_limit_service

    service = get_interact_comment_limit_service()
    saved = service.save_config(request.model_dump())
    scope = _get_runtime_quota_scope()
    status = service.get_status(account_id=str(scope.get("interact_scope_id") or "").strip())
    return {
        "success": True,
        "message": "一键互动限制配置已保存",
        "data": {
            "config": saved,
            "status": {
                **status,
                "account_scope": {
                    **dict(status.get("account_scope") or {}),
                    "resolved": bool(scope.get("resolved", False)),
                    "source": str(scope.get("source") or "").strip(),
                },
            },
        },
    }


@app.get("/api/interact-limit/status", summary="查询一键互动限制状态")
async def get_interact_limit_status():
    from src.common.interact_comment_limit_service import get_interact_comment_limit_service

    service = get_interact_comment_limit_service()
    scope = _get_runtime_quota_scope()
    status = service.get_status(account_id=str(scope.get("interact_scope_id") or "").strip())
    return {
        "success": True,
        "message": "一键互动限制状态获取成功",
        "data": {
            **status,
            "account_scope": {
                **dict(status.get("account_scope") or {}),
                "resolved": bool(scope.get("resolved", False)),
                "source": str(scope.get("source") or "").strip(),
            },
        },
    }


@app.get("/api/search-reply-limit/config", summary="获取评论下直接回复限制配置")
async def get_search_reply_limit_config(request: Request):
    require_license_admin(request)
    from src.common.search_reply_limit_service import get_search_reply_limit_service

    service = get_search_reply_limit_service()
    scope = _get_runtime_quota_scope()
    status = service.get_status(account_id=str(scope.get("reply_scope_id") or "").strip())
    return {
        "success": True,
        "message": "评论下直接回复限制配置获取成功",
        "data": {
            "config": service.load_config(),
            "status": {
                **status,
                "account_scope": {
                    **dict(status.get("account_scope") or {}),
                    "resolved": bool(scope.get("resolved", False)),
                    "source": str(scope.get("source") or "").strip(),
                },
            },
        },
        "doc": service.export_api_doc(),
    }


@app.post("/api/search-reply-limit/config", summary="保存评论下直接回复限制配置")
async def save_search_reply_limit_config(config_request: Request, request: SearchReplyLimitConfigRequest):
    require_license_admin(config_request)
    from src.common.search_reply_limit_service import get_search_reply_limit_service

    service = get_search_reply_limit_service()
    saved = service.save_config(request.model_dump())
    scope = _get_runtime_quota_scope()
    status = service.get_status(account_id=str(scope.get("reply_scope_id") or "").strip())
    _invalidate_status_cache()
    return {
        "success": True,
        "message": "评论下直接回复限制配置已保存",
        "data": {
            "config": saved,
            "status": {
                **status,
                "account_scope": {
                    **dict(status.get("account_scope") or {}),
                    "resolved": bool(scope.get("resolved", False)),
                    "source": str(scope.get("source") or "").strip(),
                },
            },
        },
    }


@app.get("/api/search-reply-limit/status", summary="查询评论下直接回复限制状态")
async def get_search_reply_limit_status():
    from src.common.search_reply_limit_service import get_search_reply_limit_service

    service = get_search_reply_limit_service()
    scope = _get_runtime_quota_scope()
    status = service.get_status(account_id=str(scope.get("reply_scope_id") or "").strip())
    return {
        "success": True,
        "message": "评论下直接回复限制状态获取成功",
        "data": {
            **status,
            "account_scope": {
                **dict(status.get("account_scope") or {}),
                "resolved": bool(scope.get("resolved", False)),
                "source": str(scope.get("source") or "").strip(),
            },
        },
    }


@app.post("/api/send", summary="启动自动私信发送任务")
async def run_send(request: SendRequest):
    trace_id = datetime.now().strftime("send_%Y%m%d_%H%M%S_%f")
    _record_task_startup_trace(
        "send_request_received",
        {
            "trace_id": trace_id,
            "count": int(request.count or 0),
            "message_count": len(request.messages or []),
            "message_preview": str((request.message or "")[:120]),
        },
    )
    blocked = _require_runtime_feature("crawler")
    if blocked:
        _record_task_startup_trace(
            "send_request_blocked_by_runtime_feature",
            {
                "trace_id": trace_id,
                "status_code": 403,
            },
        )
        return blocked
    account_ready, account_message, runtime = _validate_browser_runtime_for_task(
        force_refresh=True,
        action_label="启动私信任务",
        require_resolved_login=True,
    )
    if not account_ready:
        _record_task_startup_trace(
            "send_request_rejected_account_context",
            {
                "trace_id": trace_id,
                "message": account_message,
            },
        )
        return JSONResponse(status_code=400, content={"message": account_message})
    ready, error_message = _ensure_crawler_process_ready()
    if not ready:
        _record_task_startup_trace(
            "send_request_ready_failed",
            {
                "trace_id": trace_id,
                "error_message": error_message,
            },
        )
        return JSONResponse(status_code=400, content={"message": error_message})

    busy_response = _validate_crawler_idle_for_new_task()
    if busy_response is not None:
        crawler_status = _get_crawler_process_snapshot()
        _record_task_startup_trace(
            "send_request_rejected_busy_guard",
            {
                "trace_id": trace_id,
                "crawler_status": dict(crawler_status or {}),
            },
        )
        return busy_response

    scope = _get_send_limit_scope()
    try:
        decision = private_message_limit_service.check_send_allowed(
            request.count,
            account_id=scope["effective_account_id"],
        )
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc or ""):
            raise
        decision = private_message_limit_service.check_send_allowed(request.count)
    decision_allowed = bool(getattr(decision, "allowed", False))
    decision_status_code = str(getattr(decision, "status_code", "") or "")
    decision_message = str(getattr(decision, "message", "") or "")
    decision_details = getattr(decision, "details", {}) or {}
    if not isinstance(decision_details, dict):
        try:
            decision_details = dict(decision_details)
        except Exception:
            decision_details = {}
    _record_task_startup_trace(
        "send_request_limit_checked",
        {
            "trace_id": trace_id,
            "allowed": decision_allowed,
            "status_code": decision_status_code,
            "account_scope": dict(scope or {}),
            "details": dict(decision_details or {}),
        },
    )
    if not decision_allowed and decision_status_code not in {"hourly_limit_exceeded", "daily_limit_exceeded"}:
        _record_task_startup_trace(
            "send_request_rejected_limit",
            {
                "trace_id": trace_id,
                "message": decision_message,
                "status_code": decision_status_code,
            },
        )
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "message": decision_message,
                "reason": decision_status_code,
                "data": decision_details,
            },
        )

    if not request.messages:
        # 阶段A·F-1：消除双路径——空 messages 走旧 bot_service 路径会绕过子进程状态机/限流/停止信号
        # 强制要求 messages 非空，统一走独立爬取/发送进程链路
        _record_task_startup_trace(
            "send_request_rejected_no_messages",
            {
                "trace_id": trace_id,
                "reason": "messages_required",
            },
        )
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "message": "请至少提供一条私信内容（messages 字段不可为空）",
                "reason": "messages_required",
            },
        )

    login_transfer = _build_crawler_login_transfer_payload(force_scope_refresh=False)
    success = _get_process_manager().send_crawler_command(
        "send_messages",
        build_send_messages_payload(
            message=request.message,
            messages=request.messages,
            max_count=request.count,
            login_transfer=login_transfer,
        ),
    )
    _record_task_startup_trace(
        "send_request_command_sent",
        {
            "trace_id": trace_id,
            "success": bool(success),
            "mode": "crawler_process",
            "max_count": int(request.count or 0),
        },
    )
    if not success:
        return JSONResponse(status_code=400, content={"message": "发送任务启动失败，独立爬取/发送进程未接收命令"})
    _invalidate_status_cache()
    accepted_with_resume = False
    manual_resume_required = not decision_allowed
    response_payload = {
        "success": True,
        "message": (
            "发送任务已在独立进程中开始"
            if not manual_resume_required
            else "发送任务已启动；当前私信额度不足时不会自动续发，请在额度恢复后手动继续剩余私信"
        ),
        "data": {
            "send_limit": {
                **dict(decision_details or {}),
                "account_scope": {
                    "account_id": scope["account_id"],
                    "resolved": scope["resolved"],
                    "source": scope["source"],
                },
            },
            "accepted_with_auto_resume": accepted_with_resume,
            "manual_resume_required": manual_resume_required,
        },
    }
    _record_task_startup_trace(
        "send_request_response_ok",
        {
            "trace_id": trace_id,
            "response": response_payload,
        },
    )
    return response_payload


@app.post("/api/interact", summary="启动一键互动任务 (关注+点赞+评论)")
async def run_interact(request: InteractRequest):
    # 阶段B·B-1：trace_id 全链路
    trace_id = datetime.now().strftime("interact_%Y%m%d_%H%M%S_%f")
    _record_task_startup_trace(
        "interact_request_received",
        {
            "trace_id": trace_id,
            "uids_count": len(request.uids or []),
            "platform": str(request.platform or ""),
        },
    )
    blocked = _require_runtime_feature("crawler")
    if blocked:
        _record_task_startup_trace(
            "interact_request_blocked_by_runtime_feature",
            {"trace_id": trace_id, "status_code": 403},
        )
        return blocked
    account_ready, account_message, runtime = _validate_browser_runtime_for_task(
        force_refresh=True,
        action_label="启动一键互动",
        require_resolved_login=True,
    )
    if not account_ready:
        _record_task_startup_trace(
            "interact_request_account_not_ready",
            {"trace_id": trace_id, "message": account_message},
        )
        return JSONResponse(status_code=400, content={"message": account_message})

    ready, error_message = _ensure_crawler_process_ready()
    if not ready:
        _record_task_startup_trace(
            "interact_request_ready_failed",
            {"trace_id": trace_id, "error_message": error_message},
        )
        return JSONResponse(status_code=400, content={"message": error_message})

    busy_response = _validate_crawler_idle_for_new_task()
    if busy_response is not None:
        _record_task_startup_trace(
            "interact_request_rejected_busy",
            {"trace_id": trace_id},
        )
        return busy_response

    if not request.uids:
        _record_task_startup_trace(
            "interact_request_rejected_no_uids",
            {"trace_id": trace_id},
        )
        return JSONResponse(status_code=400, content={"message": "未选中任何客户"})

    comments = [str(item or "").strip() for item in (request.comments or []) if str(item or "").strip()]
    if not comments and str(request.content or "").strip():
        comments.append(str(request.content or "").strip())
    if not comments:
        return JSONResponse(status_code=400, content={"message": "请至少提供一条评论内容"})

    from src.common.interact_comment_limit_service import get_interact_comment_limit_service

    quota_scope = _get_runtime_quota_scope()
    interact_limit_decision = get_interact_comment_limit_service().check_send_allowed(
        account_id=str(quota_scope.get("interact_scope_id") or "").strip()
    )
    if (
        not interact_limit_decision.allowed
        and interact_limit_decision.status_code == "daily_limit_exceeded"
    ):
        return JSONResponse(
            status_code=400,
            content={
                "message": interact_limit_decision.message,
                "data": {
                    "interact_limit": interact_limit_decision.details,
                },
            },
        )

    login_transfer = _build_crawler_login_transfer_payload(force_scope_refresh=False)
    success = _get_process_manager().send_crawler_command(
        "interact",
        {
            "uids": request.uids,
            "content": comments[0],
            "comments": comments,
            "platform": request.platform,
            **login_transfer,
        },
    )

    if not success:
        return JSONResponse(status_code=400, content={"message": "互动任务启动失败，独立爬取进程未接收命令"})

    _invalidate_status_cache()
    _record_task_startup_trace(
        "interact_request_command_sent",
        {"trace_id": trace_id},
    )
    return {
        "success": True,
        "message": "一键互动任务已启动",
        "data": {"trace_id": trace_id},
    }


@app.get("/api/tasks/startup-trace", summary="阶段B·B-2：统一查询 task_startup_trace.jsonl")
async def get_task_startup_trace(
    kind: Optional[str] = None,
    trace_id: Optional[str] = None,
    limit: int = 100,
):
    """
    统一的任务启动追踪查询端点。

    Args:
        kind: 可选，按事件名关键词过滤（如 `search_request_received` / `send_request_received` /
              `interact_request_received`）。
        trace_id: 可选，按 trace_id 精确过滤。
        limit: 返回最近 N 条记录（最大 500）。
    """
    try:
        log_dir = get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        trace_path = log_dir / "task_startup_trace.jsonl"
        if not trace_path.exists():
            return {"status": "ok", "count": 0, "items": []}

        cap = max(1, min(int(limit or 100), 500))
        items: List[Dict[str, Any]] = []
        with trace_path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        # 倒序取最近 N 条
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            payload = dict(entry.get("payload") or {})
            if kind and str(kind) not in str(entry.get("event") or ""):
                continue
            if trace_id and str(trace_id) != str(payload.get("trace_id") or ""):
                continue
            items.append(
                {
                    "timestamp": entry.get("timestamp"),
                    "event": entry.get("event"),
                    "trace_id": payload.get("trace_id", ""),
                    "payload": payload,
                }
            )
            if len(items) >= cap:
                break
        return {"status": "ok", "count": len(items), "items": items}
    except OSError as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": str(exc)},
        )


@app.post("/api/stop")
async def stop_task():
    crawler_status = _get_crawler_process_snapshot()
    crawler_task = str(crawler_status.get("current_task") or "Idle")
    crawler_busy = bool(crawler_status.get("busy", False))
    if bool(crawler_status.get("alive", False)) and (crawler_busy or crawler_task != "Idle"):
        force_reset_on_timeout = bool(crawler_status.get("force_reset_recommended", False))
        result = _stop_crawler_task_with_recovery(
            close_process=False,
            force_reset_on_timeout=force_reset_on_timeout,
        )
        return {
            "success": bool(result.get("success", False)),
            "message": str(result.get("message") or "独立爬取/发送进程停止失败"),
            "forced": bool(result.get("forced", False)),
            "pending": bool(result.get("pending", False)),
        }
    bot_service.stop_task()
    _invalidate_status_cache()
    return {"success": True, "message": "Task stopping..."}

@app.post("/api/restart_browser")
async def restart_browser():
    blocked = _require_runtime_feature("monitor")
    if blocked:
        return blocked

    process_manager = _get_process_manager()
    cleanup_result = {"success": True, "forced": False}
    if hasattr(process_manager, "ensure_crawler_process_stopped_for_restart"):
        cleanup_result = process_manager.ensure_crawler_process_stopped_for_restart()

    ok, error_message, crawler_status = _kickoff_crawler_process_browser_init()
    _invalidate_status_cache()
    if not ok:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "message": error_message,
                "crawler_process": crawler_status,
                "cleanup": cleanup_result,
            },
        )
    return {
        "success": True,
        "message": "浏览器正在重启，请稍候...",
        "crawler_process": _get_crawler_process_snapshot(),
        "cleanup": cleanup_result,
    }

@app.post("/api/start_browser")
async def start_browser():
    """启动浏览器 (异步，不等待完成)
    
    修复：当is_running=True但浏览器实际已断开连接时，
    强制重置状态并允许重新启动。
    """
    blocked = _require_runtime_feature("monitor")
    if blocked:
        return blocked

    crawler_status = _get_crawler_process_snapshot()
    if _crawler_process_is_ready(crawler_status):
        _invalidate_status_cache()
        return {
            "success": True,
            "status": "success",
            "message": "浏览器已在运行",
            "is_running": True,
            "crawler_process": crawler_status,
        }

    ok, error_message, crawler_status = _kickoff_crawler_process_browser_init()
    _invalidate_status_cache()
    if not ok:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "status": "error",
                "message": error_message,
                "crawler_process": crawler_status,
            },
        )
    return {
        "success": True,
        "status": "success",
        "message": "浏览器启动中，请稍候...",
        "is_running": False,
        "crawler_process": _get_crawler_process_snapshot(),
    }


@app.post("/api/browser/open_url")
async def open_url_in_browser(request: BrowserOpenUrlRequest):
    blocked = _require_runtime_feature("monitor")
    if blocked:
        return blocked

    target_url = str(request.url or "").strip()
    if not target_url:
        return JSONResponse(status_code=400, content={"success": False, "message": "URL不能为空"})
    if not target_url.startswith("https://www.douyin.com/"):
        return JSONResponse(status_code=400, content={"success": False, "message": "仅支持打开抖音页面"})

    try:
        result = bot_service.open_url_in_same_browser(target_url)
        return {
            "success": True,
            "message": "已在抖音浏览器中新开标签页",
            "data": result,
        }
    except Exception as exc:
        logger.error(f"在抖音浏览器中打开页面失败: {exc}")
        return JSONResponse(status_code=409, content={"success": False, "message": str(exc)})

@app.get("/api/debug/browser/reply_candidates")
async def debug_browser_reply_candidates():
    """调试API: 获取当前爬取页面评论区内的回复展开候选节点。"""
    if not bot_service.is_running or not bot_service.browser_manager:
        return {"is_running": False, "candidates": []}

    try:
        def _get_reply_candidates():
            bm = bot_service.browser_manager
            if not bm:
                return {"is_running": True, "candidates": [], "error": "No browser_manager"}
            page = getattr(bm, "crawler_page", None) or getattr(bm, "page", None)
            if not page:
                return {"is_running": True, "candidates": [], "error": "No crawler page"}

            result = page.evaluate(
                """() => {
                    const visible = (node) => !!(node && node.offsetParent !== null);
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const replyPattern = /(展开|查看|全部|更多).{0,12}回复|共\\s*\\d+\\s*条回复/i;
                    const surfaceSelectors = [
                        '[data-e2e="comment-list"]',
                        '[data-e2e="comment-list-container"]',
                        '[class*="comment-list"]',
                        '[class*="CommentList"]',
                        '[class*="comment-panel"]',
                        '[class*="CommentPanel"]',
                        '.comment-main',
                    ];

                    let surface = null;
                    let usedSurfaceSelector = '';
                    for (const selector of surfaceSelectors) {
                        const node = document.querySelector(selector);
                        if (visible(node)) {
                            surface = node;
                            usedSurfaceSelector = selector;
                            break;
                        }
                    }

                    const root = surface || document.body;
                    const nodes = Array.from(root.querySelectorAll('button, a, [role="button"], span, div, p'));
                    const candidates = [];

                    for (const node of nodes) {
                        if (!visible(node)) continue;
                        const text = normalize(node.innerText || node.textContent || '');
                        if (!text || !replyPattern.test(text)) continue;
                        const clickable = node.closest('button, a, [role="button"]') || node;
                        if (!visible(clickable)) continue;
                        const rect = clickable.getBoundingClientRect();
                        candidates.push({
                            tag: clickable.tagName,
                            text: text.slice(0, 120),
                            className: String(clickable.className || '').slice(0, 200),
                            width: Math.round(rect.width || 0),
                            height: Math.round(rect.height || 0),
                            x: Math.round(rect.x || 0),
                            y: Math.round(rect.y || 0),
                            area: Math.round((rect.width || 0) * (rect.height || 0)),
                        });
                    }

                    candidates.sort((a, b) => a.y - b.y || a.x - b.x);
                    return {
                        page_url: window.location.href,
                        page_title: document.title,
                        surface_selector: usedSurfaceSelector,
                        candidate_count: candidates.length,
                        candidates: candidates.slice(0, 30),
                    };
                }"""
            )
            return {"is_running": True, **(result or {"candidates": []})}

        future = bot_service._submit_task(_get_reply_candidates)
        result = await _await_future(future, timeout=15)
        return result
    except Exception as e:
        return {"is_running": True, "error": repr(e)}


@app.get("/api/debug/browser/pages")
async def debug_browser_pages():
    """调试API: 获取Playwright浏览器的所有页面URL。"""
    if not bot_service.is_running or not bot_service.browser_manager:
        return {"is_running": False, "pages": []}

    try:
        def _get_pages_info():
            bm = bot_service.browser_manager
            if not bm:
                return {"is_running": True, "pages": [], "error": "No browser_manager"}
            context = bm.context if hasattr(bm, "context") else None
            if not context:
                return {"is_running": True, "pages": [], "error": "No context"}

            pages_info = []
            for i, page in enumerate(context.pages):
                try:
                    url = page.url
                    title = page.title() if hasattr(page, "title") else ""
                    is_monitor = (page == getattr(bm, "monitor_page", None))
                    is_crawler = (page == getattr(bm, "crawler_page", None))
                    is_search = (page == getattr(bm, "search_page", None))
                    page_role = "主页面"
                    if is_monitor:
                        page_role = "自动回复标签页"
                    elif is_crawler:
                        page_role = "爬取标签页"
                    elif is_search:
                        page_role = "综合搜索标签页"
                    else:
                        try:
                            raw_role = bm._get_page_role(page) if hasattr(bm, "_get_page_role") else ""
                        except Exception:
                            raw_role = ""
                        role_map = {
                            "main": "主页面",
                            "monitor": "自动回复标签页",
                            "crawler": "爬取标签页",
                            "search": "综合搜索标签页",
                        }
                        page_role = role_map.get(str(raw_role or "").strip(), page_role)
                    pages_info.append({"index": i, "url": url, "title": title, "role": page_role})
                except Exception as exc:
                    pages_info.append({"index": i, "error": str(exc)})

            return {"is_running": True, "pages_count": len(pages_info), "pages": pages_info}

        future = bot_service._submit_task(_get_pages_info)
        return await _await_future(future, timeout=10)
    except Exception as exc:
        return {"is_running": True, "error": str(exc)}


@app.post("/api/debug/crawl_video_once")
async def debug_crawl_video_once(request: DebugCrawlVideoRequest):
    """调试API: 使用当前 crawler_page 对单个视频执行一次评论回查。"""
    state = bot_service._get_runtime_state_snapshot()
    if not state.get("is_running") or not bot_service.browser_manager or not getattr(bot_service, "crawler", None):
        return JSONResponse(status_code=400, content={"message": "浏览器或爬虫未就绪"})
    if state.get("current_task") not in ("Idle", "", None):
        return JSONResponse(status_code=400, content={"message": f"当前存在运行中任务: {state.get('current_task')}"})

    video_url = str(request.video_url or "").strip()
    if not video_url:
        return JSONResponse(status_code=400, content={"message": "video_url 不能为空"})

    try:
        keywords = [kw.strip() for kw in (request.comment_keywords or "").split(",") if kw.strip()]
        # type: ignore[attr-defined] Crawler 类超过 8000 行，Pylance 超过 class body
        # length limit 后不解析类体内方法，因此看不到 .crawl_comments 方法。
        # 运行时 Crawler.crawl_comments 实际存在（line 7219）。
        crawler = bot_service.crawler
        if crawler is None:
            raise RuntimeError("Crawler 未初始化")
        future = bot_service._submit_task(
            crawler.crawl_comments,  # type: ignore[attr-defined]
            video_url,
            keywords,
            request.comment_time_start or "",
            request.comment_time_end or "",
            bool(request.skip_crawled),
        )
        result = await _await_future(future, timeout=900)
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"单视频评论回查失败: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})

# ========== 调试API ==========

# 注意：/api/debug/browser/pages 已在上方定义，此处不再重复

@app.get("/api/debug/browser/chat_page_dom")
async def get_chat_page_dom():
    """
    获取抖音聊天页面的DOM结构
    
    用于调试同步功能，查看会话列表的DOM结构
    通过worker线程执行，避免FastAPI线程直接操作Page对象
    """
    if not bot_service.is_running:
        return {"is_running": False, "message": "浏览器未启动"}
    
    try:
        def _get_chat_dom():
            """在worker线程中获取聊天页面DOM"""
            if not bot_service.message_monitor:
                return {"is_running": True, "message": "自动回复模块未初始化"}
            
            monitor = bot_service.message_monitor
            
            # 检查是否有聊天页面
            chat_page = None
            if monitor.browser_manager and monitor.browser_manager.context:
                context = monitor.browser_manager.context
                for page in context.pages:
                    try:
                        if "douyin.com/chat" in page.url:
                            chat_page = page
                            break
                    except Exception:
                        continue
            
            if not chat_page:
                return {"message": "未找到抖音聊天页面", "current_page_url": str(monitor.page.url) if monitor.page else None}
            
            # 获取DOM结构信息
            dom_info = chat_page.evaluate("""
            () => {
                const result = {
                    url: window.location.href,
                    title: document.title,
                    listContainer: null,
                    conversationItems: []
                };
                
                // 检查会话列表容器
                const listContainer = document.querySelector('.conversationConversationListwrapper');
                if (listContainer) {
                    result.listContainer = {
                        found: true,
                        className: listContainer.className,
                        childCount: listContainer.children.length
                    };
                    
                    // 检查会话项
                    const items = listContainer.querySelectorAll('[data-e2e="conversation-item"]');
                    result.conversationItems = Array.from(items).slice(0, 5).map(item => {
                        const nameEl = item.querySelector('.conversationConversationItemtitle');
                        const msgEl = item.querySelector('.ConversationItemHinttextBox');
                        return {
                            customerName: nameEl ? nameEl.textContent.trim() : '',
                            lastMessage: msgEl ? msgEl.textContent.trim().substring(0, 50) : '',
                            className: item.className
                        };
                    });
                } else {
                    result.listContainer = { found: false };
                }
                
                return result;
            }
            """)
            
            return {
                "is_running": True,
                "dom_info": dom_info
            }
        
        future = bot_service._submit_task(_get_chat_dom)
        result = await _await_future(future, timeout=15)
        return result
    except Exception as e:
        return {"is_running": True, "error": str(e)}

@app.get("/api/debug/browser/input_box_dom")
async def get_input_box_dom():
    """[DEBUG-INST:empty-line-diagnosis] Dump 抖音聊天页面 input_box 的真实 DOM 结构。

    包含：
    - 所有候选元素（按选择器分类）
    - 每个候选元素的 tag/className/role/data-e2e/editable/hasEditableChild/insideMsgInput
    - 每个候选元素的 outerHTML 截断
    - 当前 page URL

    通过 worker 线程执行 Page 操作。
    """
    if not bot_service.is_running:
        return {"is_running": False, "message": "浏览器未启动"}
    try:
        def _dump():
            if not bot_service.message_monitor:
                return {"is_running": True, "message": "自动回复模块未初始化"}
            monitor = bot_service.message_monitor
            if not monitor.page or monitor.page.is_closed():
                return {"is_running": True, "message": "RPA 页面已关闭"}
            return monitor.page.evaluate("""() => {
                const result = {
                    url: window.location.href,
                    title: document.title,
                    candidates: [],
                };
                // 所有候选选择器
                const selectors = [
                    '[data-e2e="msg-input"] [contenteditable="true"]',
                    '[data-e2e="msg-input"] [role="textbox"]',
                    '[data-e2e="msg-input"] textarea',
                    '[data-e2e="msg-input"] div[class*="public-DraftEditor-content"]',
                    '[class*="chat-input"] [contenteditable="true"]',
                    '[contenteditable="true"][role="textbox"]',
                    'div[class*="public-DraftEditor-content"]',
                    'div[contenteditable="true"]',
                    '[role="textbox"]',
                    'textarea',
                ];
                for (const selector of selectors) {
                    try {
                        const nodes = document.querySelectorAll(selector);
                        for (let i = 0; i < nodes.length; i++) {
                            const node = nodes[i];
                            const rect = node.getBoundingClientRect();
                            const visible = rect.width > 0 && rect.height > 0;
                            if (!visible) continue;
                            const closestMsgInput = node.closest('[data-e2e="msg-input"]');
                            result.candidates.push({
                                selector: selector,
                                index: i,
                                tag: node.tagName,
                                className: typeof node.className === 'string' ? node.className.slice(0, 100) : '',
                                dataE2e: node.getAttribute('data-e2e') || '',
                                parentDataE2e: node.parentElement ? (node.parentElement.getAttribute('data-e2e') || '') : '',
                                role: node.getAttribute('role') || '',
                                editable: !!node.isContentEditable,
                                hasEditableChild: !!node.querySelector('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]'),
                                insideMsgInput: !!closestMsgInput,
                                textContent: (node.textContent || '').slice(0, 100),
                                innerHTML: (node.innerHTML || '').slice(0, 300),
                                outerHTML: (node.outerHTML || '').slice(0, 400),
                            });
                        }
                    } catch (e) {}
                }
                return result;
            }""")
        future = bot_service._submit_task(_dump)
        result = await _await_future(future, timeout=15)
        return {"is_running": True, "dom_info": result}
    except Exception as e:
        return {"is_running": True, "error": str(e)}

@app.get("/api/debug/chat_messages")
async def debug_chat_messages():
    """调试：获取聊天消息页面的DOM结构
    
    通过worker线程执行，避免FastAPI线程直接操作Page对象
    """
    if not bot_service.is_running:
        return {"is_running": False, "message": "浏览器未启动"}
    
    try:
        def _debug_chat():
            if not bot_service.message_monitor:
                return {"is_running": True, "message": "自动回复模块未初始化"}
            debug_fn = getattr(bot_service.message_monitor, 'debug_chat_page_structure', None)
            if debug_fn:
                return debug_fn()
            return {"is_running": True, "message": "调试方法不可用"}
        
        future = bot_service._submit_task(_debug_chat)
        result = await _await_future(future, timeout=15)
        return result
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/debug/save_state")
async def debug_save_state():
    """调试：手动触发应用状态保存"""
    try:
        bot_service._save_state_before_shutdown()
        from src.douyin_bot.app_state_persistor import get_app_state_persistor
        persistor = get_app_state_persistor()
        return {"success": True, "status": persistor.get_persist_status()}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/debug/persist_status")
async def debug_persist_status():
    """调试：查看持久化状态"""
    try:
        from src.douyin_bot.app_state_persistor import get_app_state_persistor
        persistor = get_app_state_persistor()
        return persistor.get_persist_status()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/rpa_state")
async def debug_rpa_state():
    """调试：查看RPA引擎内部状态"""
    try:
        import time as _time
        result = {"is_running": bot_service.is_running, "is_monitoring": bot_service.is_monitoring_messages}
        if bot_service._use_rpa_mode and bot_service.rpa_launcher and bot_service.rpa_launcher.rpa_engine:
            engine = bot_service.rpa_launcher.rpa_engine
            result["rpa_engine"] = {
                "available": True,
                "is_monitoring": engine.is_monitoring,
                "observer_active": engine._observer_active,
                "first_fetch_done": engine._first_fetch_done,
                "chat_page_ensured": engine._chat_page_ensured,
                "poll_count": engine._poll_count,
                "states_count": len(engine._states),
                "states": {k: {"last_content": v.get("last_content", "")[:30], "sent_by_us": v.get("sent_by_us", False), "last_time_ago": f"{_time.time() - v.get('last_time', 0):.0f}s"} for k, v in engine._states.items()},
                "processed_msg_ids_count": len(engine._processed_msg_ids),
                "sent_cache_count": len(engine._sent_cache),
                "callback_registered": engine._message_callback is not None,
                "page_url": engine.page.url if engine.page and not engine.page.is_closed() else "N/A",
                "page_closed": engine.page.is_closed() if engine.page else True,
            }
        else:
            result["rpa_engine"] = {"available": False}
        return result
    except Exception as e:
        return {"error": str(e), "traceback": __import__('traceback').format_exc()}


@app.get("/api/system/status")
async def get_system_status():
    """获取系统状态（用于健康检查和诊断）"""
    import os
    import sys
    import time
    
    status = {
        "timestamp": time.time(),
        "python_version": sys.version,
        "platform": sys.platform,
        "working_directory": os.getcwd(),
        "environment": {}
    }
    
    env_keys = ["HUOKE_BASE_DIR", "HUOKE_DATA_DIR", "HUOKE_LOG_DIR", "OLLAMA_HOST", "OLLAMA_MODEL", "OLLAMA_FALLBACK_MODEL"]
    for key in env_keys:
        value = os.getenv(key, "")
        if "API_KEY" in key and value:
            value = value[:8] + "..." if len(value) > 8 else "***"
        status["environment"][key] = value or "未设置"
    
    base_dir = get_base_dir()
    data_dir = get_data_dir()
    
    status["paths"] = {
        "base_dir": str(base_dir),
        "data_dir": str(data_dir),
        "exists": {
            "data": (data_dir).exists(),
            "knowledge_base": (data_dir / "knowledge_base.json").exists(),
            "chroma_db": (data_dir / "chroma_db").exists(),
            "app_state": (data_dir / "app_state").exists(),
            "customers_db": (data_dir / "customers.json").exists()
        }
    }
    
    kb_count = 0
    kb_file = get_knowledge_base_path()
    if kb_file.exists():
        try:
            import json
            with open(kb_file, 'r', encoding='utf-8') as f:
                kb_data = json.load(f)
            kb_count = len(kb_data) if isinstance(kb_data, list) else 0
        except:
            pass
    status["knowledge_base_count"] = kb_count
    
    ollama_status = "unknown"
    try:
        import requests
        ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        resp = requests.get(f"{ollama_host}/api/tags", timeout=3)
        if resp.status_code == 200:
            ollama_status = "running"
            models = resp.json().get("models", [])
            status["ollama_models"] = [m.get("name", "") for m in models[:5]]
        else:
            ollama_status = "error"
    except:
        ollama_status = "not_running"
    status["ollama_status"] = ollama_status
    
    status["service_status"] = {
        "is_running": bot_service.is_running,
        "is_monitoring": bot_service.is_monitoring_messages,
        "is_logged_in": getattr(bot_service, '_login_status_cache', False)
    }
    
    return status


@app.post("/api/system/init_check")
async def system_init_check():
    """执行系统初始化检查"""
    try:
        from src.infrastructure.system_initializer import ensure_system_ready, get_system_status
        ready = ensure_system_ready()
        details = get_system_status()
        return {
            "ready": ready,
            "details": details
        }
    except Exception as e:
        return {
            "ready": False,
            "error": str(e)
        }


# ========== 购买意向分析API ==========

@app.get("/api/purchase-intent/analyze/{customer_name}")
async def analyze_purchase_intent(customer_name: str):
    """
    分析客户购买意向 - 企业级
    
    返回多维度购买意向评分，包括：
    - BANT评分（预算、权限、需求、时间线）
    - 购买信号检测
    - 客户旅程分析
    - 购买角色识别
    - 预测性分析
    """
    result = bot_service.analyze_purchase_intent(customer_name)
    if result.get("success"):
        return result
    else:
        return JSONResponse(status_code=500, content=result)


@app.get("/api/purchase-intent/lead-score/{customer_name}")
async def score_lead(customer_name: str):
    """
    对线索进行评分
    
    返回线索等级和跟进建议：
    - 线索等级（热/温/冷/冻）
    - 跟进优先级
    - 建议跟进时间和内容
    """
    result = bot_service.score_lead(customer_name)
    if result.get("success"):
        return result
    else:
        return JSONResponse(status_code=500, content=result)


@app.get("/api/purchase-intent/hot-leads")
async def get_hot_leads():
    """
    获取所有热线索
    
    返回高意向客户列表，需要优先跟进
    """
    result = bot_service.get_hot_leads()
    return result


@app.get("/api/purchase-intent/funnel")
async def get_conversion_funnel():
    """
    获取转化漏斗分析
    
    返回各阶段客户分布和转化预测
    """
    result = bot_service.get_conversion_funnel()
    return result


@app.get("/api/purchase-intent/analyze-all")
async def analyze_all_customers_intent():
    """
    批量分析所有客户的购买意向
    
    返回按意向分数排序的客户列表
    """
    result = bot_service.analyze_all_customers_intent()
    if result.get("success"):
        return result
    else:
        return JSONResponse(status_code=500, content=result)


# ========== 客户培育API ==========

@app.post("/api/nurture/start/{customer_name}")
async def start_nurture_campaign(customer_name: str):
    """
    启动客户培育活动
    
    为低意向客户启动自动培育流程
    """
    result = bot_service.start_nurture_campaign(customer_name)
    if result.get("success"):
        return result
    else:
        return JSONResponse(status_code=500, content=result)


@app.get("/api/nurture/message/{customer_name}")
async def get_nurture_message(customer_name: str):
    """
    获取培育消息
    
    返回下一个培育阶段的消息内容
    """
    result = bot_service.get_nurture_message(customer_name)
    if result.get("success"):
        return result
    else:
        return JSONResponse(status_code=404, content=result)


# ========== 数据分析仪表板API ==========

# ========== 统计数据API ==========

@app.get("/api/stats")
async def get_stats(platform: Optional[str] = None):
    """
    获取统计数据

    Query Parameters:
        - platform: 平台过滤
    """
    try:
        db = bot_service.db
        chat_store = bot_service._get_chat_store()

        customers = db.get_all_customers(platform or "")

        total = len(customers)
        pending = len([c for c in customers if c.get('status') == 'pending'])
        sent = len([c for c in customers if c.get('status') == 'sent'])
        replied = len([c for c in customers if c.get('status') == 'replied'])

        intent_dist = {
            "A": len([c for c in customers if c.get('intent_level') == 'A']),
            "B": len([c for c in customers if c.get('intent_level') == 'B']),
            "C": len([c for c in customers if c.get('intent_level') == 'C']),
            "D": len([c for c in customers if c.get('intent_level') == 'D']),
            "E": len([c for c in customers if c.get('intent_level') == 'E'])
        }

        platform_dist = {}
        for c in customers:
            p = c.get('platform', 'unknown')
            platform_dist[p] = platform_dist.get(p, 0) + 1

        conversations = chat_store.get_all_conversations_dicts()
        active_conversations = len([conv for conv in conversations if conv.get('status') == 'active'])
        unread_messages = len(db.get_unread_messages())

        messages = chat_store.get_all_messages_dicts()
        total_messages = len(messages)
        inbound_messages = len([m for m in messages if m.get('direction') == 'inbound'])
        outbound_messages = len([m for m in messages if m.get('direction') == 'outbound'])

        today = datetime.now().date()
        today_messages = 0
        today_inbound = 0
        today_outbound = 0

        for m in messages:
            try:
                if m.get('created_at'):
                    msg_date = datetime.fromisoformat(m.get('created_at', '2000-01-01')).date()
                    if msg_date == today:
                        today_messages += 1
                        if m.get('direction') == 'inbound':
                            today_inbound += 1
                        elif m.get('direction') == 'outbound':
                            today_outbound += 1
            except (ValueError, TypeError):
                continue

        daily_stats = []
        for i in range(6, -1, -1):
            date = today - timedelta(days=i)
            date_str = date.strftime('%m-%d')
            day_messages = []
            for m in messages:
                try:
                    if m.get('created_at'):
                        msg_date = datetime.fromisoformat(m.get('created_at', '2000-01-01')).date()
                        if msg_date == date:
                            day_messages.append(m)
                except (ValueError, TypeError):
                    continue

            daily_stats.append({
                "date": date_str,
                "total": len(day_messages),
                "inbound": len([m for m in day_messages if m.get('direction') == 'inbound']),
                "outbound": len([m for m in day_messages if m.get('direction') == 'outbound'])
            })

        high_intent_customers = [
            {
                "nickname": c.get('nickname', ''),
                "intent_level": c.get('intent_level', ''),
                "intent_score": c.get('intent_score', 0),
                "comment_content": c.get('comment_content', '')[:50] + '...' if len(c.get('comment_content', '')) > 50 else c.get('comment_content', ''),
                "source_video_url": c.get('source_video_url', '')
            }
            for c in customers if c.get('intent_level') in ['A', 'B']
        ][:10]

        reply_rate = round(replied / sent * 100, 1) if sent > 0 else 0

        intent_scores = [c.get('intent_score', 0) for c in customers if c.get('intent_score')]
        avg_intent_score = round(sum(intent_scores) / len(intent_scores), 1) if intent_scores else 0

        return {
            "customers": {
                "total": total,
                "pending": pending,
                "sent": sent,
                "replied": replied,
                "reply_rate": reply_rate
            },
            "intent_distribution": intent_dist,
            "platform_distribution": platform_dist,
            "messages": {
                "active_conversations": active_conversations,
                "unread_messages": unread_messages,
                "total_conversations": len(conversations),
                "total_messages": total_messages,
                "inbound_messages": inbound_messages,
                "outbound_messages": outbound_messages,
                "today_messages": today_messages,
                "today_inbound": today_inbound,
                "today_outbound": today_outbound
            },
            "daily_trend": daily_stats,
            "high_intent_customers": high_intent_customers,
            "avg_intent_score": avg_intent_score
        }
    except Exception as e:
        logger.error(f"获取统计数据失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            "customers": {"total": 0, "pending": 0, "sent": 0, "replied": 0, "reply_rate": 0},
            "intent_distribution": {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0},
            "platform_distribution": {},
            "messages": {
                "active_conversations": 0, "unread_messages": 0, "total_conversations": 0,
                "total_messages": 0, "inbound_messages": 0, "outbound_messages": 0,
                "today_messages": 0, "today_inbound": 0, "today_outbound": 0
            },
            "daily_trend": [],
            "high_intent_customers": [],
            "avg_intent_score": 0
        }


# ========== 报告生成API ==========

# ========== 营销效果追踪API ==========

@app.get("/api/marketing/tracking/summary")
async def get_marketing_tracking_summary(days: int = 30):
    """获取营销效果追踪汇总"""
    try:
        summary = marketing_tracking_service.get_tracking_summary(days)
        return {
            'success': True,
            'data': summary
        }
    except Exception as e:
        logger.error(f"获取营销追踪汇总失败: {e}")
        return {'success': False, 'error': str(e)}

@app.get("/api/marketing/tracking/top-customers")
async def get_marketing_top_customers(limit: int = 10, sort_by: str = "replied"):
    """获取营销效果最好的客户"""
    try:
        top_customers = marketing_tracking_service.get_top_customers(limit, sort_by)
        return {
            'success': True,
            'data': top_customers
        }
    except Exception as e:
        logger.error(f"获取top客户失败: {e}")
        return {'success': False, 'error': str(e)}

@app.get("/api/marketing/tracking/customer/{customer_name}")
async def get_customer_marketing_stats(customer_name: str):
    """获取客户营销统计"""
    try:
        return {
            'success': True,
            'data': marketing_tracking_service.get_customer_stats(customer_name)
        }
    except Exception as e:
        logger.error(f"获取客户营销统计失败: {e}")
        return {'success': False, 'error': str(e)}


def parse_web_server_args(argv=None):
    import argparse
    from src.config.settings import SERVER_HOST, SERVER_PORT

    parser = argparse.ArgumentParser(description="行业知识运营系统 Web服务")
    parser.add_argument("--host", type=str, default=SERVER_HOST, help="服务监听地址")
    parser.add_argument("--port", type=int, default=SERVER_PORT, help="服务监听端口")
    cli_args, unknown_args = parser.parse_known_args(argv)
    if unknown_args:
        logger.warning(f"忽略未知启动参数: {unknown_args}")
    return cli_args


def start_web_server(host=None, port=None):
    from src.config.settings import SERVER_HOST, SERVER_PORT

    resolved_host = host or SERVER_HOST
    resolved_port = SERVER_PORT if port is None else int(port)
    # [ROOT-CAUSE:uvicorn-bug] 0.30.6 httptools_impl 在 Windows 上有
    # Content-Length 异常。ContentLengthFixMiddleware 已处理覆盖，额外
    # 用 http="h11" 启动能减少 90% 触发率（h11 是纯 Python 实现，更稳）。
    uvicorn.run(
        app,
        host=resolved_host,
        port=resolved_port,
        http="h11",  # 避免 httptools_impl 的 Content-Length 异常
        log_level="info",
        access_log=False,  # 关闭访问日志，QPS 高时减少 IO
    )


if __name__ == "__main__":
    cli_args = parse_web_server_args()
    start_web_server(host=cli_args.host, port=cli_args.port)
