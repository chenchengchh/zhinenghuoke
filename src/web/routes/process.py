from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel

from src.remote_control import get_remote_control_service

router = APIRouter(tags=["进程管理"])


class CrawlerCommandRequest(BaseModel):
    command: str
    data: Optional[Dict[str, Any]] = None


def _get_process_manager():
    from src.common.process_manager import get_process_manager

    return get_process_manager()


def _block_remote_control(feature_name: str):
    service = get_remote_control_service()
    snapshot = service.get_snapshot()
    if service.is_feature_enabled(feature_name):
        return None
    return {
        "success": False,
        "message": snapshot.message or f"远程策略已禁用功能: {feature_name}",
        "remote_control_status": snapshot.model_dump(mode="json"),
    }


def _block_dangerous_crawler_command(command: str, data: Optional[Dict[str, Any]] = None):
    normalized_command = str(command or "").strip().lower()
    payload = dict(data or {})
    if normalized_command != "send_messages":
        return None
    if bool(payload.get("_auto_resume", False)):
        return {
            "success": False,
            "message": "通用命令口已禁用私信自动续发，请等待新的人工发送指令",
            "reason": "auto_resume_blocked",
        }
    return {
        "success": False,
        "message": "通用命令口已禁用 send_messages，请使用正式发送入口 /api/send",
        "reason": "send_messages_blocked",
    }


@router.get("/api/process/status")
async def get_process_status():
    try:
        return {"success": True, "status": _get_process_manager().get_status()}
    except Exception as exc:
        logger.error(f"获取进程状态失败: {exc}")
        return {"success": False, "error": str(exc)}


@router.post("/api/process/crawler/start")
async def start_crawler_process():
    try:
        blocked = _block_remote_control("crawler")
        if blocked:
            return blocked
        success = _get_process_manager().start_crawler_process()
        return {"success": success, "message": "爬取进程已启动" if success else "启动失败"}
    except Exception as exc:
        logger.error(f"启动爬取进程失败: {exc}")
        return {"success": False, "error": str(exc)}


@router.post("/api/process/crawler/stop")
async def stop_crawler_process():
    try:
        success = _get_process_manager().stop_crawler_process()
        return {"success": success, "message": "爬取进程已停止" if success else "停止失败"}
    except Exception as exc:
        logger.error(f"停止爬取进程失败: {exc}")
        return {"success": False, "error": str(exc)}


@router.post("/api/process/crawler/command")
async def send_crawler_command(request: CrawlerCommandRequest):
    try:
        blocked = _block_dangerous_crawler_command(request.command, request.data or {})
        if blocked:
            return blocked
        success = _get_process_manager().send_crawler_command(request.command, request.data or {})
        return {"success": success, "message": "命令已发送" if success else "发送失败，进程未运行"}
    except Exception as exc:
        logger.error(f"发送爬取命令失败: {exc}")
        return {"success": False, "error": str(exc)}


@router.post("/api/process/stop-all")
async def stop_all_processes():
    try:
        _get_process_manager().stop_all()
        return {"success": True, "message": "所有进程已停止"}
    except Exception as exc:
        logger.error(f"停止所有进程失败: {exc}")
        return {"success": False, "error": str(exc)}


__all__ = [
    "router",
    "CrawlerCommandRequest",
    "get_process_status",
    "start_crawler_process",
    "stop_crawler_process",
    "send_crawler_command",
    "stop_all_processes",
]
