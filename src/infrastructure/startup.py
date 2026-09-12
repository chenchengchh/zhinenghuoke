# -*- coding: utf-8 -*-
"""
应用启动初始化模块
提供企业级应用的启动初始化功能
"""
import asyncio
import os
from typing import Optional

from src.infrastructure.config import get_config
from src.infrastructure.logger import setup_logging, get_logger
from src.infrastructure.cache import get_cache_service
from src.infrastructure.async_tasks import get_task_queue, get_background_manager
from src.infrastructure.audit import get_audit_service

logger = get_logger("startup")
_config = get_config()


async def initialize_infrastructure():
    """
    初始化基础设施
    
    按顺序初始化所有基础设施模块
    """
    logger.info("=" * 60)
    logger.info("开始初始化基础设施")
    logger.info("=" * 60)
    
    setup_logging(
        level=_config.logging.level,
        log_dir=_config.logging.log_dir,
        json_format=_config.logging.json_format
    )
    logger.info(f"日志系统已初始化: {_config.logging.level}")
    
    cache_service = get_cache_service()
    logger.info(f"缓存服务已初始化: {_config.cache.type}")
    
    task_queue = get_task_queue()
    await task_queue.start()
    logger.info(f"任务队列已启动: 最大工作数 {_config.workers}")
    
    background_manager = get_background_manager()
    await background_manager.start()
    logger.info("后台任务管理器已启动")
    
    audit_service = get_audit_service()
    logger.info("审计服务已初始化")
    
    await _initialize_services()
    
    logger.info("=" * 60)
    logger.info("基础设施初始化完成")
    logger.info("=" * 60)


async def _initialize_services():
    """初始化业务服务"""
    embedding_service = None
    try:
        from src.rag.embedding_service import build_embedding_config_from_app_config, get_embedding_service
        embedding_config = build_embedding_config_from_app_config(_config)
        embedding_service = get_embedding_service(embedding_config)
        logger.info(
            "Embedding服务已初始化: "
            f"{embedding_service.get_model_name()} "
            f"(dimension={embedding_service.get_output_dimension()})"
        )
    except Exception as e:
        logger.warning(f"Embedding服务初始化失败: {e}")
    
    try:
        from src.rag.vector_store import ChromaVectorStore, ChromaConfig
        vector_store = ChromaVectorStore(config=ChromaConfig(), embeddingService=embedding_service)
        health = vector_store.healthCheck()
        if health.get("status") == "healthy":
            logger.info(f"向量存储已初始化: {health}")
        else:
            logger.warning(f"向量存储健康检查未通过: {health}")
    except Exception as e:
        logger.warning(f"向量存储初始化失败: {e}")
    
    try:
        from src.common.knowledge_graph import get_knowledge_graph_service
        graph_service = get_knowledge_graph_service()
        logger.info("知识图谱服务已初始化")
    except Exception as e:
        logger.warning(f"知识图谱服务初始化失败: {e}")


async def shutdown_infrastructure():
    """
    关闭基础设施
    
    按顺序关闭所有基础设施模块，确保BotService状态被正确保存
    """
    logger.info("=" * 60)
    logger.info("开始关闭基础设施")
    logger.info("=" * 60)

    try:
        from src.web.bot_service import BotService
        bot_service = BotService()
        if bot_service.is_running:
            logger.info("检测到BotService正在运行，保存状态...")
            try:
                bot_service._save_state_before_shutdown()
                logger.info("BotService状态已保存")
            except Exception as bs_e:
                logger.warning(f"保存BotService状态失败: {bs_e}")
    except Exception as e:
        logger.debug(f"BotService状态保存检查跳过: {e}")

    try:
        from src.douyin_bot.app_state_persistor import get_app_state_persistor
        persistor = get_app_state_persistor()
        persistor.stop_auto_save()
        logger.info("状态自动保存已停止")
    except Exception as e:
        logger.debug(f"停止状态自动保存跳过: {e}")
    
    task_queue = get_task_queue()
    await task_queue.stop()
    logger.info("任务队列已停止")
    
    background_manager = get_background_manager()
    await background_manager.stop()
    logger.info("后台任务管理器已停止")
    
    audit_service = get_audit_service()
    audit_service.flush()
    logger.info("审计服务已刷新")
    
    logger.info("=" * 60)
    logger.info("基础设施关闭完成")
    logger.info("=" * 60)


def setup_app(app):
    """
    配置FastAPI应用
    
    Args:
        app: FastAPI应用实例
    """
    from fastapi.middleware.cors import CORSMiddleware
    from src.infrastructure.middleware import (
        RequestMiddleware,
        ErrorHandlingMiddleware,
        LoggingMiddleware,
        SecurityHeadersMiddleware
    )
    from src.infrastructure.api_docs import setup_swagger_ui
    
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(ErrorHandlingMiddleware, debug=_config.debug)
    app.add_middleware(RequestMiddleware)
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"]
    )
    
    setup_swagger_ui(app)
    
    from src.infrastructure.health import router as health_router
    app.include_router(health_router)
    
    @app.on_event("startup")
    async def startup_event():
        await initialize_infrastructure()
    
    @app.on_event("shutdown")
    async def shutdown_event():
        await shutdown_infrastructure()
    
    logger.info(f"应用配置完成: {_config.app_name} v{_config.app_version}")
    
    return app
