from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from src.common.unified_knowledge_service import get_unified_knowledge_service
from src.common.rag.exceptions import (
    KnowledgeBaseError,
    RetrievalError,
)
from src.infrastructure.runtime_paths import get_chroma_dir
from src.web.dependencies.enterprise import bind_request_tenant_context
from src.web.dependencies.ai import get_enhanced_customer_ai_service
from src.web.routes.upload_common import (
    build_upload_response,
    build_upload_service,
    infer_upload_category,
    normalize_chunk_size,
    normalize_chunk_strategy,
    parse_upload_tags,
    read_and_validate_upload,
    serialize_uploaded_file,
)

router = APIRouter(tags=["RAG"])


@router.get("/api/rag/health")
async def rag_health_check():
    """
    RAG 系统健康检查端点。

    用于：
    - 负载均衡健康探测
    - 运维监控告警
    - 部署后冒烟测试

    Returns:
        - 200: 系统正常，返回知识库条数、缓存命中率、运行指标
        - 503: 知识库不可用等异常
    """
    started = time.time()
    try:
        kb = get_unified_knowledge_service()
        knowledge_count = 0
        try:
            # 优先使用公开方法，回退到内部属性
            getter = getattr(kb, "get_all_items", None) or getattr(kb, "list_items", None)
            if callable(getter):
                items = getter()
            else:
                items = getattr(kb, "_items", []) or []
            knowledge_count = len(items)
        except (KnowledgeBaseError, AttributeError, TypeError) as e:
            logger.warning(f"健康检查: 获取知识条目数失败 - {e}")

        # 检索链路简单探针
        probe_ok = True
        probe_error: Optional[str] = None
        try:
            await asyncio.to_thread(kb.search, "__health_probe__", 1)
        except (RetrievalError, KnowledgeBaseError) as e:
            probe_ok = False
            probe_error = str(e)
        except Exception as e:  # 兜底捕获，避免健康检查影响主流程
            probe_ok = False
            probe_error = str(e)

        status = "ok" if probe_ok and knowledge_count >= 0 else "degraded"
        payload = {
            "status": status,
            "knowledge_count": knowledge_count,
            "retrieval_probe_ok": probe_ok,
            "probe_error": probe_error,
            "latency_ms": int((time.time() - started) * 1000),
            "timestamp": time.time(),
        }
        if probe_ok:
            return payload
        return JSONResponse(status_code=503, content=payload)
    except (KnowledgeBaseError, ImportError) as e:
        logger.warning(f"RAG 健康检查失败: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "message": str(e),
                "latency_ms": int((time.time() - started) * 1000),
                "timestamp": time.time(),
            },
        )


@router.get("/api/rag/audit-log")
async def get_content_audit_log(limit: int = 50):
    """
    获取知识库内容审计日志。

    用于：
    - 审查最近一次敏感词命中
    - 排查脱敏效果
    """
    try:
        from src.common.rag.content_auditor import get_content_auditor
        auditor = get_content_auditor()
        return {
            "status": "ok",
            "count": min(limit, 500),
            "items": auditor.get_audit_log(limit=min(limit, 500)),
        }
    except ImportError as e:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": str(e)},
        )


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


class RAGEvaluateRequest(BaseModel):
    query: str
    answer: str
    sources: List[str] = Field(default_factory=list)
    is_resolved: Optional[bool] = None
    user_feedback: Optional[Dict[str, Any]] = None


class RAGSearchRequest(BaseModel):
    query: str
    enterprise_id: Optional[str] = None
    top_k: int = 5


class OptimizedSearchRequest(BaseModel):
    query: str
    enterprise_id: Optional[str] = None
    top_k: int = 5
    mode: str = "adaptive"
    enable_self_query: bool = True
    enable_compression: bool = True
    enable_multi_hop: bool = True


class FileBatchDeleteRequest(BaseModel):
    file_ids: list
    enterprise_id: Optional[str] = None


def _serialize_search_results(results: List[Any]) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for item, score in results:
        item_metadata = getattr(item, "metadata", {}) or {}
        serialized.append(
            {
                "id": getattr(item, "id", ""),
                "doc_id": getattr(item, "id", ""),
                "question": getattr(item, "question", ""),
                "answer": getattr(item, "answer", ""),
                "category": getattr(item, "category", ""),
                "score": score,
                "content": f"{getattr(item, 'question', '')}\n{getattr(item, 'answer', '')}".strip(),
                "chunk_quality": item_metadata.get("chunkQuality", {}),
                "metadata": {
                    "source": "unified_knowledge_service",
                    "source_type": getattr(item, "source_type", ""),
                    "tags": getattr(item, "tags", []),
                    "keywords": getattr(item, "keywords", []),
                    "document_id": item_metadata.get("documentId", ""),
                    "chunk_id": item_metadata.get("chunkId", ""),
                    "original_name": item_metadata.get("originalName", ""),
                    "chunk_quality": item_metadata.get("chunkQuality", {}),
                },
            }
        )
    return serialized


