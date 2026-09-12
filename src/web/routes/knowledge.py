from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from src.common.unified_knowledge_service import build_trigger_keywords, get_unified_knowledge_service
from src.infrastructure.runtime_paths import get_chroma_dir
from src.web.dependencies.enterprise import bind_request_tenant_context

router = APIRouter(tags=["知识库管理"])


def _to_item_dict(item: Any) -> Dict[str, Any]:
    if item is None:
        return {}
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "to_dict"):
        try:
            return dict(item.to_dict() or {})
        except Exception:
            return {}
    return {}


def _schedule_retrieval_warmup():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _warmup():
        try:
            await asyncio.to_thread(get_unified_knowledge_service().warmup_retrieval_chain)
        except Exception as exc:
            logger.warning(f"后台预热统一检索链失败: {exc}")

    loop.create_task(_warmup())


class KnowledgeAddRequest(BaseModel):
    """添加知识请求"""

    question: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    category: str = "other"
    domain: str = ""
    topic: str = ""
    keywords: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list)
    reply_templates: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    enterprise_id: str = "default"


class KnowledgeUpdateRequest(BaseModel):
    """更新知识请求"""

    question: Optional[str] = None
    answer: Optional[str] = None
    category: Optional[str] = None
    domain: Optional[str] = None
    topic: Optional[str] = None
    keywords: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    aliases: Optional[List[str]] = None
    reply_templates: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    priority: Optional[int] = None
    enterprise_id: Optional[str] = None


class KnowledgeBatchRequest(BaseModel):
    item_ids: list
    action: str


class KnowledgeTestMatchRequest(BaseModel):
    """知识匹配测试请求"""

    query: str
    top_k: int = 5


class KnowledgeImportRequest(BaseModel):
    """知识导入请求"""

    items: list = []
    source: str = "main"


class KnowledgeKeywordBackfillRequest(BaseModel):
    force_full_resync: bool = True
    sync_document_structuring_records: bool = True
    overwrite_published_records: bool = True


@router.get("/api/knowledge")
async def get_knowledge_list(
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
):
    """获取知识库列表。"""
    unified_service = get_unified_knowledge_service()
    items = unified_service.get_items()

    if search:
        search_lower = search.lower()
        filtered_items = []
        for item in items:
            score = 0
            if search_lower in item.question.lower():
                score += 50
            if search_lower in item.answer.lower():
                score += 30
            for kw in item.keywords:
                if search_lower in kw.lower():
                    score += 40
            for alias in item.aliases:
                if search_lower in alias.lower():
                    score += 35
            if score > 0:
                filtered_items.append((item, score))

        filtered_items.sort(key=lambda x: x[1], reverse=True)
        items = [item for item, _score in filtered_items]

    total = len(items)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_items = items[start_idx:end_idx]

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "items": [item.to_dict() for item in page_items],
    }


@router.get("/api/knowledge/stats")
async def get_knowledge_stats():
    """获取知识库统计数据。"""
    unified_service = get_unified_knowledge_service()
    return unified_service.get_statistics()


@router.get("/api/knowledge/statistics/overview")
async def get_knowledge_statistics_overview():
    """获取知识库统计概览。"""
    try:
        unified_service = get_unified_knowledge_service()
        stats = unified_service.get_statistics()
        # 概览卡片需要展示真实向量文档数，不能退回到本地 manifest 快照。
        vector_stats = unified_service.get_vector_stats(warm=True)

        return {
            "success": True,
            "statistics": {
                "total_count": stats["total_count"],
                "enabled_count": stats["enabled_count"],
                "disabled_count": stats["disabled_count"],
                "vector_synced_count": vector_stats.get("count", 0),
                "total_use_count": stats["total_use_count"],
                "category_distribution": stats["category_distribution"],
                "source_distribution": stats["source_distribution"],
                "top_used": stats["top_used"],
                "vector_count": vector_stats.get("count", 0),
            },
        }
    except Exception as e:
        logger.error(f"获取知识库统计概览失败: {e}")
        return {
            "success": False,
            "statistics": {
                "total_count": 0,
                "enabled_count": 0,
                "disabled_count": 0,
                "vector_synced_count": 0,
                "total_use_count": 0,
                "category_distribution": {},
                "source_distribution": {},
                "top_used": [],
            },
        }


