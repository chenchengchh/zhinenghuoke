import json
import hmac
import os
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from fastapi import APIRouter, HTTPException, Response, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.licensing.license_models import ActivationRequest, LicenseType
from src.licensing.machine_fingerprint import collect_machine_fingerprint_components
from src.licensing.request_code import build_request_code_payload, encode_request_code
from src.licensing.license_service import get_license_service
from src.infrastructure.runtime_paths import get_data_dir, get_knowledge_base_path
from src.web.license_admin_auth import (
    LICENSE_ADMIN_COOKIE_NAME,
    build_license_admin_cookie_value,
    get_license_admin_credentials,
    require_license_admin,
)


router = APIRouter(prefix="/api/license", tags=["授权"])


class LicenseLoginRequest(BaseModel):
    username: str = ""
    password: str = ""


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_excel(path: Path, rows, *, sheet_name: str) -> None:
    dataframe = pd.DataFrame(rows or [{}])
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False, sheet_name=sheet_name[:31] or "Sheet1")


def _demo_routes_enabled() -> bool:
    return os.getenv("HUOKE_ENABLE_DEMO_LICENSE_ROUTES", "").strip().lower() in {"1", "true", "yes", "on"}


@router.get("/status")
async def get_license_status():
    service = get_license_service()
    return {"success": True, "data": service.get_state_snapshot().model_dump(mode="json")}


@router.get("/request-code")
async def get_license_request_code(http_request: Request):
    require_license_admin(http_request)
    service = get_license_service()
    components = collect_machine_fingerprint_components()
    payload = build_request_code_payload(
        machine_hash=service.get_machine_hash(),
        components=components,
    )
    return {
        "success": True,
        "data": {
            "request_code": encode_request_code(payload),
            "machine_hash": payload.machine_hash,
            "fingerprint_version": payload.fingerprint_version,
            "machine_profile": payload.machine_profile,
            "components": components,
        },
    }