@router.post("/api/rag/evaluate")
async def evaluate_rag(request: RAGEvaluateRequest):
    service = get_enhanced_customer_ai_service()
    result = service._rag_evaluator.evaluate(
        query=request.query,
        answer=request.answer,
        contexts=request.sources,
    )
    return {
        "query": result.query,
        "answer": result.answer,
        "contexts": result.contexts,
        "overall_score": result.overall_score,
        "faithfulness": result.faithfulness,
        "answer_relevance": result.answer_relevance,
        "context_relevance": result.context_relevance,
        "context_recall": result.context_recall,
        "hallucination_score": result.hallucination_score,
        "hallucination_segments": result.hallucination_segments,
        "details": result.details,
    }


@router.get("/api/rag/metrics")
async def get_rag_metrics_summary(days: int = 7):
    service = get_enhanced_customer_ai_service()
    evaluator = getattr(service, "_rag_evaluator", None)
    if not evaluator or not hasattr(evaluator, "get_metrics_summary"):
        return {"error": "RAG评估指标摘要不可用"}
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    return evaluator.get_metrics_summary(start_date, end_date)


@router.get("/api/rag/metrics/daily")
async def get_rag_daily_metrics(date: Optional[str] = None):
    service = get_enhanced_customer_ai_service()
    stats = getattr(service, "_rag_evaluator", None)
    if not stats or not hasattr(stats, "get_daily_stats"):
        return {"error": "RAG评估器不可用"}
    target_date = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now()
    return stats.get_daily_stats(target_date)


@router.get("/api/rag/observability-samples")
async def get_rag_observability_samples(limit: int = 50):
    service = get_enhanced_customer_ai_service()
    return {
        "success": True,
        "total": min(max(int(limit or 50), 1), 200),
        "samples": service.export_mainline_observability_samples(limit=min(max(int(limit or 50), 1), 200)),
    }


@router.get("/api/rag/evaluation-dataset")
async def get_rag_evaluation_dataset(limit: int = 50):
    service = get_enhanced_customer_ai_service()
    dataset = service.build_minimum_rag_evaluation_dataset(limit=min(max(int(limit or 50), 1), 200))
    return {
        "success": True,
        "total": len(dataset),
        "items": dataset,
    }


@router.get("/api/rag/evaluation-dataset/structured")
async def get_structured_rag_evaluation_dataset(limit: int = 50):
    service = get_enhanced_customer_ai_service()
    dataset = service.build_structured_rag_evaluation_dataset(limit=min(max(int(limit or 50), 1), 500))
    return {
        "success": True,
        "total": len(dataset),
        "items": dataset,
    }


@router.get("/api/rag/evaluation-report")
async def get_rag_evaluation_report(limit: int = 50):
    service = get_enhanced_customer_ai_service()
    report = service.build_rag_evaluation_report(limit=min(max(int(limit or 50), 1), 500))
    return {
        "success": True,
        "report": report,
    }


