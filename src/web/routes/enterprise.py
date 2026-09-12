from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from src.web.dependencies.enterprise import bind_request_tenant_context
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

router = APIRouter(tags=["企业管理"])


class EnterpriseAddRequest(BaseModel):
    name: str
    code: str
    contact: str = ""
    phone: str = ""
    config: str = ""
    enabled: bool = True


class EnterpriseUpdateRequest(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    config: Optional[str] = None
    enabled: Optional[bool] = None


class EnterpriseUploadRequest(BaseModel):
    enterprise_id: str = "default"
    category: str = "general"
    tags: List[str] = []
    chunk_strategy: str = "recursive"
    chunk_size: int = 500
    auto_process: bool = True


@router.get("/api/enterprises")
async def get_enterprises():
    try:
        from src.rag.enterprise_service import EnterpriseService

        return EnterpriseService().listEnterprises()
    except ImportError:
        return []
    except Exception as exc:
        logger.error(f"获取企业列表失败: {exc}")
        return []


@router.get("/api/enterprises/{enterprise_id}")
async def get_enterprise(enterprise_id: str):
    try:
        from src.rag.enterprise_service import EnterpriseService

        enterprise = EnterpriseService().getEnterprise(enterprise_id)
        if enterprise:
            return enterprise.toDict()
        return JSONResponse(status_code=404, content={"message": "企业不存在"})
    except ImportError:
        return JSONResponse(status_code=500, content={"message": "企业服务未启用"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.post("/api/enterprises")
async def add_enterprise(request: EnterpriseAddRequest):
    try:
        from src.rag.enterprise_service import EnterpriseService

        enterprise = EnterpriseService().createEnterprise(
            name=request.name,
            contactEmail=request.contact or "",
            code=request.code or request.name.lower().replace(" ", "_"),
        )
        if enterprise:
            return {"success": True, "message": "添加成功", "id": enterprise.id}
        return JSONResponse(status_code=500, content={"success": False, "message": "添加失败"})
    except ImportError:
        return JSONResponse(status_code=500, content={"message": "企业服务未启用"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.put("/api/enterprises/{enterprise_id}")
async def update_enterprise(enterprise_id: str, request: EnterpriseUpdateRequest):
    try:
        from src.rag.enterprise_service import EnterpriseService

        updates = {}
        if request.name is not None:
            updates["name"] = request.name
        if request.code is not None:
            updates["code"] = request.code
        if request.contact is not None:
            updates["contact"] = request.contact
        if request.phone is not None:
            updates["phone"] = request.phone
        if request.config is not None:
            updates["config"] = request.config
        if request.enabled is not None:
            updates["enabled"] = request.enabled

        success = EnterpriseService().updateEnterprise(enterprise_id, **updates)
        if success:
            return {"success": True, "message": "更新成功"}
        return JSONResponse(status_code=404, content={"success": False, "message": "企业不存在"})
    except ImportError:
        return JSONResponse(status_code=500, content={"message": "企业服务未启用"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.delete("/api/enterprises/{enterprise_id}")
async def delete_enterprise(enterprise_id: str):
    try:
        from src.rag.enterprise_service import EnterpriseService

        success = EnterpriseService().deleteEnterprise(enterprise_id)
        if success:
            return {"success": True, "message": "删除成功"}
        return JSONResponse(status_code=404, content={"success": False, "message": "企业不存在"})
    except ImportError:
        return JSONResponse(status_code=500, content={"message": "企业服务未启用"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.post("/api/enterprise/upload")
async def enterprise_upload_file(
    request: Request,
    file: UploadFile = File(...),
    enterprise_id: str = Form("default"),
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
        logger.error(f"导入企业级RAG模块失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": "企业级RAG服务未启用"})
    except ValueError as exc:
        logger.error(f"文件验证失败: {exc}")
        return JSONResponse(status_code=400, content={"success": False, "message": str(exc)})
    except Exception as exc:
        logger.error(f"企业级文件上传失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/enterprise/progress/{task_id}")
async def get_processing_progress(task_id: str):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        progress = get_enterprise_file_upload_service().getProcessingProgress(task_id)
        if progress:
            return {"success": True, "progress": progress.toDict()}
        return JSONResponse(status_code=404, content={"success": False, "message": "任务不存在"})
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "企业级RAG服务未启用"})
    except Exception as exc:
        logger.error(f"获取进度失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/enterprise/files")
async def get_enterprise_files_list(
    enterprise_id: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        files = get_enterprise_file_upload_service().listFiles(
            enterpriseId=enterprise_id or "",
            category=category or "",
            status=status or "all",
        )
        return {
            "success": True,
            "total": len(files),
            "files": [
                serialize_uploaded_file(
                    file,
                    upload_time_key="created_at",
                    processed_time_key="processed_at",
                    include_tags=True,
                )
                for file in files
            ],
        }
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "企业级RAG服务未启用"})
    except Exception as exc:
        logger.error(f"获取文件列表失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/enterprise/files/{file_id}")
async def get_enterprise_file_info(file_id: str, enterprise_id: Optional[str] = None):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        file_info = get_enterprise_file_upload_service().getFile(
            enterpriseId=enterprise_id or "default",
            fileId=file_id,
        )
        if file_info:
            return {
                "success": True,
                "file": serialize_uploaded_file(
                    file_info,
                    upload_time_key="upload_time",
                    processed_time_key="processed_time",
                    include_tags=True,
                    include_file_type=True,
                ),
            }
        return JSONResponse(status_code=404, content={"success": False, "message": "文件不存在"})
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "企业级RAG服务未启用"})
    except Exception as exc:
        logger.error(f"获取文件信息失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.post("/api/enterprise/files/process-all")
async def process_all_pending_files(enterprise_id: str = "default"):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service
        from src.common.unified_knowledge_service import get_unified_knowledge_service

        upload_service = get_enterprise_file_upload_service(knowledgeService=get_unified_knowledge_service())
        files = upload_service.listFiles(enterpriseId=enterprise_id)
        pending_statuses = {"uploaded", "pending_ocr"}
        pending_files = [file for file in files if file.status in pending_statuses]
        if not pending_files:
            return {
                "success": True,
                "message": "没有待处理或待OCR的文件",
                "processed_count": 0,
                "eligible_statuses": sorted(pending_statuses),
            }

        processed_count = 0
        errors = []
        for file in pending_files:
            try:
                await upload_service.processFile(enterpriseId=file.enterpriseId, fileId=file.id)
                processed_count += 1
            except Exception as exc:
                errors.append({"filename": file.originalName, "error": str(exc)})

        return {
            "success": True,
            "message": f"已处理 {processed_count} 个文件",
            "processed_count": processed_count,
            "error_count": len(errors),
            "errors": errors[:5],
            "eligible_statuses": sorted(pending_statuses),
        }
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "企业级RAG服务未启用"})
    except Exception as exc:
        logger.error(f"批量处理文件失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.post("/api/enterprise/files/{file_id}/process")
async def process_single_file(file_id: str, enterprise_id: Optional[str] = None):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        ent_id = enterprise_id or "default"
        result = await get_enterprise_file_upload_service().processFile(enterpriseId=ent_id, fileId=file_id)
        return {"success": True, "message": "文件处理完成", "file": result.toDict() if hasattr(result, "toDict") else {"id": file_id}}
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "企业级RAG服务未启用"})
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"success": False, "message": str(exc)})
    except Exception as exc:
        logger.error(f"处理文件失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.delete("/api/enterprise/files/{file_id}")
async def delete_enterprise_file(file_id: str, enterprise_id: Optional[str] = None):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        success = get_enterprise_file_upload_service().deleteFile(
            enterpriseId=enterprise_id or "default",
            fileId=file_id,
        )
        if success:
            return {"success": True, "message": "删除成功"}
        return JSONResponse(status_code=404, content={"success": False, "message": "文件不存在"})
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "企业级RAG服务未启用"})
    except Exception as exc:
        logger.error(f"删除文件失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/enterprise/stats")
async def get_enterprise_storage_stats(enterprise_id: Optional[str] = None):
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        stats = get_enterprise_file_upload_service().getStorageStats(enterprise_id or "default")
        return {"success": True, "stats": stats}
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "企业级RAG服务未启用"})
    except Exception as exc:
        logger.error(f"获取统计失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/enterprise/supported-formats")
async def get_enterprise_supported_formats():
    try:
        from src.rag.enterprise_file_upload import get_enterprise_file_upload_service

        upload_service = get_enterprise_file_upload_service()
        return {
            "success": True,
            "formats": upload_service.getSupportedExtensions(),
            "max_size_mb": upload_service.config.maxFileSize / 1024 / 1024,
        }
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "企业级RAG服务未启用"})
    except Exception as exc:
        logger.error(f"获取支持格式失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})
