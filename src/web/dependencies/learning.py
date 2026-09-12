from __future__ import annotations

from loguru import logger

def get_learning_system():
    try:
        from src.common.learning_dependency_factory import get_learning_system as _get_learning_system

        return _get_learning_system()
    except Exception as exc:
        logger.error(f"获取学习系统失败: {exc}")
        return None


def get_learning_facade():
    try:
        from src.common.learning_dependency_factory import get_learning_facade as _get_learning_facade

        return _get_learning_facade()
    except Exception as exc:
        logger.error(f"获取学习门面失败: {exc}")
        return None


def get_learning_workflow_service():
    try:
        from src.common.learning_dependency_factory import (
            get_learning_workflow_service as _get_learning_workflow_service,
        )

        return _get_learning_workflow_service()
    except Exception as exc:
        logger.error(f"获取学习工作流服务失败: {exc}")
        return None