@router.post("/api/rag/search")
async def rag_search(request: RAGSearchRequest, http_request: Request):
    try:
        tenant_context = bind_request_tenant_context(
            http_request,
            enterprise_id=request.enterprise_id,
        )
        enterprise_id = tenant_context["enterprise_id"]
        _schedule_retrieval_warmup()
        results = get_unified_knowledge_service().search(
            query=request.query,
            top_k=request.top_k,
            enterprise_id=enterprise_id,
            allow_vector_init=False,
        )
        serialized = _serialize_search_results(results)
        return {"success": True, "total": len(serialized), "results": serialized}
    except Exception as exc:
        logger.error(f"RAG搜索失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.post("/api/rag/search/optimized")
async def optimized_search(request: OptimizedSearchRequest, http_request: Request):
    try:
        tenant_context = bind_request_tenant_context(
            http_request,
            enterprise_id=request.enterprise_id,
        )
        enterprise_id = tenant_context["enterprise_id"]
        _schedule_retrieval_warmup()
        retrieval_options = {
            "mode": request.mode,
            "enable_self_query": request.enable_self_query,
            "enable_compression": request.enable_compression,
            "enable_multi_hop": request.enable_multi_hop,
        }
        service = get_unified_knowledge_service()
        results = service.search(
            query=request.query,
            top_k=request.top_k,
            enterprise_id=enterprise_id,
            allow_vector_init=False,
            retrieval_options=retrieval_options,
        )
        retrieval_plan = service._ensure_retrieval_policy_service().build_plan(
            query=request.query,
            enterprise_id=enterprise_id,
            retrieval_options={
                **retrieval_options,
                "allow_vector_init": False,
            },
        )
        serialized = _serialize_search_results(results)
        return {
            "success": True,
            "query": request.query,
            "refined_query": retrieval_plan.routing_query or request.query,
            "strategy_used": "unified_search_with_planner",
            "total": len(serialized),
            "results": serialized,
            "metadata": {
                "source": "unified_knowledge_service",
                "compat_mode": "optimized_search_planned_via_unified",
                "requested_mode": request.mode,
                "requested_options": {
                    "enable_self_query": request.enable_self_query,
                    "enable_compression": request.enable_compression,
                    "enable_multi_hop": request.enable_multi_hop,
                },
                "applied_plan": {
                    "requested_mode": retrieval_plan.requested_mode,
                    "enabled_sources": retrieval_plan.enabled_sources,
                    "per_source_top_k": retrieval_plan.per_source_top_k,
                    "enable_query_rewrite": retrieval_plan.enable_query_rewrite,
                    "enable_query_expansion": retrieval_plan.enable_query_expansion,
                    "enable_semantic_dedup": retrieval_plan.enable_semantic_dedup,
                    "enable_relevance_filter": retrieval_plan.enable_relevance_filter,
                    "notes": retrieval_plan.notes,
                },
            },
        }
    except Exception as exc:
        logger.error(f"优化搜索失败: {exc}")
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.get("/api/rag/search/stats")
async def get_search_stats():
    try:
        return {
            "success": True,
            "cache": {
                "enabled": False,
                "backend": "unified_knowledge_service",
            },
            "retrieval_chain": {
                "primary": "UnifiedKnowledgeService.search",
                "rerank": "internal",
                "compat_endpoint": "/api/rag/search/optimized",
            },
        }
    except Exception as exc:
        logger.error(f"获取统计信息失败: {exc}")
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.post("/api/rag/search/cache/clear")
async def clear_search_cache():
    try:
        return {"success": True, "message": "统一检索主链未启用独立搜索缓存，无需清理"}
    except Exception as exc:
        logger.error(f"清空缓存失败: {exc}")
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.post("/api/rag/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    enterprise_id: str = Form(""),
    category: str = Form(""),
    tags: str = Form(""),
    chunk_strategy: str = Form("recursive"),
    chunk_size: int = Form(500),
    auto_process: bool = Form(True),
):
    try:
        tenant_context = bind_request_tenant_context(
            request,
            enterprise_id=enterprise_id,
        )
        resolved_enterprise_id = tenant_context["enterprise_id"]
        filename, content = await read_and_validate_upload(file)
        upload_service = build_upload_service()
        tag_list = parse_upload_tags(tags)
        resolved_category = str(category or "").strip() or infer_upload_category(
            filename,
            content,
            enterprise_id=resolved_enterprise_id,
        )
        resolved_chunk_strategy = normalize_chunk_strategy(chunk_strategy)
        resolved_chunk_size = normalize_chunk_size(chunk_size)
        if hasattr(upload_service, "setChunkStrategy"):
            upload_service.setChunkStrategy(resolved_chunk_strategy)
        uploaded_file = await upload_service.uploadFile(
            enterpriseId=resolved_enterprise_id,
            fileContent=content,
            fileName=filename,
            category=resolved_category,
            tags=tag_list,
            autoProcess=bool(auto_process),
            chunkStrategy=resolved_chunk_strategy,
            chunkSize=resolved_chunk_size,
        )
        return build_upload_response(
            uploaded_file=uploaded_file,
            enterprise_id=resolved_enterprise_id,
            category=resolved_category,
            tags=tag_list,
            chunk_strategy=str(getattr(resolved_chunk_strategy, "value", resolved_chunk_strategy)),
            chunk_size=resolved_chunk_size,
        )
    except ImportError as exc:
        logger.error(f"导入RAG模块失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": f"RAG服务未启用: {str(exc)}"})
    except ValueError as exc:
        logger.error(f"文件验证失败: {exc}")
        return JSONResponse(status_code=400, content={"success": False, "message": str(exc)})
    except Exception as exc:
        logger.error(f"文件上传失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/rag/files")
async def get_uploaded_files(request: Request, enterprise_id: Optional[str] = None, status: Optional[str] = None):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        tenant_context = bind_request_tenant_context(
            request,
            enterprise_id=enterprise_id,
        )
        resolved_enterprise_id = tenant_context["enterprise_id"]
        upload_service = get_enterprise_file_upload_service()
        files = upload_service.listFiles(enterpriseId=resolved_enterprise_id, status=status or "all")
        return {
            "total": len(files),
            "files": [
                serialize_uploaded_file(
                    file,
                    upload_time_key="upload_time",
                    processed_time_key="processed_time",
                )
                for file in files
            ],
        }
    except ImportError:
        return {"total": 0, "files": []}
    except Exception as exc:
        logger.error(f"获取文件列表失败: {exc}")
        return {"total": 0, "files": []}