@router.get("/api/knowledge/vector/stats")
async def get_knowledge_vector_stats(warm: bool = False):
    """获取向量数据库统计信息。"""
    try:
        unified_service = get_unified_knowledge_service()
        vector_stats = unified_service.get_vector_stats(warm=warm)
        return {"success": True, "data": vector_stats}
    except Exception as e:
        logger.error(f"获取向量数据库统计失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/knowledge/vector/resync")
async def resync_knowledge_to_vector():
    """重新同步知识库到向量数据库。"""
    try:
        unified_service = get_unified_knowledge_service()
        result = unified_service.resync_to_vector()
        return {"success": result.get("success", False), "data": result}
    except Exception as e:
        logger.error(f"重新同步向量数据库失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/knowledge/keywords/backfill")
async def backfill_knowledge_keywords(
    payload: KnowledgeKeywordBackfillRequest,
    http_request: Request,
):
    """批量回洗历史知识关键词，并按需同步文档整理镜像与向量索引。"""
    unified_service = get_unified_knowledge_service()
    tenant_context = bind_request_tenant_context(http_request)
    enterprise_id = tenant_context["enterprise_id"]
    backfill_result = unified_service.backfill_trigger_keywords(
        enterprise_id=enterprise_id,
        sync_vector=True,
        force_full_resync=payload.force_full_resync,
    )
    document_result = None
    if payload.sync_document_structuring_records:
        from src.common.document_structuring_service import get_document_structuring_service

        document_result = get_document_structuring_service().sync_published_keyword_mirrors(
            enterprise_id=enterprise_id,
            overwrite_records=payload.overwrite_published_records,
        )
    return {
        "success": True,
        "message": "历史知识关键词回洗完成",
        "data": {
            "knowledge_backfill": backfill_result,
            "document_sync": document_result,
        },
    }


@router.get("/api/vector-db/info")
async def get_vector_db_info():
    """获取向量数据库详细信息。"""
    try:
        from src.rag.vector_store import ChromaConfig, ChromaVectorStore

        config = ChromaConfig(persistDirectory=str(get_chroma_dir()), collectionPrefix="enterprise_")
        vector_store = ChromaVectorStore(config)
        info = vector_store.getDatabaseInfo()
        return {"success": True, "data": info}
    except Exception as e:
        logger.error(f"获取向量数据库信息失败: {e}")
        return {"success": False, "error": str(e)}


@router.get("/api/vector-db/health")
async def check_vector_db_health():
    """向量数据库健康检查。"""
    try:
        from src.rag.vector_store import ChromaConfig, ChromaVectorStore

        config = ChromaConfig(persistDirectory=str(get_chroma_dir()), collectionPrefix="enterprise_")
        vector_store = ChromaVectorStore(config)
        health = vector_store.healthCheck()
        return {"success": True, "health": health}
    except Exception as e:
        logger.error(f"向量数据库健康检查失败: {e}")
        return {"success": False, "error": str(e)}


@router.get("/api/vector-db/collections")
async def list_vector_db_collections():
    """列出所有向量数据库集合。"""
    try:
        from src.rag.vector_store import ChromaConfig, ChromaVectorStore

        config = ChromaConfig(persistDirectory=str(get_chroma_dir()), collectionPrefix="enterprise_")
        vector_store = ChromaVectorStore(config)
        collections = vector_store.listCollections()
        return {"success": True, "collections": collections}
    except Exception as e:
        logger.error(f"列出向量数据库集合失败: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/api/vector-db/collection/{enterprise_id}")
async def delete_vector_db_collection(enterprise_id: str):
    """删除指定企业的向量数据库集合。"""
    try:
        from src.rag.vector_store import ChromaConfig, ChromaVectorStore

        config = ChromaConfig(persistDirectory=str(get_chroma_dir()), collectionPrefix="enterprise_")
        vector_store = ChromaVectorStore(config)

        stats = vector_store.getCollectionStats(enterprise_id)
        if not stats.get("exists", True):
            return {"success": False, "error": f"集合 {enterprise_id} 不存在"}

        deleted = vector_store.deleteEnterpriseData(enterprise_id)
        return {
            "success": deleted,
            "message": f"已删除企业 {enterprise_id} 的向量数据" if deleted else "删除失败",
        }
    except Exception as e:
        logger.error(f"删除向量数据库集合失败: {e}")
        return {"success": False, "error": str(e)}