@router.post("/login")
async def login_license_management(request: LicenseLoginRequest, response: Response):
    configured_username, configured_password = get_license_admin_credentials()
    if not configured_username or not configured_password:
        raise HTTPException(status_code=503, detail="授权管理未配置登录凭据，请先设置服务端环境变量")

    username = str(request.username or "").strip()
    password = str(request.password or "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入账户和密码")

    username_ok = hmac.compare_digest(username, configured_username)
    password_ok = hmac.compare_digest(password, configured_password)
    if not username_ok or not password_ok:
        raise HTTPException(status_code=401, detail="账户或密码错误")

    response.set_cookie(
        key=LICENSE_ADMIN_COOKIE_NAME,
        value=build_license_admin_cookie_value(configured_username),
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return {"success": True}


@router.post("/activate")
async def activate_license(request: ActivationRequest, http_request: Request):
    require_license_admin(http_request)
    service = get_license_service()
    result = service.activate(request.license_key, request.phone_number, request.customer_name)
    return result.model_dump(mode="json")


@router.post("/deactivate")
async def deactivate_license(http_request: Request):
    require_license_admin(http_request)
    service = get_license_service()
    result = service.deactivate()
    return result.model_dump(mode="json")


@router.post("/export-data")
async def export_license_management_data(http_request: Request):
    require_license_admin(http_request)
    try:
        from src.common.unified_knowledge_service import get_unified_knowledge_service
        from src.web.routes.analytics import (
            _build_dashboard_action_items_payload,
            _build_follow_up_export_rows,
            _build_high_intent_export_rows,
            get_high_intent_customers_enhanced,
        )

        data_dir = get_data_dir()
        export_dir = data_dir / "export_bundle"
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        knowledge_items = [item.to_dict() for item in get_unified_knowledge_service().get_items()]
        knowledge_json_path = export_dir / f"knowledge_base_all_{timestamp}.json"
        _write_json(
            knowledge_json_path,
            {
                "exported_at": datetime.now().isoformat(),
                "total": len(knowledge_items),
                "items": knowledge_items,
            },
        )

        raw_kb_path = get_knowledge_base_path()
        copied_kb_path = None
        if raw_kb_path.exists():
            copied_kb_path = export_dir / f"knowledge_base_source_{timestamp}.json"
            shutil.copy2(raw_kb_path, copied_kb_path)

        high_intent_payload = await get_high_intent_customers_enhanced(min_score=50, limit=500)
        if isinstance(high_intent_payload, JSONResponse):
            return high_intent_payload
        high_intent_customers = list((high_intent_payload or {}).get("customers") or [])
        high_intent_rows = _build_high_intent_export_rows(high_intent_customers)
        high_intent_json_path = export_dir / f"high_intent_customers_{timestamp}.json"
        high_intent_excel_path = export_dir / f"high_intent_customers_{timestamp}.xlsx"
        _write_json(
            high_intent_json_path,
            {
                "exported_at": datetime.now().isoformat(),
                "total": len(high_intent_customers),
                "customers": high_intent_customers,
            },
        )
        _write_excel(high_intent_excel_path, high_intent_rows, sheet_name="高意向客户")

        follow_up_items = list(_build_dashboard_action_items_payload() or [])
        contact_provided_items = [
            item
            for item in follow_up_items
            if str(item.get("type") or item.get("trigger_type") or "").strip() == "contact_provided_follow_up"
        ]
        contact_rows = _build_follow_up_export_rows(contact_provided_items)
        contact_json_path = export_dir / f"contact_provided_customers_{timestamp}.json"
        contact_excel_path = export_dir / f"contact_provided_customers_{timestamp}.xlsx"
        _write_json(
            contact_json_path,
            {
                "exported_at": datetime.now().isoformat(),
                "total": len(contact_provided_items),
                "customers": contact_provided_items,
            },
        )
        _write_excel(contact_excel_path, contact_rows, sheet_name="留资客户")

        manifest = {
            "exported_at": datetime.now().isoformat(),
            "export_dir": str(export_dir),
            "files": {
                "knowledge_all_json": str(knowledge_json_path),
                "knowledge_source_json": str(copied_kb_path) if copied_kb_path else "",
                "high_intent_json": str(high_intent_json_path),
                "high_intent_excel": str(high_intent_excel_path),
                "contact_provided_json": str(contact_json_path),
                "contact_provided_excel": str(contact_excel_path),
            },
            "counts": {
                "knowledge_items": len(knowledge_items),
                "high_intent_customers": len(high_intent_customers),
                "contact_provided_customers": len(contact_provided_items),
            },
        }
        manifest_path = export_dir / f"export_manifest_{timestamp}.json"
        _write_json(manifest_path, manifest)
        manifest["manifest_path"] = str(manifest_path)

        return {
            "success": True,
            "message": "导出完成，文件已写入 data/export_bundle 目录",
            "data": manifest,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/demo/trial")
async def generate_trial_demo_license(http_request: Request):
    require_license_admin(http_request)
    if not _demo_routes_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    service = get_license_service()
    key = service.generate_demo_license_key(LicenseType.TRIAL_7D)
    return {"success": True, "license_key": key}


@router.get("/demo/permanent")
async def generate_permanent_demo_license(http_request: Request):
    require_license_admin(http_request)
    if not _demo_routes_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    service = get_license_service()
    key = service.generate_demo_license_key(LicenseType.PERMANENT)
    return {"success": True, "license_key": key}


class LicenseGenerateRequest(BaseModel):
    license_type: str = "trial_7d"
    customer_name: str = ""
    phone_number: str = ""
    request_code: str = ""
    machine_hash: str = ""
    license_id: str = ""
    features: dict = {}


@router.post("/generate")
async def generate_license_key_api(request: LicenseGenerateRequest, http_request: Request):
    """生成验证码（license_key）。需管理员登录。"""
    require_license_admin(http_request)
    try:
        from src.licensing.license_models import LicenseFeatures, get_license_type_label, get_license_valid_days
        from src.licensing.request_code import decode_request_code
        import base64 as _b64

        phone_number = str(request.phone_number or "").strip()
        if not phone_number:
            raise HTTPException(status_code=400, detail="注册手机号不能为空")

        resolved_machine_hash = str(request.machine_hash or "").strip()
        request_code = str(request.request_code or "").strip()
        if request_code:
            try:
                request_payload = decode_request_code(request_code)
                resolved_machine_hash = request_payload.machine_hash
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"请求码无效：{exc}")
        if not resolved_machine_hash:
            raise HTTPException(status_code=400, detail="设备请求码或机器码不能为空")

        try:
            license_type = LicenseType(str(request.license_type or "").strip())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"不支持的授权类型：{request.license_type}")

        features = LicenseFeatures(**(request.features or {}))
        service = get_license_service()
        license_key = service.generate_license_key(
            license_type=license_type,
            customer_name=str(request.customer_name or "").strip(),
            phone_number=phone_number,
            machine_hash=resolved_machine_hash,
            features=features,
            license_id=str(request.license_id or "").strip(),
        )

        # 解码 license_key 展示内部 payload
        try:
            decoded_bytes = _b64.urlsafe_b64decode(license_key.encode("utf-8") + b"===")
            signed_payload = json.loads(decoded_bytes.decode("utf-8"))
        except Exception:
            signed_payload = {}

        return {
            "success": True,
            "data": {
                "license_key": license_key,
                "license_type": license_type.value,
                "license_type_label": get_license_type_label(license_type),
                "valid_days": get_license_valid_days(license_type),
                "customer_name": str(request.customer_name or "").strip(),
                "phone_number": phone_number,
                "license_id": str(request.license_id or "").strip(),
                "machine_hash": resolved_machine_hash,
                "features": features.model_dump(mode="json"),
                "signed_payload": signed_payload,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class LicenseDecodeRequest(BaseModel):
    license_key: str = ""


@router.post("/decode")
async def decode_license_key_api(request: LicenseDecodeRequest, http_request: Request):
    """解码验证码（license_key）展示内部信息。需管理员登录。"""
    require_license_admin(http_request)
    import base64 as _b64
    license_key = str(request.license_key or "").strip()
    if not license_key:
        raise HTTPException(status_code=400, detail="license_key 不能为空")
    try:
        decoded_bytes = _b64.urlsafe_b64decode(license_key.encode("utf-8") + b"===")
        payload = json.loads(decoded_bytes.decode("utf-8"))
        return {"success": True, "data": payload}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解码失败：{exc}")
