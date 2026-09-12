from typing import Any, Dict

from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel

from src.remote_control import get_remote_control_service


router = APIRouter(prefix="/api/remote-control", tags=["远程控制"])


class ApplyRemotePolicyRequest(BaseModel):
    policy: Dict[str, Any]


def _get_bot_service():
    from src.web.bot_service import get_bot_service

    return get_bot_service()


def _get_process_manager():
    from src.common.process_manager import get_process_manager

    return get_process_manager()


@router.get("/status")
async def get_remote_control_status():
    service = get_remote_control_service()
    return {"success": True, "data": service.get_snapshot().model_dump(mode="json")}


@router.post("/refresh")
async def refresh_remote_control_policy():
    service = get_remote_control_service()
    try:
        snapshot = service.refresh_from_server()
        service.enforce_runtime_limits(_get_bot_service(), _get_process_manager())
        return {"success": True, "data": snapshot.model_dump(mode="json")}
    except Exception as exc:
        logger.error(f"刷新远程控制策略失败: {exc}")
        return {"success": False, "message": str(exc)}


@router.post("/apply-local")
async def apply_local_remote_control_policy(request: ApplyRemotePolicyRequest):
    service = get_remote_control_service()
    try:
        snapshot = service.apply_policy(request.policy, source="manual")
        service.enforce_runtime_limits(_get_bot_service(), _get_process_manager())
        return {"success": snapshot.valid, "data": snapshot.model_dump(mode="json")}
    except Exception as exc:
        logger.error(f"应用本地远程控制策略失败: {exc}")
        return {"success": False, "message": str(exc)}


@router.post("/clear")
async def clear_local_remote_control_policy():
    service = get_remote_control_service()
    try:
        service.clear_policy()
        return {"success": True, "data": service.get_snapshot().model_dump(mode="json")}
    except Exception as exc:
        logger.error(f"清除远程控制策略失败: {exc}")
        return {"success": False, "message": str(exc)}