@router.post("/api/rag/files/{file_id}/process")
async def process_uploaded_file(file_id: str, request: Request, enterprise_id: Optional[str] = None):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        tenant_context = bind_request_tenant_context(
            request,
            enterprise_id=enterprise_id,
        )
        ent_id = tenant_context["enterprise_id"]
        result = await get_enterprise_file_upload_service().processFile(enterpriseId=ent_id, fileId=file_id)
        return {"success": True, "message": "文件处理完成", "file": result.toDict() if hasattr(result, "toDict") else {"id": file_id}}
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "RAG服务未启用"})
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"success": False, "message": str(exc)})
    except Exception as exc:
        logger.error(f"处理上传文件失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.delete("/api/rag/files/{file_id}")
async def delete_uploaded_file(file_id: str, request: Request, enterprise_id: Optional[str] = None):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        upload_service = get_enterprise_file_upload_service()
        tenant_context = bind_request_tenant_context(
            request,
            enterprise_id=enterprise_id,
        )
        resolved_enterprise_id = tenant_context["enterprise_id"]
        success = upload_service.deleteFile(enterpriseId=resolved_enterprise_id, fileId=file_id)
        if success:
            return {"success": True, "message": "删除成功"}
        return JSONResponse(status_code=404, content={"success": False, "message": "文件不存在"})
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "RAG服务未启用"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.post("/api/rag/files/batch-delete")
async def batch_delete_uploaded_files(request: FileBatchDeleteRequest, http_request: Request):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        upload_service = get_enterprise_file_upload_service()
        tenant_context = bind_request_tenant_context(
            http_request,
            enterprise_id=request.enterprise_id,
        )
        resolved_enterprise_id = tenant_context["enterprise_id"]
        success_count = 0
        fail_count = 0
        for file_id in request.file_ids:
            try:
                success = upload_service.deleteFile(
                    enterpriseId=resolved_enterprise_id,
                    fileId=file_id,
                )
                if success:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as exc:
                logger.error(f"批量删除文件失败 {file_id}: {exc}")
                fail_count += 1

        return {
            "success": True,
            "message": f"成功删除 {success_count} 个文件，失败 {fail_count} 个",
            "success_count": success_count,
            "fail_count": fail_count,
        }
    except ImportError:
        return JSONResponse(status_code=500, content={"message": "RAG服务未启用"})
    except Exception as exc:
        logger.error(f"批量删除文件失败: {exc}")
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.get("/api/rag/stats")
async def get_rag_stats(request: Request, enterprise_id: Optional[str] = None):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service
        from src.rag.vector_store import ChromaVectorStore, ChromaConfig

        tenant_context = bind_request_tenant_context(
            request,
            enterprise_id=enterprise_id,
        )
        ent_id = tenant_context["enterprise_id"]
        upload_service = get_enterprise_file_upload_service()
        storage_stats = upload_service.getStorageStats(ent_id)
        try:
            config = ChromaConfig(persistDirectory=str(get_chroma_dir()), collectionPrefix="enterprise_")
            vector_store = ChromaVectorStore(config)
            vector_stats = vector_store.getCollectionStats(ent_id)
        except Exception as exc:
            logger.warning(f"获取向量存储统计失败: {exc}")
            vector_stats = {"count": 0, "name": "N/A", "metadata": {}}

        return {"success": True, "enterprise_id": ent_id, "storage": storage_stats, "vector_store": vector_stats}
    except ImportError:
        return JSONResponse(status_code=500, content={"message": "RAG服务未启用"})
    except Exception as exc:
        logger.error(f"获取RAG统计失败: {exc}")
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.get("/api/rag/files/{file_id}/content")
async def get_file_content(file_id: str, request: Request, enterprise_id: Optional[str] = None):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        upload_service = get_enterprise_file_upload_service()
        tenant_context = bind_request_tenant_context(
            request,
            enterprise_id=enterprise_id,
        )
        resolved_enterprise_id = tenant_context["enterprise_id"]
        content = upload_service.getFileContent(enterpriseId=resolved_enterprise_id, fileId=file_id)
        if content is not None:
            return {"success": True, "content": content}
        return JSONResponse(status_code=404, content={"success": False, "message": "文件不存在或内容为空"})
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "RAG服务未启用"})
    except Exception as exc:
        logger.error(f"获取文件内容失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})
