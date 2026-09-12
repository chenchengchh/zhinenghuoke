from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from src.common.chat_store import ChatStoreFacade
from src.common.conversation_id import build_conversation_id, resolve_conversation_id
from src.common.database import DatabaseManager
from src.common.industry_schema_service import IndustrySchemaService

router = APIRouter(tags=["智能回复"])

_api_idempotency_lock = threading.Lock()
_api_idempotency_cache: Dict[str, Dict[str, Any]] = {}
_API_IDEMPOTENCY_TTL_SECONDS = 60
_API_IDEMPOTENCY_MAX_ENTRIES = 500


def _check_api_idempotency(
    customer_name: str, content: str, conversation_id: str
) -> Optional[Dict[str, Any]]:
    content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
    cache_key = f"{customer_name}:{conversation_id}:{content_hash}"
    now = time.time()
    with _api_idempotency_lock:
        expired_keys = [
            k for k, v in _api_idempotency_cache.items()
            if now - v.get("_ts", 0) > _API_IDEMPOTENCY_TTL_SECONDS
        ]
        for k in expired_keys:
            del _api_idempotency_cache[k]
        if len(_api_idempotency_cache) > _API_IDEMPOTENCY_MAX_ENTRIES:
            oldest = sorted(
                _api_idempotency_cache.items(), key=lambda x: x[1].get("_ts", 0)
            )
            for k, _ in oldest[: len(oldest) // 2]:
                del _api_idempotency_cache[k]
        cached = _api_idempotency_cache.get(cache_key)
        if cached and (now - cached.get("_ts", 0)) < _API_IDEMPOTENCY_TTL_SECONDS:
            return cached
    return None


def _record_api_idempotency(
    customer_name: str, content: str, conversation_id: str, result: Dict[str, Any]
) -> None:
    content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
    cache_key = f"{customer_name}:{conversation_id}:{content_hash}"
    with _api_idempotency_lock:
        _api_idempotency_cache[cache_key] = {**result, "_ts": time.time()}


class SmartReplyRequest(BaseModel):
    """智能回复请求"""

    message: str
    customer_name: str = ""
    customer_id: Optional[str] = None
    platform: str = "douyin"
    session_id: Optional[str] = None
    conversation_history: Optional[List[Dict[str, Any]]] = None
    enterprise_id: str = ""
    preferred_schema_id: str = ""
    tenant_resolution_mode: str = "fail_soft"


def _resolve_request_schema_context(request: SmartReplyRequest) -> Dict[str, str]:
    enterprise_id = str(request.enterprise_id or "").strip()
    preferred_schema_id = str(request.preferred_schema_id or "").strip()
    resolution_mode = str(request.tenant_resolution_mode or "").strip() or "fail_soft"

    schema_service = IndustrySchemaService()
    schema_id = schema_service.resolve_schema_id(
        enterprise_id=enterprise_id,
        preferred_schema_id=preferred_schema_id,
        resolution_mode=resolution_mode,
    )

    if resolution_mode == "fail_closed" and not enterprise_id and not preferred_schema_id:
        raise HTTPException(status_code=422, detail="enterprise_id 不能为空")

    if resolution_mode == "fail_closed" and not schema_id:
        raise HTTPException(status_code=422, detail="无法解析当前请求对应的 schema")

    return {
        "enterprise_id": enterprise_id,
        "schema_id": schema_id,
        "resolution_mode": resolution_mode,
    }


def _resolve_conversation_id(db: DatabaseManager, customer_name: str, platform: str, customer_id: str) -> str:
    return resolve_conversation_id(
        db=db,
        customer_name=customer_name,
        platform=platform,
        customer_id=customer_id,
    )


@router.post("/api/smart-reply")
async def smart_reply(request: SmartReplyRequest):
    """统一智能回复入口，直接复用正式生产回复主链。"""
    try:
        from src.common.enhanced_customer_service import (
            get_enhanced_customer_service,
            resolve_context_session_id,
        )

        service = get_enhanced_customer_service()
        db = DatabaseManager()
        chat_store = ChatStoreFacade(db)
        customer_id = request.customer_id or ""
        platform = request.platform or "unknown"
        customer_name = request.customer_name or "客户"
        content = (request.message or "").strip()
        conversation_history = request.conversation_history
        conversation_id = _resolve_conversation_id(db, customer_name, platform, customer_id)
        if conversation_history is None:
            conversation_history = chat_store.get_recent_messages_dicts(conversation_id, limit=10)

        cached_result = _check_api_idempotency(customer_name, content, conversation_id)
        if cached_result:
            logger.info(
                f"[smart-reply] idempotent_hit customer={customer_name} "
                f"conversation_id={conversation_id} content={content[:30]}"
            )
            return {k: v for k, v in cached_result.items() if k != "_ts"}

        schema_context = _resolve_request_schema_context(request)
        enterprise_id = schema_context["enterprise_id"]
        schema_id = schema_context["schema_id"]
        resolution_mode = schema_context["resolution_mode"]

        session_id = resolve_context_session_id(
            platform=platform,
            customer_id=customer_id,
            provided_session_id=request.session_id,
            conversation_history=conversation_history,
        ) or None
        result = service.process_message(
            message=request.message,
            customer_name=customer_name,
            conversation_history=conversation_history,
            customer_data={
                "sec_uid": customer_id,
                "platform": platform,
                "conversation_id": conversation_id,
                "enterprise_id": enterprise_id,
                "schema_id": schema_id,
                "tenant_resolution_mode": resolution_mode,
            },
            use_enhanced=True,
            session_id=session_id,
        )
        response = {
            "success": True,
            "reply": result.get("reply", ""),
            "intent_type": result.get("intent", "unknown"),
            "confidence": result.get("confidence", 0.0),
            "matched_knowledge": result.get("matched_knowledge"),
            "retrieval_evidence": result.get("retrieval_evidence", []),
            "reply_analysis": result.get("reply_analysis", {}),
            "need_human": result.get("need_human", False),
            "sentiment": result.get("sentiment", "neutral"),
            "urgency": result.get("urgency", "medium"),
            "source": result.get("source", "enhanced_customer_service"),
            "enterprise_id": enterprise_id,
            "schema_id": schema_id,
            "data": {
                "reply": result.get("reply", ""),
                "intentLevel": result.get("intent_level", "-"),
                "intentScore": result.get("intent_score", result.get("confidence", 0.0)),
                "matchedKnowledge": result.get("matched_knowledge"),
                "retrievalEvidence": result.get("retrieval_evidence", []),
                "replyAnalysis": result.get("reply_analysis", {}),
                "needHuman": result.get("need_human", False),
                "sentiment": result.get("sentiment", "neutral"),
                "urgency": result.get("urgency", "medium"),
                "source": result.get("source", "enhanced_customer_service"),
                "enterpriseId": enterprise_id,
                "schemaId": schema_id,
            },
        }
        _record_api_idempotency(customer_name, content, conversation_id, response)
        return response
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "message": exc.detail},
        )
    except Exception as exc:
        logger.error(f"统一消息处理失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})
