from __future__ import annotations

"""学习审核路由。

当前仅保留知识库页面正在使用的学习审核主链：
统计、启停、待审核列表、通过、拒绝。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from src.web.dependencies.learning import get_learning_facade

router = APIRouter(tags=["学习系统"])


def _require_learning_facade():
    facade = get_learning_facade()
    if facade is None:
        raise RuntimeError("学习门面不可用")
    return facade


class LearningApprovalRequest(BaseModel):
    item_id: str
    item_type: Optional[str] = None
    approved_answer: Optional[str] = None
    category: Optional[str] = None


class LearningRejectionRequest(BaseModel):
    item_id: str
    item_type: Optional[str] = None
    reason: Optional[str] = None


def _parse_sort_time(value: Any) -> datetime:
    if not value:
        return datetime.min
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def _serialize_work_item(item: Any) -> Dict[str, Any]:
    if hasattr(item, "to_dict"):
        payload = item.to_dict()
    else:
        payload = dict(item)
    metadata = payload.get("metadata") or {}
    payload["metadata"] = metadata
    payload["suggested_intent"] = metadata.get("suggested_intent")
    return payload


def _list_combined_pending(top_k: int) -> Dict[str, List[Dict[str, Any]]]:
    facade = _require_learning_facade()
    stats = facade.get_stats()
    pending_validation_total = int(stats.get("pending_validation", 0) or 0)
    get_pending_review_knowledge = getattr(facade, "get_pending_review_knowledge", None)
    pending_review_supported = callable(get_pending_review_knowledge)
    pending_review_total = int(stats.get("pending_review", 0) or 0) if pending_review_supported else 0
    pending_validation = [
        _serialize_work_item(item)
        for item in facade.get_pending_validation(max(top_k, pending_validation_total))
    ]
    pending_review = []
    if pending_review_supported:
        pending_review = [_serialize_work_item(item) for item in get_pending_review_knowledge()]
    items = pending_validation + pending_review
    items.sort(
        key=lambda item: _parse_sort_time(item.get("updated_at") or item.get("created_at")),
        reverse=True,
    )
    visible_items = items[:top_k]
    return {
        "total": len(visible_items),
        "items": visible_items,
    }


@router.get("/api/learning/stats")
async def get_learning_stats():
    try:
        stats = _require_learning_facade().get_stats()
        pending_validation_only = int(stats.get("pending_validation", 0) or 0)
        pending_review = int(stats.get("pending_review", 0) or 0)
        stats["pending_validation_only"] = pending_validation_only
        stats["pending_review"] = pending_review
        stats["pending_validation"] = pending_validation_only + pending_review
        return stats
    except Exception as exc:
        logger.error(f"获取学习系统统计失败: {exc}")
        return {
            "learning_enabled": False,
            "total_learned": 0,
            "pending_validation": 0,
            "pending_validation_only": 0,
            "pending_review": 0,
            "approved_knowledge": 0,
            "rejected_knowledge": 0,
            "auto_approved": 0,
            "human_approved": 0,
            "rejected": 0,
            "pending": 0,
            "knowledge_auto_created": 0,
        }


@router.post("/api/learning/enable")
async def enable_learning():
    try:
        facade = _require_learning_facade()
        if facade.set_learning_enabled(True):
            return {"message": "学习系统已启用", "learning_enabled": True}
        return JSONResponse(status_code=500, content={"message": "学习系统不可用"})
    except Exception as exc:
        logger.error(f"启用学习系统失败: {exc}")
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.post("/api/learning/disable")
async def disable_learning():
    try:
        facade = _require_learning_facade()
        if facade.set_learning_enabled(False):
            return {"message": "学习系统已禁用", "learning_enabled": False}
        return JSONResponse(status_code=500, content={"message": "学习系统不可用"})
    except Exception as exc:
        logger.error(f"禁用学习系统失败: {exc}")
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.get("/api/learning/pending")
async def get_pending_validation(top_k: int = 20):
    try:
        return _list_combined_pending(top_k)
    except Exception as exc:
        logger.error(f"获取待验证内容失败: {exc}")
        return {"total": 0, "items": []}


@router.get("/api/learning/history")
async def get_learning_history(limit: int = 50, status: str = "approved"):
    try:
        items = _require_learning_facade().get_learning_history(limit)
        normalized_status = (status or "").strip().lower()
        if normalized_status and normalized_status != "all":
            action_aliases = {
                "approved": {"approved", "auto_approved"},
                "rejected": {"rejected"},
                "pending": {"pending"},
            }
            allowed_actions = action_aliases.get(normalized_status, {normalized_status})
            items = [
                item for item in items
                if str(item.get("status", "")).lower() == normalized_status
                or str(item.get("action", "")).lower() in allowed_actions
            ]
        return {"total": len(items), "items": items[:limit]}
    except Exception as exc:
        logger.error(f"获取学习历史失败: {exc}")
        return {"total": 0, "items": []}


@router.post("/api/learning/approve")
async def approve_learning(request: LearningApprovalRequest):
    try:
        facade = _require_learning_facade()
        if request.item_type == "knowledge_review":
            result = facade.review_generated_knowledge(
                request.item_id,
                approved=True,
                answer=request.approved_answer or "",
            )
            success = bool(result.get("success"))
            message = result.get("message") or "已批准入库"
        else:
            success = facade.approve_pending(
                request.item_id,
                approved_answer=request.approved_answer or "",
                category=request.category or "",
            )
            message = "已批准入库"
        if success:
            return {"message": message, "item_id": request.item_id}
        return JSONResponse(status_code=404, content={"message": "待验证项不存在"})
    except Exception as exc:
        logger.error(f"批准学习内容失败: {exc}")
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.post("/api/learning/reject")
async def reject_learning(request: LearningRejectionRequest):
    try:
        reason = request.reason or "内容不符合要求"
        facade = _require_learning_facade()
        if request.item_type == "knowledge_review":
            result = facade.review_generated_knowledge(
                request.item_id,
                approved=False,
                reason=reason,
            )
            success = bool(result.get("success"))
            message = result.get("message") or "已拒绝"
        else:
            success = facade.reject_pending(request.item_id, reason)
            message = "已拒绝"
        if success:
            return {"message": message, "item_id": request.item_id, "reason": reason}
        return JSONResponse(status_code=404, content={"message": "待验证项不存在"})
    except Exception as exc:
        logger.error(f"拒绝学习内容失败: {exc}")
        return JSONResponse(status_code=500, content={"message": str(exc)})
