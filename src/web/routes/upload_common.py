from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import UploadFile

from src.common.schema_category_resolver import infer_category_from_text
from src.common.unified_knowledge_service import get_unified_knowledge_service
from src.infrastructure.config import get_config
from src.infrastructure.runtime_paths import get_chroma_dir


ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt", ".csv", ".md", ".json"}
MAX_UPLOAD_FILE_SIZE = 50 * 1024 * 1024

UPLOAD_STATUS_META: Dict[str, Dict[str, Any]] = {
    "completed": {
        "label": "已完成",
        "badge_class": "bg-success",
        "hint": "",
        "requires_ocr": False,
        "can_retry_processing": False,
    },
    "processed": {
        "label": "已完成",
        "badge_class": "bg-success",
        "hint": "",
        "requires_ocr": False,
        "can_retry_processing": False,
    },
    "processing": {
        "label": "处理中",
        "badge_class": "bg-warning",
        "hint": "文件正在解析、切片或入库中",
        "requires_ocr": False,
        "can_retry_processing": False,
    },
    "uploaded": {
        "label": "待处理",
        "badge_class": "bg-info",
        "hint": "文件已上传，等待进入处理队列",
        "requires_ocr": False,
        "can_retry_processing": True,
    },
    "failed": {
        "label": "失败",
        "badge_class": "bg-danger",
        "hint": "处理失败，请检查错误信息后重试",
        "requires_ocr": False,
        "can_retry_processing": True,
    },
    "pending_ocr": {
        "label": "待OCR",
        "badge_class": "bg-warning text-dark",
        "hint": "文档未提取到可用文本，需先完成 OCR 再重新处理",
        "requires_ocr": True,
        "can_retry_processing": True,
    },
    "cancelled": {
        "label": "已取消",
        "badge_class": "bg-secondary",
        "hint": "处理任务已取消，可按需重新处理",
        "requires_ocr": False,
        "can_retry_processing": True,
    },
    "timeout": {
        "label": "超时",
        "badge_class": "bg-secondary",
        "hint": "处理任务超时，可按需重新处理",
        "requires_ocr": False,
        "can_retry_processing": True,
    },
}


async def read_and_validate_upload(file: UploadFile) -> Tuple[str, bytes]:
    filename = file.filename or "unknown"
    file_ext = os.path.splitext(filename)[1].lower()
    if file_ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError(f"不支持的文件格式: {file_ext}，支持: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}")

    content = await file.read()
    if len(content) > MAX_UPLOAD_FILE_SIZE:
        raise ValueError(f"文件大小超过限制（最大50MB），当前: {len(content) / 1024 / 1024:.1f}MB")
    if len(content) == 0:
        raise ValueError("文件内容为空")
    return filename, content


def parse_upload_tags(tags: str | List[str] | None) -> List[str]:
    if isinstance(tags, list):
        return [str(item).strip() for item in tags if str(item).strip()]
    if not tags:
        return []
    return [item.strip() for item in str(tags).split(",") if item.strip()]


def _decode_upload_preview(content: bytes, max_bytes: int = 4096) -> str:
    preview = content[:max_bytes]
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return preview.decode(encoding, errors="ignore")
        except Exception:
            continue
    return ""


def infer_upload_category(filename: str, content: bytes, *, enterprise_id: str = "") -> str:
    text = f"{filename.lower()} {_decode_upload_preview(content).lower()}"
    return infer_category_from_text(
        text,
        default_category="other",
        enterprise_id=str(enterprise_id or "").strip(),
    )


def normalize_chunk_strategy(chunk_strategy: str):
    from src.rag.enterprise_ingestion_pipeline import ChunkStrategy

    strategy_key = (chunk_strategy or ChunkStrategy.RECURSIVE.value).strip().lower()
    strategy_map = {
        "fixed": ChunkStrategy.FIXED_SIZE,
        ChunkStrategy.FIXED_SIZE.value: ChunkStrategy.FIXED_SIZE,
        ChunkStrategy.RECURSIVE.value: ChunkStrategy.RECURSIVE,
        ChunkStrategy.SEMANTIC.value: ChunkStrategy.SEMANTIC,
        ChunkStrategy.HIERARCHICAL.value: ChunkStrategy.HIERARCHICAL,
        ChunkStrategy.SLIDING_WINDOW.value: ChunkStrategy.RECURSIVE,
    }
    return strategy_map.get(strategy_key, ChunkStrategy.RECURSIVE)


