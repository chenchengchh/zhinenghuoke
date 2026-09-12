"""
监控管理路由模块

处理RPA监控、浏览器状态、会话管理等功能
"""

import os

from fastapi import APIRouter
from loguru import logger

router = APIRouter(prefix="/api/monitor", tags=["监控管理"])
SYNC_BROWSER_START_TIMEOUT_SECONDS = 180.0


def _is_test_inbound_enabled() -> bool:
    environment = str(os.getenv("ENVIRONMENT", "development") or "development").strip().lower()
    explicit_flag = str(os.getenv("HUOKE_ENABLE_TEST_INBOUND", "") or "").strip().lower()
    return environment in {"development", "testing"} and explicit_flag in {"1", "true", "yes", "on"}


def _invalidate_main_status_cache() -> None:
    try:
        from src.web.main import _invalidate_status_cache

        _invalidate_status_cache()
    except Exception as cache_e:
        logger.debug(f"失效主状态缓存失败: {cache_e}")


def _default_reply_settings():
    """自动回复默认配置固定开启，前端不再单独展示设置项。"""
    return {
        "auto_reply_enabled": True,
        "intent_analysis_enabled": True,
        "push_notifications_enabled": True,
    }


@router.post("/start")
async def start_monitor():
    """启动消息回复
    
    业务逻辑：
    1. 浏览器未运行 -> 启动浏览器，若已登录则继续直接启动消息回复
    2. 浏览器已运行 + 回复已运行 -> 返回"已在运行中"
    3. 浏览器已运行 + 回复未运行 -> 启动消息回复
    """
    try:
        from src.web.bot_service import get_bot_service
        from src.web.main import _require_runtime_feature

        blocked = _require_runtime_feature("monitor")
        if blocked:
            return blocked
        
        bot_service = get_bot_service()
        
        if not bot_service.is_running:
            browser_started = False
            if hasattr(bot_service, "start_browser_sync"):
                # 打包冷启动时浏览器和本地模型初始化较慢，20 秒容易被误判为失败。
                browser_started = bool(
                    bot_service.start_browser_sync(
                        timeout_seconds=SYNC_BROWSER_START_TIMEOUT_SECONDS
                    )
                )
            else:
                bot_service.start_browser()
                browser_started = bool(bot_service.is_running)

            if not browser_started:
                return {
                    "success": False,
                    "message": "浏览器启动失败，请重试",
                    "monitoring_started": False,
                    "browser_started": False,
                    "requires_login": False,
                }

            if not bot_service.check_login():
                return {
                    "success": True,
                    "message": "浏览器已启动，请扫码登录后再启动消息回复",
                    "monitoring_started": False,
                    "browser_started": True,
                    "requires_login": True,
                }
        
        monitoring_status = bot_service.get_status()
        if monitoring_status.get("effective_monitoring"):
            return {
                "success": False,
                "message": "消息回复已在运行中",
                "monitoring_started": True,
                "browser_started": True,
                "requires_login": False,
            }
        
        result = bot_service.start_message_monitoring()
        _invalidate_main_status_cache()
        if result:
            return {
                "success": True,
                "message": "消息回复已启动",
                "monitoring_started": True,
                "browser_started": True,
                "requires_login": False,
            }
        else:
            failure_reason = getattr(bot_service, "_last_monitor_start_error", "") or "消息回复启动失败，请检查浏览器是否已登录"
            return {
                "success": False,
                "message": failure_reason,
                "monitoring_started": False,
                "browser_started": bool(bot_service.is_running),
                "requires_login": ("登录" in failure_reason) or ("扫码" in failure_reason),
            }
    except Exception as e:
        logger.error(f"启动消息回复失败: {e}")
        return {"success": False, "message": f"启动失败: {str(e)}"}


@router.post("/stop")
async def stop_monitor():
    """停止消息回复"""
    try:
        from src.web.bot_service import get_bot_service
        
        service = get_bot_service()
        if hasattr(service, "stop_message_monitoring_sync"):
            stopped = service.stop_message_monitoring_sync(reason="api:/api/monitor/stop")
        else:
            service.stop_message_monitoring(reason="api:/api/monitor/stop")
            stopped = True
        _invalidate_main_status_cache()
        if stopped:
            return {"success": True, "message": "消息回复已停止"}
        return {"success": True, "message": "消息回复停止中，请稍后刷新确认"}
    except Exception as e:
        logger.error(f"停止消息回复失败: {e}")
        return {"success": False, "message": f"停止失败: {str(e)}"}


@router.post("/test-inbound")
async def test_inbound(message: dict):
    if not _is_test_inbound_enabled():
        return {
            "success": False,
            "reason": "test_inbound_disabled",
            "message": "测试入站接口默认禁用；仅开发/测试环境且显式开启 HUOKE_ENABLE_TEST_INBOUND 时可用",
        }
    from src.web.bot_service import get_bot_service
    bot_service = get_bot_service()
    payload = dict(message or {})
    payload.setdefault("inbound_trigger", "api_test_inbound")
    success, reason = bot_service.submit_inbound_message(payload)
    return {"success": success, "reason": reason}