@router.get("/api/vector-db/collection/{enterprise_id}/stats")
async def get_vector_db_collection_stats(enterprise_id: str):
    """获取指定企业向量集合的统计信息。"""
    try:
        from src.rag.vector_store import ChromaConfig, ChromaVectorStore

        config = ChromaConfig(persistDirectory=str(get_chroma_dir()), collectionPrefix="enterprise_")
        vector_store = ChromaVectorStore(config)
        stats = vector_store.getCollectionStats(enterprise_id)
        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"获取向量集合统计失败: {e}")
        return {"success": False, "error": str(e)}


@router.get("/api/knowledge/search/vector")
async def search_knowledge_vector(request: Request, q: str, top_k: int = 5, category: Optional[str] = None):
    """向量语义检索。"""
    try:
        unified_service = get_unified_knowledge_service()
        tenant_context = bind_request_tenant_context(request)
        enterprise_id = tenant_context["enterprise_id"]
        _schedule_retrieval_warmup()
        results = unified_service.search(
            q,
            top_k=top_k,
            category=category or "",
            use_vector=True,
            enterprise_id=enterprise_id,
            allow_vector_init=False,
        )
        return {
            "success": True,
            "total": len(results),
            "items": [
                {"knowledge": item.to_dict(), "score": score, "search_type": "vector"}
                for item, score in results
            ],
        }
    except Exception as e:
        logger.error(f"向量检索失败: {e}")
        return {"success": False, "error": str(e)}


@router.get("/api/knowledge/search/hybrid")
async def search_knowledge_hybrid(
    q: str,
    request: Request = None,
    top_k: int = 5,
    category: Optional[str] = None,
    vector_weight: float = 0.6,
):
    """统一主链检索视图。保留 hybrid 入口名，仅复用统一搜索主链。"""
    try:
        unified_service = get_unified_knowledge_service()
        enterprise_id = ""
        if request is not None:
            tenant_context = bind_request_tenant_context(request)
            enterprise_id = tenant_context["enterprise_id"]
        _schedule_retrieval_warmup()
        unified_results = unified_service.search(
            q,
            top_k=top_k,
            category=category or "",
            use_vector=True,
            enterprise_id=enterprise_id,
            allow_vector_init=False,
        )
        keyword_weight = 1 - vector_weight
        results = [
            {
                "knowledge": item.to_dict(),
                "hybrid_score": score,
                "vector_score": score,
                "keyword_score": score,
                "search_type": "hybrid",
            }
            for item, score in unified_results
        ]
        return {
            "success": True,
            "total": len(results),
            "items": results,
            "weights": {"vector": vector_weight, "keyword": keyword_weight},
            "strategy": "unified_mainline",
        }
    except Exception as e:
        logger.error(f"混合检索失败: {e}")
        return {"success": False, "error": str(e)}


@router.get("/api/knowledge/search")
async def search_knowledge(request: Request, q: str, top_k: int = 5, category: Optional[str] = None, source: str = "all"):
    """搜索知识。"""
    unified_service = get_unified_knowledge_service()
    tenant_context = bind_request_tenant_context(request)
    enterprise_id = tenant_context["enterprise_id"]
    _schedule_retrieval_warmup()
    results = unified_service.search(
        q,
        top_k,
        category or "",
        source,
        enterprise_id=enterprise_id,
        allow_vector_init=False,
    )
    return {
        "total": len(results),
        "items": [{"knowledge": item.to_dict(), "score": score} for item, score in results],
    }


@router.get("/api/knowledge/unified/stats")
async def get_unified_knowledge_stats(warm: bool = False):
    """获取统一知识库统计信息。"""
    try:
        unified_service = get_unified_knowledge_service()
        knowledge_stats = unified_service.get_statistics()
        vector_stats = unified_service.get_vector_stats(warm=warm)

        sync_status = "synced"
        if vector_stats.get("enabled"):
            if vector_stats.get("count", 0) < knowledge_stats.get("total_count", 0):
                sync_status = "out_of_sync"
        else:
            sync_status = "vector_disabled"

        return {
            "success": True,
            "data": {
                "knowledge_stats": {
                    "total_count": knowledge_stats["total_count"],
                    "enabled_count": knowledge_stats["enabled_count"],
                    "total_use_count": knowledge_stats["total_use_count"],
                    "category_distribution": knowledge_stats["category_distribution"],
                    "source_distribution": knowledge_stats["source_distribution"],
                },
                "vector_stats": vector_stats,
                "sync_status": sync_status,
            },
        }
    except Exception as e:
        logger.error(f"获取统一知识库统计失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/knowledge/unified/sync")
