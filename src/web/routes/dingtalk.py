from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter

from src.common.dingtalk_client import DingTalkClientError, get_dingtalk_client
from src.common.dingtalk_config_service import get_dingtalk_config_service


router = APIRouter(prefix="/api/dingtalk", tags=["钉钉"])


class DingTalkConfigRequest(BaseModel):
    enabled: bool = False
    app_key: str = ""
    app_secret: str = ""
    agent_id: str = ""
    receiver_name: str = ""
    receiver_mobile: str = ""
    notify_contact_provided_only: bool = True


class DingTalkBindReceiverRequest(BaseModel):
    receiver_mobile: str


class DingTalkTestSendRequest(BaseModel):
    message: str = ""


@router.get("/config")
async def get_dingtalk_config():
    service = get_dingtalk_config_service()
    return {"success": True, "data": service.mask_config_for_client()}


@router.post("/config")
async def save_dingtalk_config(request: DingTalkConfigRequest):
    service = get_dingtalk_config_service()
    config = service.save_config(request.model_dump())
    return {
        "success": True,
        "message": "钉钉配置已保存",
        "data": service.mask_config_for_client(config),
    }


@router.post("/bind-receiver")
async def bind_dingtalk_receiver(request: DingTalkBindReceiverRequest):
    service = get_dingtalk_config_service()
    config = service.load_config()
    errors = service.validate_config(config)
    if errors:
        return {"success": False, "message": "请先保存完整的钉钉应用配置"}

    mobile = str(request.receiver_mobile or "").strip()
    if not mobile:
        return {"success": False, "message": "请先输入接收人手机号"}

    try:
        client = get_dingtalk_client()
        access_token = client.get_access_token(
            str(config.get("app_key") or "").strip(),
            str(config.get("app_secret") or "").strip(),
        )
        userid = client.get_userid_by_mobile(access_token, mobile)
        updated = service.update_binding_result(
            receiver_mobile=mobile,
            receiver_userid=userid,
            status="success",
            error="",
        )
        return {
            "success": True,
            "message": "接收人绑定成功",
            "data": {
                "receiver_mobile": updated.get("receiver_mobile", ""),
                "receiver_userid": updated.get("receiver_userid", ""),
                "bind_status": updated.get("last_bind_status", "success"),
            },
        }
    except DingTalkClientError as exc:
        service.update_binding_result(
            receiver_mobile=mobile,
            receiver_userid="",
            status="failed",
            error=str(exc),
        )
        return {"success": False, "message": str(exc)}


@router.post("/test-send")
async def send_dingtalk_test_message(request: DingTalkTestSendRequest):
    service = get_dingtalk_config_service()
    config = service.load_config()
    can_send, reason = service.can_send(config)
    if not can_send:
        return {"success": False, "message": reason}

    try:
        client = get_dingtalk_client()
        access_token = client.get_access_token(
            str(config.get("app_key") or "").strip(),
            str(config.get("app_secret") or "").strip(),
        )
        message = str(request.message or "").strip() or "这是一条钉钉测试提醒，请确认手机 App 是否已收到。"
        response = client.send_text_message(
            access_token=access_token,
            agent_id=str(config.get("agent_id") or "").strip(),
            userid=str(config.get("receiver_userid") or "").strip(),
            content=message,
        )
        service.update_test_send_result(status="success", error="")
        return {
            "success": True,
            "message": "测试消息已提交发送",
            "data": {
                "status": "sent",
                "task_id": response.get("task_id", ""),
            },
        }
    except DingTalkClientError as exc:
        service.update_test_send_result(status="failed", error=str(exc))
        return {"success": False, "message": str(exc)}


@router.get("/status")
async def get_dingtalk_status():
    service = get_dingtalk_config_service()
    config = service.load_config()
    can_send, reason = service.can_send(config)
    return {
        "success": True,
        "data": {
            "configured": not bool(service.validate_config(config)),
            "receiver_bound": bool(str(config.get("receiver_userid") or "").strip()),
            "can_send": can_send,
            "last_send_status": str(config.get("last_test_send_status") or "never"),
            "last_send_error": str(config.get("last_test_send_error") or reason),
        },
    }
