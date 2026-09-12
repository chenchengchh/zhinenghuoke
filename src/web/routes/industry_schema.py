from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.common.industry_schema_service import get_industry_schema_service

router = APIRouter(tags=["行业Schema"])


class IndustrySchemaSaveRequest(BaseModel):
    schema_payload: Dict[str, Any] = Field(default_factory=dict, alias="schema")


class IndustrySchemaValidateRequest(BaseModel):
    schema_payload: Dict[str, Any] = Field(default_factory=dict, alias="schema")


class IndustrySchemaPreviewRequest(BaseModel):
    enterprise_id: str = Field("", min_length=0)
    preferred_schema_id: str = Field("", min_length=0)
    resolution_mode: str = Field("", min_length=0)


class IndustrySchemaSettingsRequest(BaseModel):
    active_schema_id: str = Field("", min_length=0)
    default_resolution_policy: str = Field("", min_length=0)
    require_enterprise_binding: bool | None = None
    fallback_schema_id: str = Field("", min_length=0)
    allow_preferred_schema_override: bool | None = None
    enterprise_schema_bindings: Dict[str, str] = Field(default_factory=dict)


@router.get("/api/industry-schemas")
async def list_industry_schemas():
    service = get_industry_schema_service()
    settings = service.get_settings()
    return {
        "success": True,
        "items": service.list_schemas(),
        "settings": settings,
        "template": service.get_schema_template(),
    }


@router.get("/api/industry-schemas/settings")
async def get_industry_schema_settings():
    service = get_industry_schema_service()
    return {"success": True, "settings": service.get_settings()}


@router.put("/api/industry-schemas/settings")
async def update_industry_schema_settings(request: IndustrySchemaSettingsRequest):
    service = get_industry_schema_service()
    try:
        updates: Dict[str, Any] = {}
        if str(request.active_schema_id or "").strip():
            updates["active_schema_id"] = str(request.active_schema_id).strip()
        if str(request.default_resolution_policy or "").strip():
            updates["default_resolution_policy"] = str(request.default_resolution_policy).strip()
        if request.require_enterprise_binding is not None:
            updates["require_enterprise_binding"] = bool(request.require_enterprise_binding)
        if str(request.fallback_schema_id or "").strip():
            updates["fallback_schema_id"] = str(request.fallback_schema_id).strip()
        if request.allow_preferred_schema_override is not None:
            updates["allow_preferred_schema_override"] = bool(request.allow_preferred_schema_override)
        if isinstance(request.enterprise_schema_bindings, dict):
            updates["enterprise_schema_bindings"] = {
                str(key or "").strip(): str(value or "").strip()
                for key, value in request.enterprise_schema_bindings.items()
                if str(key or "").strip() and str(value or "").strip()
            }
        settings = service.update_settings(updates)
    except Exception as exc:
        return JSONResponse(status_code=400, content={"success": False, "message": str(exc)})
    return {"success": True, "message": "已更新行业 Schema 设置", "settings": settings}


@router.get("/api/industry-schemas/{schema_id}")
async def get_industry_schema(schema_id: str):
    service = get_industry_schema_service()
    try:
        item = service.get_schema(schema_id)
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"success": False, "message": "Schema 不存在"})
    return {"success": True, "item": item}


@router.put("/api/industry-schemas/{schema_id}")
async def save_industry_schema(schema_id: str, request: IndustrySchemaSaveRequest):
    service = get_industry_schema_service()
    try:
        item = service.save_schema(schema_id, request.schema_payload)
    except Exception as exc:
        return JSONResponse(status_code=400, content={"success": False, "message": str(exc)})
    return {"success": True, "message": "Schema 已保存", "item": item}


@router.delete("/api/industry-schemas/{schema_id}")
async def delete_industry_schema(schema_id: str):
    service = get_industry_schema_service()
    try:
        result = service.delete_schema(schema_id)
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"success": False, "message": "Schema 不存在"})
    except Exception as exc:
        return JSONResponse(status_code=400, content={"success": False, "message": str(exc)})
    return {"success": True, "message": "模板已删除", "result": result}


@router.post("/api/industry-schemas/validate")
async def validate_industry_schema(request: IndustrySchemaValidateRequest):
    service = get_industry_schema_service()
    try:
        result = service.validate_schema(request.schema_payload)
    except Exception as exc:
        return JSONResponse(status_code=400, content={"success": False, "message": str(exc)})
    return {"success": True, "result": result}


@router.post("/api/industry-schemas/preview-resolution")
async def preview_industry_schema_resolution(request: IndustrySchemaPreviewRequest):
    service = get_industry_schema_service()
    try:
        result = service.preview_schema_resolution(
            enterprise_id=request.enterprise_id,
            preferred_schema_id=request.preferred_schema_id,
            resolution_mode=request.resolution_mode,
        )
    except Exception as exc:
        return JSONResponse(status_code=400, content={"success": False, "message": str(exc)})
    return {"success": True, "result": result}