async def sync_unified_knowledge():
    """同步知识库到向量数据库。"""
    try:
        unified_service = get_unified_knowledge_service()
        result = unified_service.resync_to_vector()
        return {
            "success": result.get("success", False),
            "message": f"已同步 {result.get('synced_count', 0)} 条知识到向量数据库",
            "data": result,
        }
    except Exception as e:
        logger.error(f"同步知识库失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/knowledge/unified/reload")
async def reload_unified_knowledge():
    """重新加载知识库数据并同步向量库。"""
    try:
        from src.common.unified_knowledge_service import (
            get_unified_knowledge_service,
            reset_unified_knowledge_service,
        )

        reset_unified_knowledge_service()
        unified_service = get_unified_knowledge_service()
        stats = unified_service.get_statistics()
        sync_result = unified_service.resync_to_vector()

        return {
            "success": True,
            "message": f"知识库已重新加载并同步到向量数据库，共 {stats.get('total_count', 0)} 条知识",
            "data": {
                "total_count": stats.get("total_count", 0),
                "enabled_count": stats.get("enabled_count", 0),
                "category_distribution": stats.get("category_distribution", {}),
                "vector_sync": sync_result,
            },
        }
    except Exception as e:
        logger.error(f"重新加载知识库失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/knowledge/reload")
async def reload_knowledge_alias():
    """兼容旧路径的知识库重载接口。"""
    return await reload_unified_knowledge()


