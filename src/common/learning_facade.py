from __future__ import annotations

import warnings
from typing import Any, Dict, List, Optional

from src.common.learning_workflow_service import LearningWorkflowService


class LearningFacade:
    """统一暴露学习系统可公开使用的业务能力与状态。

    .. deprecated::
        请使用 :class:`src.common.unified_learning_service.UnifiedLearningService` 替代。
    """

    def __init__(self, learning_system=None, learning_engine=None, knowledge_base=None, workflow_service=None):
        warnings.warn(
            "LearningFacade 已弃用，请使用 UnifiedLearningService",
            DeprecationWarning,
            stacklevel=2,
        )
        if workflow_service is None and learning_system is None and learning_engine is None and knowledge_base is None:
            from src.common.learning_dependency_factory import (
                get_learning_workflow_service as _get_learning_workflow_service,
            )

            workflow_service = _get_learning_workflow_service()
        elif workflow_service is None:
            workflow_service = LearningWorkflowService(
                learning_system=learning_system,
                learning_engine=learning_engine,
                knowledge_base=knowledge_base,
            )

        self.workflow_service = workflow_service
        self.learning_system = learning_system or getattr(workflow_service, "learning_system", None)
        self.learning_engine = learning_engine or getattr(workflow_service, "learning_engine", None)
        self.knowledge_base = knowledge_base or getattr(workflow_service, "knowledge_base", None)

    # Core status and pending review
    def get_stats(self) -> Dict[str, Any]:
        return self.workflow_service.get_stats_snapshot().to_dict()

    def set_learning_enabled(self, enabled: bool) -> bool:
        return self.workflow_service.set_learning_enabled(enabled)

    def get_pending_validation(self, top_k: int = 20) -> List[Any]:
        return self.workflow_service.list_pending_validation(top_k)

    def approve_pending(self, item_id: str, approved_answer: str = "", category: str = "") -> bool:
        return self.workflow_service.approve_pending(item_id, approved_answer=approved_answer, category=category)

    def reject_pending(self, item_id: str, reason: str) -> bool:
        return self.workflow_service.reject_pending(item_id, reason)

    def clear_pending_validation(self) -> int:
        return self.workflow_service.clear_pending_validation()

    def get_learning_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [item.to_dict() for item in self.workflow_service.get_history(limit)]

    # Learning operations
    def get_enhanced_stats(self) -> Dict[str, Any]:
        return self.workflow_service.get_enhanced_stats()

    def get_quality_report(self) -> Dict[str, Any]:
        return self.workflow_service.get_quality_report()

    def get_learning_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self.workflow_service.get_learning_tasks(limit)

    def generate_learning_question(self) -> Optional[str]:
        return self.workflow_service.generate_learning_question()

    def generate_user_question(self) -> Optional[str]:
        return self.generate_learning_question()

    def complete_learning_task(self, task_id: str, result: Optional[Dict[str, Any]] = None) -> bool:
        return self.workflow_service.complete_learning_task(task_id, result)

    def apply_knowledge_decay(self) -> Dict[str, Any]:
        return self.workflow_service.apply_knowledge_decay()

    def get_knowledge_gaps(self, top_n: int = 20) -> List[Dict[str, Any]]:
        return self.workflow_service.get_knowledge_gaps(top_n)

    def auto_fill_gaps(self, top_n: int = 5) -> int:
        return self.workflow_service.auto_fill_gaps(top_n)

    def convert_patterns_to_knowledge(self, min_count: int = 5) -> int:
        return self.workflow_service.convert_patterns_to_knowledge(min_count)

    def apply_feedback_to_knowledge_base(self) -> None:
        self.workflow_service.apply_feedback_to_knowledge_base()

    def get_auto_filled_gap_records(self) -> Dict[str, Dict[str, Any]]:
        return self.workflow_service.get_auto_filled_gap_records()

    def get_learning_engine_stats(self) -> Dict[str, Any]:
        return self.workflow_service.get_learning_engine_stats()

    def submit_learning_feedback(
        self,
        query: str,
        feedback_type: str,
        rating: int = 0,
        comment: str = "",
        knowledge_id: str = "",
        session_id: str = "default",
    ) -> bool:
        return self.workflow_service.submit_learning_feedback(
            query=query,
            feedback_type=feedback_type,
            rating=rating,
            comment=comment,
            knowledge_id=knowledge_id,
            session_id=session_id,
        )

    def get_learning_query_suggestions(self, q: str = "", top_n: int = 5) -> List[str]:
        return self.workflow_service.get_learning_query_suggestions(q, top_n)

    def get_query_suggestions(self, q: str = "", top_n: int = 5) -> List[str]:
        return self.get_learning_query_suggestions(q, top_n)

    def save_learning_data(self) -> bool:
        return self.workflow_service.save_learning_data()

    def generate_learning_test_data(self) -> int:
        return self.workflow_service.generate_learning_test_data()

    def generate_test_learning_data(self) -> int:
        return self.generate_learning_test_data()

    # Diagnostics and generated knowledge review
    def detect_knowledge_conflicts(self, limit: int = 50, offset: int = 0, force_refresh: bool = False) -> Dict[str, Any]:
        return self.workflow_service.detect_knowledge_conflicts(
            limit=limit,
            offset=offset,
            force_refresh=force_refresh,
        )

    def get_memory_tier_stats(self) -> Dict[str, Any]:
        return self.workflow_service.get_memory_tier_stats()

    def get_pending_review_knowledge(self) -> List[Dict[str, Any]]:
        return [item.to_dict() for item in self.workflow_service.list_pending_review_knowledge()]

    def review_generated_knowledge(
        self,
        item_id: str,
        approved: bool,
        answer: Optional[str] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        return self.workflow_service.review_generated_knowledge(
            item_id=item_id,
            approved=approved,
            answer=answer,
            reason=reason,
        )


def get_learning_facade() -> LearningFacade:
    from src.common.learning_dependency_factory import get_learning_facade as _get_learning_facade

    return _get_learning_facade()


__all__ = ["LearningFacade", "get_learning_facade"]