@router.get("/status")
async def get_monitor_status():
    """获取消息回复状态，用于合并后的获取任务页面显示。"""
    from src.web.bot_service import get_bot_service
    
    bot_service = get_bot_service()
    status = bot_service.get_status()
    effective_monitoring = bool(status.get("effective_monitoring", status.get("is_monitoring_messages", False)))
    unread_count = int(status.get("unread_message_count", 0) or 0)
    try:
        retry_pending_events = bot_service.get_outbox_events(statuses=["retry_pending"], limit=200)
        paused_workflows = bot_service.get_workflow_runs(statuses=["paused", "waiting_retry"], limit=200)
    except Exception as workflow_e:
        logger.debug(f"获取 outbox/workflow 摘要失败: {workflow_e}")
        retry_pending_events = []
        paused_workflows = []

    oldest_retry_at = ""
    if retry_pending_events:
        retry_times = [str(item.get("next_retry_at", "") or "").strip() for item in retry_pending_events]
        retry_times = [item for item in retry_times if item]
        if retry_times:
            oldest_retry_at = min(retry_times)
    return {
        "current_url": status.get("current_url", ""),
        "is_running": status.get("is_running", False),
        "current_task": status.get("current_task", "Idle"),
        "is_logged_in": status.get("is_logged_in", False),
        "last_sync": status.get("last_sync", None),
        "is_monitoring_messages": status.get("is_monitoring_messages", False),
        "effective_monitoring": effective_monitoring,
        "monitoring_state": status.get("monitoring_state", "stopped"),
        "unread_message_count": unread_count,
        "status_text": "监听中" if effective_monitoring else "未启动",
        "reply_settings": _default_reply_settings(),
        "outbox_retry_pending_count": len(retry_pending_events),
        "paused_workflow_count": len(paused_workflows),
        "oldest_outbox_retry_at": oldest_retry_at,
    }


@router.get("/send-diagnostics")
async def get_send_diagnostics(trace_id: str = ""):
    """获取最近发送链诊断快照。"""
    try:
        from src.web.bot_service import get_bot_service

        bot_service = get_bot_service()
        return {"success": True, "diagnostics": bot_service.get_send_diagnostics(trace_id=trace_id)}
    except Exception as e:
        logger.error(f"获取发送诊断失败: {e}")
        return {"success": False, "message": f"获取失败: {str(e)}", "diagnostics": {}}


@router.get("/sessions")
async def get_active_sessions(limit: int = 50):
    """获取活跃的多轮对话会话"""
    sessions = []
    
    try:
        from src.common.enhanced_customer_service import get_enhanced_customer_service
        
        service = get_enhanced_customer_service()
        dialogue_manager = service._dialogue_manager

        for session_id in list(dialogue_manager.state_tracker._states.keys()):
            state = dialogue_manager.state_tracker._states[session_id]
            sessions.append({
                "session_id": session_id,
                "customer_id": getattr(state, 'customer_id', ''),
                "state": state.state.value if hasattr(state, 'state') else "unknown",
                "current_intent": getattr(state, 'current_intent', None),
                "turn_count": getattr(state, 'turn_count', 0),
                "updated_at": state.updated_at.isoformat() if hasattr(state, 'updated_at') else None
            })

        sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

        return {"total": len(sessions), "sessions": sessions[:limit]}

    except Exception as e:
        logger.error(f"获取会话列表失败: {e}")
        return {"total": 0, "sessions": [], "error": str(e)}


@router.post("/sessions/{session_id}/clear")
async def clear_session(session_id: str):
    """清除指定的多轮对话会话"""
    try:
        from src.common.enhanced_customer_service import get_enhanced_customer_service

        service = get_enhanced_customer_service()
        service.clear_session(session_id)
        return {"success": True, "message": f"会话 {session_id} 已清除"}
    except Exception as e:
        logger.error(f"清除会话失败: {e}")
        return {"success": False, "message": f"清除失败: {str(e)}"}


@router.get("/proactive/actions")
async def get_proactive_actions_summary():
    """获取主动服务动作汇总"""
    try:
        from src.common.enhanced_customer_service import get_enhanced_customer_service

        service = get_enhanced_customer_service()
        engine = service._proactive_engine

        all_actions = engine.action_scheduler._scheduled_actions
        pending = [a for a in all_actions if a.status == "pending"]

        by_type = {}
        for action in all_actions:
            action_type = action.action_type.value
            by_type[action_type] = by_type.get(action_type, 0) + 1

        return {
            "total_actions": len(all_actions),
            "pending_actions": len(pending),
            "sent_actions": len([a for a in all_actions if a.status == "sent"]),
            "by_type": by_type
        }
    except Exception as e:
        logger.error(f"获取主动动作汇总失败: {e}")
        return {"error": str(e)}


@router.get("/debug/user-id")
async def debug_get_user_id():
    """调试端点：获取当前登录用户ID"""
    try:
        import asyncio
        from src.web.bot_service import get_bot_service
        bot = get_bot_service()
        rpa = None
        launcher = getattr(bot, 'rpa_launcher', None)
        if launcher:
            rpa = getattr(launcher, 'rpa_engine', None)
        if not rpa:
            return {"success": False, "message": "RPA引擎不可用"}

        my_uid = await asyncio.get_event_loop().run_in_executor(None, rpa.resolve_my_user_id)

        interceptor = getattr(rpa, 'api_interceptor', None)
        api_msg_count = 0
        api_sample = []
        if interceptor:
            msgs = interceptor.get_captured_messages(consume=False)
            api_msg_count = len(msgs)
            for m in msgs[-3:]:
                api_sample.append({
                    "sender_id": m.get("sender_id", ""),
                    "customer_id": m.get("customer_id", ""),
                    "direction": m.get("direction", ""),
                    "content": str(m.get("content", ""))[:50],
                    "msg_id": m.get("msg_id", ""),
                })

        return {
            "success": True,
            "my_user_id": my_uid,
            "source": "api_interceptor" if my_uid and not my_uid.startswith("csrf:") else
                      "cookies" if my_uid and my_uid.startswith("csrf:") else
                      "dom" if my_uid else "unresolved",
            "api_interceptor_message_count": api_msg_count,
            "api_sample": api_sample,
        }
    except Exception as e:
        return {"success": False, "message": str(e)}