@router.get("/api/knowledge/export")
async def export_knowledge(format: str = "json"):
    """导出知识库数据。"""
    try:
        import csv
        import io
        from datetime import datetime

        unified_service = get_unified_knowledge_service()
        all_items = unified_service.get_all_items()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if format.lower() == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["id", "question", "answer", "category", "keywords", "enabled", "priority", "source"])
            for item in all_items:
                item_dict = item.to_dict() if hasattr(item, "to_dict") else item
                writer.writerow(
                    [
                        item_dict.get("id", ""),
                        item_dict.get("question", ""),
                        item_dict.get("answer", ""),
                        item_dict.get("category", ""),
                        "|".join(item_dict.get("keywords", [])),
                        item_dict.get("enabled", True),
                        item_dict.get("priority", 0),
                        item_dict.get("source", ""),
                    ]
                )
            csv_content = output.getvalue()
            buffer = io.BytesIO(csv_content.encode("utf-8-sig"))
            filename = f"knowledge_base_export_{timestamp}.csv"
            return StreamingResponse(
                buffer,
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
            )

        export_data = [item.to_dict() for item in all_items]
        json_content = json.dumps(export_data, ensure_ascii=False, indent=2)
        buffer = io.BytesIO(json_content.encode("utf-8"))
        filename = f"knowledge_base_export_{timestamp}.json"
        return StreamingResponse(
            buffer,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )
    except Exception as e:
        logger.error(f"导出知识库失败: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@router.get("/api/knowledge/{item_id}")
async def get_knowledge_item(item_id: str):
    """获取单个知识条目详情。"""
    unified_service = get_unified_knowledge_service()
    item = unified_service.get_item_by_id(item_id)
    if item:
        return {"success": True, "item": item.to_dict()}
    return JSONResponse(status_code=404, content={"success": False, "message": "知识不存在"})


@router.post("/api/knowledge")
async def add_knowledge(request: KnowledgeAddRequest, http_request: Request):
    """添加知识条目。"""
    unified_service = get_unified_knowledge_service()
    tenant_context = bind_request_tenant_context(
        http_request,
        enterprise_id=request.enterprise_id,
    )
    enterprise_id = tenant_context["enterprise_id"]
    item_data = {
        "question": request.question,
        "answer": request.answer,
        "category": request.category,
        "domain": request.domain,
        "topic": request.topic,
        "keywords": build_trigger_keywords(
            request.question,
            request.answer,
            provided_keywords=request.keywords or [],
            aliases=request.aliases or [],
        ),
        "tags": request.tags or [],
        "aliases": request.aliases or [],
        "reply_templates": request.reply_templates or [],
        "metadata": request.metadata or {},
        "priority": request.priority or 0,
        "enterprise_id": enterprise_id,
    }
    item = unified_service.add_item(item_data)
    return {"success": True, "message": "添加成功", "id": item.id}


@router.put("/api/knowledge/{item_id}")
async def update_knowledge(item_id: str, request: KnowledgeUpdateRequest, http_request: Request):
    """更新知识条目。"""
    unified_service = get_unified_knowledge_service()
    tenant_context = bind_request_tenant_context(
        http_request,
        enterprise_id=request.enterprise_id,
    )
    enterprise_id = tenant_context["enterprise_id"]
    updates = {}
    if request.question is not None:
        updates["question"] = request.question
    if request.answer is not None:
        updates["answer"] = request.answer
    if request.category is not None:
        updates["category"] = request.category
    if request.domain is not None:
        updates["domain"] = request.domain
    if request.topic is not None:
        updates["topic"] = request.topic
    if request.keywords is not None:
        updates["keywords"] = request.keywords
    if request.tags is not None:
        updates["tags"] = request.tags
    if request.aliases is not None:
        updates["aliases"] = request.aliases
    if request.reply_templates is not None:
        updates["reply_templates"] = request.reply_templates
    if request.metadata is not None:
        updates["metadata"] = request.metadata
    if request.priority is not None:
        updates["priority"] = request.priority
    if request.enterprise_id is not None:
        updates["enterprise_id"] = enterprise_id
    if any(value is not None for value in (request.question, request.answer, request.keywords, request.aliases)):
        current_item = _to_item_dict(getattr(unified_service, "get_item_by_id", lambda _item_id: None)(item_id))
        effective_question = request.question if request.question is not None else current_item.get("question", "")
        effective_answer = request.answer if request.answer is not None else current_item.get("answer", "")
        effective_aliases = request.aliases if request.aliases is not None else current_item.get("aliases", [])
        effective_keywords = request.keywords if request.keywords is not None else current_item.get("keywords", [])
        updates["keywords"] = build_trigger_keywords(
            effective_question,
            effective_answer,
            provided_keywords=effective_keywords,
            aliases=effective_aliases,
        )

    item = unified_service.update_item(item_id, updates)
    if item:
        return {"success": True, "message": "更新成功"}
    return JSONResponse(status_code=404, content={"success": False, "message": "知识不存在"})


@router.delete("/api/knowledge/{item_id}")
async def delete_knowledge(item_id: str):
    """删除知识条目。"""
    unified_service = get_unified_knowledge_service()
    success = unified_service.delete_item(item_id)
    if success:
        return {"success": True, "message": "删除成功"}
    return JSONResponse(status_code=404, content={"success": False, "message": "知识不存在"})


@router.post("/api/knowledge/batch")
async def batch_knowledge_action(request: KnowledgeBatchRequest):
    """批量操作知识条目。"""
    unified_service = get_unified_knowledge_service()
    if request.action != "delete":
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "知识库页面已移除启用/禁用功能，仅支持批量删除"},
        )

    item_ids = [str(item_id or "").strip() for item_id in (request.item_ids or []) if str(item_id or "").strip()]
    if not item_ids:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "请选择要删除的知识条目"},
        )

    try:
        success_count = int(unified_service.batch_delete(item_ids))
        fail_count = max(len(item_ids) - success_count, 0)
    except Exception as e:
        logger.error(f"批量删除知识失败: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"批量删除失败: {e}"},
        )

    return {
        "success": True,
        "message": f"已删除{success_count}条，失败{fail_count}条",
        "success_count": success_count,
        "fail_count": fail_count,
    }


@router.post("/api/knowledge/test-match")
async def test_knowledge_match(request: KnowledgeTestMatchRequest, http_request: Request):
    """测试知识匹配。"""
    try:
        unified_service = get_unified_knowledge_service()
        tenant_context = bind_request_tenant_context(http_request)
        enterprise_id = tenant_context["enterprise_id"]
        raw_results = unified_service.search(request.query, top_k=request.top_k, enterprise_id=enterprise_id)
        results = []
        for item, score in raw_results:
            data: dict = (
                item.to_dict()
                if hasattr(item, "to_dict")
                else dict(item)
                if isinstance(item, dict)
                else {"data": str(item)}
            )
            data["score"] = score
            results.append(data)
        return {"success": True, "results": results}
    except Exception as e:
        logger.error(f"知识匹配测试失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/knowledge/upload")
