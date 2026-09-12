from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from src.common.document_structuring_service import get_document_structuring_service
from src.web.dependencies.enterprise import bind_request_tenant_context
from src.web.routes.upload_common import parse_upload_tags, read_and_validate_upload

router = APIRouter(tags=["Document Structuring"])


class DocumentStructuringUploadResponse(BaseModel):
    success: bool
    message: str
    document_source_id: str
    file_id: str
    upload_job_id: str
    parse_job_id: str = ""
    structure_job_id: str = ""
    enterprise_id: str
    category_hint: str
    tags: list[str]
    status: str


class StructureDocumentRequest(BaseModel):
    structurer_type: str = "auto"
    draft_only: bool = True
    max_drafts: int = 500
    regenerate: bool = False


class UpdateDraftRequest(BaseModel):
    question: str
    answer: str
    category: str = "other"
    domain: str = ""
    topic: str = ""
    tags: list[str] = []
    keywords: list[str] = []
    aliases: list[str] = []
    review_comment: str = ""
    edited_by: str = ""


class BatchDraftActionRequest(BaseModel):
    draft_ids: list[str]
    review_comment: str = ""
    operator: str = ""


class BackfillPublishedKnowledgeRequest(BaseModel):
    overwrite: bool = False


@router.post("/api/doc-structuring/upload", response_model=DocumentStructuringUploadResponse)
async def upload_document_for_structuring(
    request: Request,
    file: UploadFile = File(...),
    enterprise_id: str = Form("default"),
    category_hint: str = Form("other"),
    tags: str = Form(""),
    auto_parse: bool = Form(True),
    auto_generate_drafts: bool = Form(True),
    draft_only: bool = Form(True),
    uploaded_by: str = Form(""),
):
    try:
        tenant_context = bind_request_tenant_context(
            request,
            enterprise_id=enterprise_id,
        )
        normalized_enterprise_id = tenant_context["enterprise_id"]
        normalized_tags = parse_upload_tags(tags)
        filename, content = await read_and_validate_upload(file)
        result = get_document_structuring_service().upload_document(
            enterprise_id=normalized_enterprise_id,
            filename=filename,
            content=content,
            category_hint=category_hint,
            tags=normalized_tags,
            auto_parse=auto_parse,
            auto_generate_drafts=auto_generate_drafts,
            draft_only=draft_only,
            uploaded_by=uploaded_by,
        )
        return {
            "success": True,
            "message": "文档上传成功，已进入整理工作台",
            "document_source_id": result["document_source_id"],
            "file_id": result["file_id"],
            "upload_job_id": result["upload_job_id"],
            "parse_job_id": result.get("parse_job_id", ""),
            "structure_job_id": result.get("structure_job_id", ""),
            "enterprise_id": normalized_enterprise_id,
            "category_hint": str(category_hint or "").strip() or "other",
            "tags": normalized_tags,
            "status": result["status"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"文档上传失败: {exc}") from exc


@router.get("/api/doc-structuring/documents")
async def list_document_structuring_documents(
    request: Request,
    enterprise_id: str = Query("default"),
    status: str = Query("all"),
    keyword: str = Query(""),
    file_type: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
    payload = get_document_structuring_service().list_documents(
        enterprise_id=tenant_context["enterprise_id"],
        status=status,
        keyword=keyword,
        file_type=file_type,
        page=page,
        page_size=page_size,
    )
    return {
        "success": True,
        "data": payload["items"],
        "pagination": payload["pagination"],
    }


@router.get("/api/doc-structuring/documents/{document_id}")
async def get_document_structuring_document_detail(
    document_id: str,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
    detail = get_document_structuring_service().get_document_detail(
        document_id,
        enterprise_id=tenant_context["enterprise_id"],
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {
        "success": True,
        "data": detail,
    }


@router.get("/api/doc-structuring/documents/{document_id}/history")
async def get_document_structuring_document_history(
    document_id: str,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
    detail = get_document_structuring_service().get_document_history(
        document_id,
        enterprise_id=tenant_context["enterprise_id"],
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {
        "success": True,
        "data": detail,
    }


@router.delete("/api/doc-structuring/documents/{document_id}")
async def delete_document_structuring_document(
    document_id: str,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    try:
        tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
        result = get_document_structuring_service().delete_document(
            document_id,
            enterprise_id=tenant_context["enterprise_id"],
        )
        return {
            "success": True,
            "message": "文档删除成功",
            "data": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/doc-structuring/documents/{document_id}/parse")
async def parse_document_structuring_document(
    document_id: str,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    try:
        tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
        result = get_document_structuring_service().parse_document(
            document_id,
            enterprise_id=tenant_context["enterprise_id"],
        )
        return {
            "success": True,
            "message": "文档解析完成",
            "data": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/doc-structuring/documents/{document_id}/structure")
async def structure_document_structuring_document(
    document_id: str,
    payload: StructureDocumentRequest,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    try:
        tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
        result = get_document_structuring_service().structure_document(
            document_id,
            enterprise_id=tenant_context["enterprise_id"],
            structurer_type=payload.structurer_type,
            draft_only=payload.draft_only,
            max_drafts=payload.max_drafts,
            regenerate=payload.regenerate,
        )
        return {
            "success": True,
            "message": "知识草稿生成完成",
            "data": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/doc-structuring/drafts")
async def list_document_structuring_drafts(
    request: Request,
    enterprise_id: str = Query("default"),
    document_id: str = Query(""),
    review_status: str = Query("all"),
    category: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
    payload = get_document_structuring_service().list_drafts(
        enterprise_id=tenant_context["enterprise_id"],
        document_id=document_id,
        review_status=review_status,
        category=category,
        page=page,
        page_size=page_size,
    )
    return {
        "success": True,
        "data": payload["items"],
        "pagination": payload["pagination"],
    }


@router.get("/api/doc-structuring/drafts/{draft_id}")
async def get_document_structuring_draft_detail(
    draft_id: str,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
    detail = get_document_structuring_service().get_draft_detail(
        draft_id,
        enterprise_id=tenant_context["enterprise_id"],
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return {
        "success": True,
        "data": detail,
    }


@router.put("/api/doc-structuring/drafts/{draft_id}")
async def update_document_structuring_draft(
    draft_id: str,
    payload: UpdateDraftRequest,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    try:
        tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
        updated = get_document_structuring_service().update_draft(
            draft_id,
            enterprise_id=tenant_context["enterprise_id"],
            question=payload.question,
            answer=payload.answer,
            category=payload.category,
            domain=payload.domain,
            topic=payload.topic,
            tags=payload.tags,
            keywords=payload.keywords,
            aliases=payload.aliases,
            review_comment=payload.review_comment,
            edited_by=payload.edited_by,
        )
        return {
            "success": True,
            "message": "草稿更新成功",
            "data": updated,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/doc-structuring/drafts/batch-approve")
async def approve_document_structuring_drafts(
    payload: BatchDraftActionRequest,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    try:
        tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
        result = get_document_structuring_service().approve_drafts(
            payload.draft_ids,
            enterprise_id=tenant_context["enterprise_id"],
            review_comment=payload.review_comment,
            reviewed_by=payload.operator,
        )
        return {
            "success": True,
            "message": "草稿审核通过成功",
            "data": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/doc-structuring/drafts/batch-reject")
async def reject_document_structuring_drafts(
    payload: BatchDraftActionRequest,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    try:
        tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
        result = get_document_structuring_service().reject_drafts(
            payload.draft_ids,
            enterprise_id=tenant_context["enterprise_id"],
            review_comment=payload.review_comment,
            reviewed_by=payload.operator,
        )
        return {
            "success": True,
            "message": "草稿拒绝成功",
            "data": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/doc-structuring/drafts/{draft_id}/publish")
async def publish_document_structuring_draft(
    draft_id: str,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
    operator: str = Query(""),
):
    try:
        tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
        result = get_document_structuring_service().publish_draft(
            draft_id,
            enterprise_id=tenant_context["enterprise_id"],
            reviewed_by=operator,
        )
        return {
            "success": True,
            "message": "草稿发布成功",
            "data": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/doc-structuring/drafts/batch-publish")
async def batch_publish_document_structuring_drafts(
    payload: BatchDraftActionRequest,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    try:
        tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
        result = get_document_structuring_service().batch_publish_drafts(
            payload.draft_ids,
            enterprise_id=tenant_context["enterprise_id"],
            reviewed_by=payload.operator,
        )
        failed_count = int(result.get("failed_count") or 0)
        success_count = int(result.get("count") or 0)
        total_count = int(result.get("total_count") or 0)
        if failed_count and success_count:
            message = f"批量发布部分成功：成功 {success_count} 条，失败 {failed_count} 条"
            success = False
        elif failed_count and not success_count:
            message = f"批量发布失败：共 {total_count} 条，失败 {failed_count} 条"
            success = False
        else:
            message = "草稿批量发布成功"
            success = True
        return {
            "success": success,
            "message": message,
            "data": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/doc-structuring/published-knowledge/backfill")
async def backfill_document_structuring_published_knowledge(
    payload: BackfillPublishedKnowledgeRequest,
    request: Request,
    enterprise_id: Optional[str] = Query(None),
):
    try:
        tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
        result = get_document_structuring_service().backfill_published_knowledge_records(
            enterprise_id=tenant_context["enterprise_id"],
            overwrite=payload.overwrite,
        )
        return {
            "success": True,
            "message": "历史已发布知识回填完成",
            "data": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/doc-structuring/jobs/{job_id}")
async def get_document_structuring_job_detail(job_id: str):
    detail = get_document_structuring_service().get_job_detail(job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "success": True,
        "data": detail,
    }


@router.get("/api/doc-structuring/stats")
async def get_document_structuring_stats(
    request: Request,
    enterprise_id: str = Query("default"),
):
    tenant_context = bind_request_tenant_context(request, enterprise_id=enterprise_id)
    stats = get_document_structuring_service().get_stats(
        enterprise_id=tenant_context["enterprise_id"],
    )
    return {
        "success": True,
        "data": stats,
    }