def normalize_chunk_size(chunk_size: int | None) -> int:
    return max(100, min(int(chunk_size or 500), 5000))


def build_upload_service():
    from src.rag.enterprise_file_upload import get_enterprise_file_upload_service
    from src.rag.embedding_service import build_embedding_config_from_app_config, get_embedding_service
    from src.rag.vector_store import ChromaConfig, ChromaVectorStore

    embedding_service = get_embedding_service(
        build_embedding_config_from_app_config(get_config())
    )
    vector_store = ChromaVectorStore(
        ChromaConfig(persistDirectory=str(get_chroma_dir()), collectionPrefix="enterprise_"),
        embeddingService=embedding_service,
    )
    return get_enterprise_file_upload_service(
        embeddingService=embedding_service,
        vectorStore=vector_store,
        knowledgeService=get_unified_knowledge_service(),
    )


def describe_upload_status(status: str | None) -> Dict[str, Any]:
    status_key = str(status or "").strip().lower()
    meta = UPLOAD_STATUS_META.get(
        status_key,
        {
            "label": status_key or "未知状态",
            "badge_class": "bg-secondary",
            "hint": "",
            "requires_ocr": False,
            "can_retry_processing": False,
        },
    )
    return {
        "status": status_key or "unknown",
        "status_label": meta["label"],
        "status_badge_class": meta["badge_class"],
        "status_hint": meta["hint"],
        "requires_ocr": bool(meta["requires_ocr"]),
        "can_retry_processing": bool(meta["can_retry_processing"]),
    }


def serialize_uploaded_file(
    uploaded_file: Any,
    *,
    upload_time_key: str,
    processed_time_key: str,
    include_tags: bool = False,
    include_file_type: bool = False,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": uploaded_file.id,
        "filename": uploaded_file.originalName,
        "size": uploaded_file.fileSize,
        "status": uploaded_file.status,
        "category": uploaded_file.category,
        "enterprise_id": uploaded_file.enterpriseId,
        "chunk_count": uploaded_file.chunkCount,
        "vector_count": uploaded_file.vectorCount,
        "task_id": uploaded_file.taskId,
        "error_message": uploaded_file.errorMessage,
        upload_time_key: uploaded_file.createdAt.isoformat() if uploaded_file.createdAt else None,
        processed_time_key: uploaded_file.processedAt.isoformat() if uploaded_file.processedAt else None,
        **describe_upload_status(getattr(uploaded_file, "status", "")),
    }
    if include_tags:
        payload["tags"] = list(getattr(uploaded_file, "tags", []) or [])
    if include_file_type:
        payload["file_type"] = getattr(uploaded_file, "fileType", "")
    return payload


def build_upload_response(
    uploaded_file: Any,
    enterprise_id: str,
    category: str,
    tags: List[str],
    chunk_strategy: Optional[str],
    chunk_size: Optional[int],
) -> Dict[str, Any]:
    return {
        "success": True,
        "message": "上传成功",
        "file_id": uploaded_file.id,
        "filename": uploaded_file.originalName,
        "size": uploaded_file.fileSize,
        "enterprise_id": enterprise_id or "default",
        "category": category,
        "tags": tags,
        "status": uploaded_file.status,
        "chunk_count": uploaded_file.chunkCount,
        "vector_count": uploaded_file.vectorCount,
        "task_id": uploaded_file.taskId,
        "document_id": getattr(uploaded_file, "documentId", ""),
        "chunk_strategy": chunk_strategy,
        "chunk_size": chunk_size,
        **describe_upload_status(getattr(uploaded_file, "status", "")),
    }