async def upload_knowledge_file(
    request: Request,
    file: UploadFile = File(...),
    enterprise_id: str = Form("default"),
    source: str = Form(""),
):
    """上传知识文件（JSON/CSV/TXT）。"""
    try:
        import csv
        import io

        content = await file.read()
        filename = file.filename or ""
        unified_service = get_unified_knowledge_service()
        tenant_context = bind_request_tenant_context(
            request,
            enterprise_id=enterprise_id,
        )
        normalized_enterprise_id = tenant_context["enterprise_id"]
        normalized_source = (source or "").strip() or ("enterprise" if normalized_enterprise_id != "default" else "main")

        def normalize_item(item: dict) -> dict:
            merged = dict(item or {})
            merged.setdefault("source", normalized_source)
            merged.setdefault("enterprise_id", normalized_enterprise_id)
            return merged

        if filename.endswith(".json"):
            data = json.loads(content.decode("utf-8"))
            items = data if isinstance(data, list) else [data]
            count = 0
            for item in items:
                if unified_service.add_item(normalize_item(item)):
                    count += 1
            return {
                "success": True,
                "message": f"成功导入{count}条知识",
                "count": count,
                "source": normalized_source,
                "enterprise_id": normalized_enterprise_id,
            }

        if filename.endswith(".csv"):
            reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
            count = 0
            for row in reader:
                item = {
                    "question": row.get("question", ""),
                    "answer": row.get("answer", ""),
                    "category": row.get("category", "other"),
                }
                if unified_service.add_item(normalize_item(item)):
                    count += 1
            return {
                "success": True,
                "message": f"成功导入{count}条知识",
                "count": count,
                "source": normalized_source,
                "enterprise_id": normalized_enterprise_id,
            }

        if filename.endswith(".txt"):
            text = content.decode("utf-8")
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            count = 0
            i = 0
            while i < len(lines) - 1:
                question = lines[i]
                answer = lines[i + 1] if i + 1 < len(lines) else ""
                item = {"question": question, "answer": answer, "category": "other"}
                if unified_service.add_item(normalize_item(item)):
                    count += 1
                i += 2
            return {
                "success": True,
                "message": f"成功导入{count}条知识",
                "count": count,
                "source": normalized_source,
                "enterprise_id": normalized_enterprise_id,
            }

        return {"success": False, "message": "不支持的文件格式，请使用JSON、CSV或TXT"}
    except Exception as e:
        logger.error(f"知识文件上传失败: {e}")
        return {"success": False, "message": str(e)}


@router.post("/api/knowledge/import")
async def import_knowledge(request: KnowledgeImportRequest, http_request: Request):
    """导入知识（JSON数据）。"""
    try:
        unified_service = get_unified_knowledge_service()
        tenant_context = bind_request_tenant_context(http_request)
        enterprise_id = tenant_context["enterprise_id"]
        count = 0
        for item in request.items:
            item["source"] = request.source
            item.setdefault("enterprise_id", enterprise_id)
            if unified_service.add_item(item):
                count += 1
        return {"success": True, "message": f"成功导入{count}条知识", "count": count}
    except Exception as e:
        logger.error(f"知识导入失败: {e}")
        return {"success": False, "message": str(e)}


@router.get("/api/knowledge/version/{item_id}")
async def get_knowledge_version(item_id: str):
    """获取知识版本历史。"""
    try:
        unified_service = get_unified_knowledge_service()
        item = unified_service.get_item_by_id(item_id)
        if not item:
            return {"success": False, "message": "知识条目不存在"}
        item_dict = item.to_dict() if hasattr(item, "to_dict") else (item if isinstance(item, dict) else {})
        return {
            "success": True,
            "item_id": item_id,
            "versions": [
                {
                    "version": 1,
                    "updated_at": item_dict.get("updated_at", ""),
                    "question": item_dict.get("question", ""),
                    "answer": item_dict.get("answer", ""),
                }
            ],
        }
    except Exception as e:
        logger.error(f"获取知识版本失败: {e}")
        return {"success": False, "message": str(e)}


@router.post("/api/knowledge/version/{item_id}/rollback/{version_id}")
async def rollback_knowledge_version(item_id: str, version_id: str):
    """回滚知识版本。"""
    try:
        del item_id, version_id
        return {"success": False, "message": "版本回滚功能暂未实现"}
    except Exception as e:
        logger.error(f"知识版本回滚失败: {e}")
        return {"success": False, "message": str(e)}
