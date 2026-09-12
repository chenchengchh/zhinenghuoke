from __future__ import annotations

"""学习域默认依赖装配入口。

集中管理 facade、workflow、repository 及相关 service 的默认单例，
避免 common/web 层重复维护实例缓存。
"""

from loguru import logger

_learning_system_instance = None
_learning_engine_instance = None
_learning_facade_instance = None
_learning_workflow_service_instance = None
_learning_repository_instance = None
_learning_conflict_scan_service_instance = None
_learning_memory_tier_service_instance = None


def get_learning_system():
    global _learning_system_instance
    if _learning_system_instance is None:
        from src.common.self_learning_rag import get_self_learning_rag_system

        _learning_system_instance = get_self_learning_rag_system()
    return _learning_system_instance


def get_learning_engine():
    global _learning_engine_instance
    if _learning_engine_instance is None:
        from src.common.intelligent_learning_engine import get_learning_engine as _get_learning_engine

        _learning_engine_instance = _get_learning_engine()
    return _learning_engine_instance


def get_learning_repository(knowledge_base=None):
    global _learning_repository_instance
    if knowledge_base is not None:
        from src.common.learning_repository import LearningRepository

        return LearningRepository(knowledge_base=knowledge_base)

    if _learning_repository_instance is None:
        from src.common.learning_repository import LearningRepository

        _learning_repository_instance = LearningRepository()
    return _learning_repository_instance


def get_learning_conflict_scan_service(knowledge_base=None, learning_repository=None, conflict_detector_factory=None):
    global _learning_conflict_scan_service_instance
    if knowledge_base is not None or learning_repository is not None or conflict_detector_factory is not None:
        from src.common.learning_conflict_scan_service import LearningConflictScanService

        repository = learning_repository or get_learning_repository(knowledge_base=knowledge_base)
        return LearningConflictScanService(
            repository=repository,
            knowledge_base=knowledge_base or getattr(repository, "knowledge_base", None),
            conflict_detector_factory=conflict_detector_factory,
        )

    if _learning_conflict_scan_service_instance is None:
        from src.common.learning_conflict_scan_service import LearningConflictScanService

        repository = get_learning_repository()
        knowledge_base = getattr(repository, "knowledge_base", None)
        _learning_conflict_scan_service_instance = LearningConflictScanService(
            repository=repository,
            knowledge_base=knowledge_base,
        )
    return _learning_conflict_scan_service_instance


def get_learning_memory_tier_service(memory_tier_manager=None):
    """已废弃：learning_memory_tier_service 已删除，直接返回 MemoryTierManager 实例。"""
    global _learning_memory_tier_service_instance
    from src.common.memory_tier_manager import MemoryTierManager

    if memory_tier_manager is not None:
        return memory_tier_manager

    if _learning_memory_tier_service_instance is None:
        _learning_memory_tier_service_instance = MemoryTierManager()
    return _learning_memory_tier_service_instance


def get_learning_workflow_service():
    global _learning_workflow_service_instance
    if _learning_workflow_service_instance is None:
        from src.common.learning_workflow_service import LearningWorkflowService

        repository = get_learning_repository()
        knowledge_base = getattr(repository, "knowledge_base", None)
        _learning_workflow_service_instance = LearningWorkflowService(
            learning_system=get_learning_system(),
            learning_engine=get_learning_engine(),
            knowledge_base=knowledge_base,
            learning_repository=repository,
            conflict_scan_service=get_learning_conflict_scan_service(),
            memory_tier_service=get_learning_memory_tier_service(),
        )
    return _learning_workflow_service_instance


def get_learning_facade():
    global _learning_facade_instance
    if _learning_facade_instance is None:
        from src.common.learning_facade import LearningFacade

        try:
            _learning_facade_instance = LearningFacade(workflow_service=get_learning_workflow_service())
        except Exception as exc:
            logger.error(f"初始化 LearningFacade 失败: {exc}")
            raise
    return _learning_facade_instance


def reset_learning_dependency_singletons() -> None:
    global _learning_system_instance
    global _learning_engine_instance
    global _learning_facade_instance
    global _learning_workflow_service_instance
    global _learning_repository_instance
    global _learning_conflict_scan_service_instance
    global _learning_memory_tier_service_instance

    _learning_system_instance = None
    _learning_engine_instance = None
    _learning_facade_instance = None
    _learning_workflow_service_instance = None
    _learning_repository_instance = None
    _learning_conflict_scan_service_instance = None
    _learning_memory_tier_service_instance = None


__all__ = [
    "get_learning_system",
    "get_learning_engine",
    "get_learning_repository",
    "get_learning_conflict_scan_service",
    "get_learning_memory_tier_service",
    "get_learning_workflow_service",
    "get_learning_facade",
    "reset_learning_dependency_singletons",
]
