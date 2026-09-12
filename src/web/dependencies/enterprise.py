from __future__ import annotations

from typing import Optional

from fastapi import Request

from src.infrastructure.logger import get_request_context, request_context

DEFAULT_ENTERPRISE_ID = "default"
DEFAULT_TENANT_RESOLUTION_MODE = "fail_closed"
ENTERPRISE_HEADER_NAMES = ("X-Enterprise-ID", "X-Enterprise-Id", "X-Enterprise")
ENTERPRISE_QUERY_NAMES = ("enterprise_id", "enterpriseId")
PREFERRED_SCHEMA_HEADER_NAMES = ("X-Preferred-Schema-ID", "X-Preferred-Schema")
PREFERRED_SCHEMA_QUERY_NAMES = ("preferred_schema_id", "preferredSchemaId", "schema_id", "schemaId")
TENANT_RESOLUTION_HEADER_NAMES = ("X-Tenant-Resolution-Mode", "X-Resolution-Mode")
TENANT_RESOLUTION_QUERY_NAMES = ("tenant_resolution_mode", "tenantResolutionMode", "resolution_mode", "resolutionMode")


def normalize_enterprise_id(value: Optional[str], fallback: str = DEFAULT_ENTERPRISE_ID) -> str:
    normalized = str(value or "").strip()
    return normalized or fallback


def resolve_enterprise_id(
    request: Optional[Request] = None,
    *,
    explicit: Optional[str] = None,
    fallback: str = DEFAULT_ENTERPRISE_ID,
    persist_to_context: bool = True,
) -> str:
    candidate = str(explicit or "").strip()

    if not candidate and request is not None:
        for header_name in ENTERPRISE_HEADER_NAMES:
            header_value = str(request.headers.get(header_name) or "").strip()
            if header_value:
                candidate = header_value
                break

    if not candidate and request is not None:
        for query_name in ENTERPRISE_QUERY_NAMES:
            query_value = str(request.query_params.get(query_name) or "").strip()
            if query_value:
                candidate = query_value
                break

    resolved = normalize_enterprise_id(candidate, fallback=fallback)

    if persist_to_context:
        ctx = dict(get_request_context() or {})
        ctx["enterprise_id"] = resolved
        request_context.set(ctx)

    return resolved


def bind_request_enterprise_context(
    request: Optional[Request] = None,
    *,
    explicit: Optional[str] = None,
    fallback: str = DEFAULT_ENTERPRISE_ID,
) -> str:
    return resolve_enterprise_id(
        request,
        explicit=explicit,
        fallback=fallback,
        persist_to_context=True,
    )


def resolve_preferred_schema_id(
    request: Optional[Request] = None,
    *,
    explicit: Optional[str] = None,
    persist_to_context: bool = True,
) -> str:
    candidate = str(explicit or "").strip()

    if not candidate and request is not None:
        for header_name in PREFERRED_SCHEMA_HEADER_NAMES:
            header_value = str(request.headers.get(header_name) or "").strip()
            if header_value:
                candidate = header_value
                break

    if not candidate and request is not None:
        for query_name in PREFERRED_SCHEMA_QUERY_NAMES:
            query_value = str(request.query_params.get(query_name) or "").strip()
            if query_value:
                candidate = query_value
                break

    if persist_to_context:
        ctx = dict(get_request_context() or {})
        ctx["preferred_schema_id"] = candidate
        request_context.set(ctx)

    return candidate


def resolve_tenant_resolution_mode(
    request: Optional[Request] = None,
    *,
    explicit: Optional[str] = None,
    fallback: str = DEFAULT_TENANT_RESOLUTION_MODE,
    persist_to_context: bool = True,
) -> str:
    candidate = str(explicit or "").strip()

    if not candidate and request is not None:
        for header_name in TENANT_RESOLUTION_HEADER_NAMES:
            header_value = str(request.headers.get(header_name) or "").strip()
            if header_value:
                candidate = header_value
                break

    if not candidate and request is not None:
        for query_name in TENANT_RESOLUTION_QUERY_NAMES:
            query_value = str(request.query_params.get(query_name) or "").strip()
            if query_value:
                candidate = query_value
                break

    resolved = candidate or str(fallback or "").strip()

    if persist_to_context:
        ctx = dict(get_request_context() or {})
        ctx["tenant_resolution_mode"] = resolved
        request_context.set(ctx)

    return resolved


def bind_request_tenant_context(
    request: Optional[Request] = None,
    *,
    enterprise_id: Optional[str] = None,
    preferred_schema_id: Optional[str] = None,
    tenant_resolution_mode: Optional[str] = None,
    fallback_enterprise: str = DEFAULT_ENTERPRISE_ID,
    fallback_resolution_mode: str = DEFAULT_TENANT_RESOLUTION_MODE,
) -> dict:
    resolved_enterprise_id = resolve_enterprise_id(
        request,
        explicit=enterprise_id,
        fallback=fallback_enterprise,
        persist_to_context=True,
    )
    resolved_preferred_schema_id = resolve_preferred_schema_id(
        request,
        explicit=preferred_schema_id,
        persist_to_context=True,
    )
    resolved_tenant_resolution_mode = resolve_tenant_resolution_mode(
        request,
        explicit=tenant_resolution_mode,
        fallback=fallback_resolution_mode,
        persist_to_context=True,
    )
    return {
        "enterprise_id": resolved_enterprise_id,
        "preferred_schema_id": resolved_preferred_schema_id,
        "tenant_resolution_mode": resolved_tenant_resolution_mode,
    }
