# -*- coding: utf-8 -*-
"""
健康检查模块
提供系统健康状态检查功能
"""
import time
from typing import Any, Dict, Optional
from datetime import datetime
from fastapi import APIRouter, Response

from src.infrastructure.config import get_config
from src.infrastructure.logger import get_logger
from src.infrastructure.runtime_paths import get_knowledge_base_path

router = APIRouter(tags=["系统管理"])
logger = get_logger("health")

_start_time = time.time()


def get_uptime() -> float:
    """获取系统运行时间(秒)"""
    return time.time() - _start_time


def format_uptime(seconds: float) -> str:
    """格式化运行时间"""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0:
        parts.append(f"{hours}小时")
    if minutes > 0:
        parts.append(f"{minutes}分钟")
    parts.append(f"{secs}秒")
    
    return " ".join(parts)


@router.get("/health", summary="健康检查")
async def health_check(response: Response):
    """
    健康检查端点
    
    用于负载均衡和容器健康检查
    """
    response.status_code = 200
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "uptime": format_uptime(get_uptime())
    }


@router.get("/health/ready", summary="就绪检查")
async def readiness_check(response: Response):
    """
    就绪检查端点
    
    检查系统是否准备好接收请求
    """
    checks = {}
    all_healthy = True
    embedding_service = None
    
    try:
        from src.rag.embedding_service import build_embedding_config_from_app_config, get_embedding_service
        embedding_service = get_embedding_service(build_embedding_config_from_app_config(get_config()))
        runtime_contract = embedding_service.get_runtime_contract()
        checks["embedding"] = {
            "status": "healthy",
            "model": runtime_contract["actual_model"],
            "dimension": runtime_contract["actual_dimension"],
            "provider": runtime_contract["provider"],
        }
    except Exception as e:
        checks["embedding"] = {"status": "unhealthy", "error": str(e)}
        all_healthy = False
    
    try:
        from src.rag.vector_store import ChromaVectorStore, ChromaConfig
        vector_store = ChromaVectorStore(config=ChromaConfig(), embeddingService=embedding_service)
        health = vector_store.healthCheck()
        stats = vector_store.getCollectionStats("default")
        checks["vector_store"] = {
            "status": health.get("status", "unknown"),
            "count": stats.get("count", 0),
            "embedding_contract": health.get("embedding_contract", {}),
        }
        if health.get("status") != "healthy":
            all_healthy = False
    except Exception as e:
        checks["vector_store"] = {"status": "unhealthy", "error": str(e)}
        all_healthy = False
    
    try:
        knowledge_file = get_knowledge_base_path()
        if knowledge_file.exists():
            import json
            with open(knowledge_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            checks["knowledge_base"] = {
                "status": "healthy",
                "count": len(data) if isinstance(data, list) else 0
            }
        else:
            checks["knowledge_base"] = {"status": "degraded", "error": "文件不存在"}
    except Exception as e:
        checks["knowledge_base"] = {"status": "unhealthy", "error": str(e)}
        all_healthy = False
    
    response.status_code = 200 if all_healthy else 503
    
    return {
        "status": "ready" if all_healthy else "not_ready",
        "timestamp": datetime.now().isoformat(),
        "checks": checks
    }


@router.get("/health/live", summary="存活检查")
async def liveness_check(response: Response):
    """
    存活检查端点
    
    检查服务是否存活
    """
    response.status_code = 200
    return {
        "status": "alive",
        "timestamp": datetime.now().isoformat()
    }


@router.get("/metrics", summary="系统指标")
async def system_metrics():
    """
    系统指标端点
    
    返回系统运行指标
    """
    config = get_config()
    
    try:
        import psutil
    except ImportError:
        return {
            "timestamp": datetime.now().isoformat(),
            "uptime_seconds": round(get_uptime(), 2),
            "uptime_formatted": format_uptime(get_uptime()),
            "system": {"error": "psutil未安装，无法获取系统指标"},
            "application": {
                "name": config.app_name,
                "version": config.app_version,
                "environment": config.environment
            }
        }
    
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    
    return {
        "timestamp": datetime.now().isoformat(),
        "uptime_seconds": round(get_uptime(), 2),
        "uptime_formatted": format_uptime(get_uptime()),
        "system": {
            "cpu_percent": cpu_percent,
            "memory": {
                "total": memory.total,
                "available": memory.available,
                "used": memory.used,
                "percent": memory.percent
            },
            "disk": {
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
                "percent": disk.percent
            }
        },
        "application": {
            "name": config.app_name,
            "version": config.app_version,
            "environment": config.environment
        }
    }
