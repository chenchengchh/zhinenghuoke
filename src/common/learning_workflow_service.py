from __future__ import annotations

import warnings
from datetime import datetime
import uuid
from typing import Any, Dict, List, Optional

from src.common.learning_conflict_scan_service import LearningConflictScanService
from src.common.memory_tier_manager import MemoryTierManager
from src.common.learning_repository import LearningRepository
from src.common.learning_types import LearningHistoryItem, LearningStatsSnapshot, LearningWorkItem


class LearningWorkflowService:
    """统一学习运营主链的编排入口。

    .. deprecated::
        请使用 :class:`src.common.unified_learning_service.UnifiedLearningService` 替代。
    """

    def __init__(
        self,
        learning_system=None,
        learning_engine=None,
        knowledge_base=None,
        learning_repository=None,
        conflict_scan_service=None,
        memory_tier_service=None,
        memory_tier_manager=None,
        conflict_detector_factory=None,
    ):
        warnings.warn(
            "LearningWorkflowService 已弃用，请使用 UnifiedLearningService",
            DeprecationWarning,
            stacklevel=2,
        )
        use_factory_defaults = all(
            value is None
            for value in (
                learning_system,
                learning_engine,
                knowledge_base,
                learning_repository,
                conflict_scan_service,
                memory_tier_service,
                memory_tier_manager,
                conflict_detector_factory,
            )
        )

        if use_factory_defaults:
            from src.common.learning_dependency_factory import (
                get_learning_conflict_scan_service,
                get_learning_engine,
                get_learning_repository,
                get_learning_system,
            )

            learning_system = get_learning_system()
            learning_engine = get_learning_engine()
            learning_repository = get_learning_repository()
            knowledge_base = getattr(learning_repository, "knowledge_base", None)
            conflict_scan_service = get_learning_conflict_scan_service()
            memory_tier_service = None
        elif knowledge_base is None and learning_repository is not None:
            knowledge_base = getattr(learning_repository, "knowledge_base", None)

        self.learning_system = learning_system
        self.learning_engine = learning_engine
        self.knowledge_base = knowledge_base
        self.learning_repository = learning_repository or LearningRepository(knowledge_base=self.knowledge_base)
        self.conflict_scan_service = conflict_scan_service or LearningConflictScanService(
            repository=self.learning_repository,
            knowledge_base=self.knowledge_base,
            conflict_detector_factory=conflict_detector_factory,
        )
        self.memory_tier_service = memory_tier_service or MemoryTierManager()

    def set_learning_enabled(self, enabled: bool) -> bool:
        if not self.learning_system:
            return False
        if enabled and hasattr(self.learning_system, "enable_learning"):
            self.learning_system.enable_learning()
            return True
        if not enabled and hasattr(self.learning_system, "disable_learning"):
            self.learning_system.disable_learning()
            return True
        return False

    def get_stats_snapshot(self) -> LearningStatsSnapshot:
        engine_stats = self.learning_engine.get_learning_stats() if self.learning_engine else {}
        recent_learning = []
        if self.learning_system and hasattr(self.learning_system, "get_recent_learning"):
            recent_learning = self.learning_system.get_recent_learning(5)

        return LearningStatsSnapshot(
            learning_enabled=getattr(self.learning_system, "learning_enabled", True),
            total_learned=getattr(self.learning_system, "total_learned", 0),
            pending_validation=len(self.list_pending_validation(1000)),
            approved_knowledge=getattr(self.learning_system, "approved_count", 0),
            rejected_knowledge=getattr(self.learning_system, "rejected_count", 0),
            auto_approved=getattr(self.learning_system, "auto_approved_count", 0),
            unknown_questions_detected=getattr(self.learning_system, "unknown_detected_count", 0),
            accuracy_improvement=getattr(self.learning_system, "accuracy_improvement", 0.0),
            recent_learning=recent_learning,
            human_approved=getattr(self.learning_system, "approved_count", 0),
            rejected=getattr(self.learning_system, "rejected_count", 0),
            pending=len(self.list_pending_validation(1000)),
            patterns_learned=engine_stats.get("patterns_learned", 0),
            gaps_auto_filled=engine_stats.get("gaps_auto_filled", 0),
            knowledge_auto_created=engine_stats.get("knowledge_auto_created", 0),
            feedback_collected=engine_stats.get("feedback_collected", 0),
            pending_review=len(self.list_pending_review_knowledge()),
        )

    def list_pending_validation(self, top_k: int = 20) -> List[LearningWorkItem]:
        if not self.learning_system or not hasattr(self.learning_system, "get_pending_validation"):
            return []
        items = self.learning_system.get_pending_validation(top_k)
        return [self._build_pending_validation_item(item) for item in items]

    def generate_learning_test_data(self) -> int:
        if not self.learning_system or not hasattr(self.learning_system, "pending_validation"):
            return 0

        from src.common.self_learning_rag import ExtractedKnowledge

        test_data = [
            {
                "question": "抖音智能获客系统怎么收费？",
                "answer": "我们的抖音智能获客系统提供基础版、专业版和企业版三种套餐。基础版月费299元，适合个人用户；专业版月费599元，适合中小企业；企业版月费1299元，提供完整功能和专属客服支持。",
                "category": "price",
                "keywords": ["收费", "价格", "套餐"],
                "confidence": 0.85,
            },
            {
                "question": "系统支持哪些平台？",
                "answer": "目前系统主要支持抖音平台，包括私信自动回复、评论区获客、直播间互动等功能。后续会逐步支持快手、小红书等平台。",
                "category": "product",
                "keywords": ["平台", "支持", "功能"],
                "confidence": 0.78,
            },
            {
                "question": "如何开通代理合作？",
                "answer": "代理合作需要满足以下条件：1. 有一定的客户资源；2. 认同我们的产品理念；3. 缴纳代理保证金。具体政策请联系商务经理详谈。",
                "category": "cooperation",
                "keywords": ["代理", "合作", "开通"],
                "confidence": 0.82,
            },
            {
                "question": "系统会不会封号？",
                "answer": "我们的系统采用智能风控机制，模拟人工操作节奏，严格控制操作频率。只要按照系统建议的使用方式操作，封号风险极低。同时我们也提供账号安全指导。",
                "category": "service",
                "keywords": ["封号", "安全", "风险"],
                "confidence": 0.75,
            },
            {
                "question": "可以免费试用吗？",
                "answer": "可以的！我们提供7天免费试用期，期间可以体验所有核心功能。试用结束后如需继续使用，可以选择合适的套餐进行付费。",
                "category": "promotion",
                "keywords": ["试用", "免费", "体验"],
                "confidence": 0.88,
            },
        ]

        added_count = 0
        for item in test_data:
            knowledge = ExtractedKnowledge(
                id=str(uuid.uuid4()),
                question=item["question"],
                answer=item["answer"],
                source="test_generation",
                confidence=item["confidence"],
                keywords=item["keywords"],
                category=item["category"],
                tags=item["keywords"],
                related_items=[],
                validation_status="pending",
                created_at=datetime.now(),
            )
            self.learning_system.pending_validation.append(knowledge)
            added_count += 1

        if hasattr(self.learning_system, "_save_state"):
            self.learning_system._save_state(force=True)
        return added_count

    # Compatibility aliases
    def generate_test_data(self) -> int:
        return self.generate_learning_test_data()

    def approve_pending(self, item_id: str, approved_answer: str = "", category: str = "") -> bool:
        if not self.learning_system or not hasattr(self.learning_system, "approve_knowledge"):
            return False
        return bool(
            self.learning_system.approve_knowledge(
                item_id,
                approved_answer=approved_answer,
                category=category,
            )
        )

    def reject_pending(self, item_id: str, reason: str = "") -> bool:
        if not self.learning_system or not hasattr(self.learning_system, "reject_knowledge"):
            return False
        return bool(self.learning_system.reject_knowledge(item_id, reason))

    def clear_pending_validation(self) -> int:
        if not self.learning_system:
            return 0
        if hasattr(self.learning_system, "clear_pending_validation"):
            return int(self.learning_system.clear_pending_validation())
        return 0

    def get_history(self, limit: int = 50) -> List[LearningHistoryItem]:
        if not self.learning_system or not hasattr(self.learning_system, "get_learning_history"):
            return []
        history = self.learning_system.get_learning_history(limit)
        return [self._build_history_item(item) for item in history]

    def get_enhanced_stats(self) -> Dict[str, Any]:
        if not self.learning_system or not hasattr(self.learning_system, "get_enhanced_stats"):
            return {"error": "学习系统不可用"}
        return self.learning_system.get_enhanced_stats()

    def get_quality_report(self) -> Dict[str, Any]:
        if not self.learning_system or not hasattr(self.learning_system, "get_quality_report"):
            return {"error": "学习系统不可用"}
        return self.learning_system.get_quality_report()

    def get_learning_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        if not self.learning_system or not hasattr(self.learning_system, "get_learning_tasks"):
            return []
        return self.learning_system.get_learning_tasks(limit)

    def generate_learning_question(self) -> Optional[str]:
        if not self.learning_system or not hasattr(self.learning_system, "generate_user_question_for_learning"):
            return None
        return self.learning_system.generate_user_question_for_learning()

    def generate_user_question(self) -> Optional[str]:
        return self.generate_learning_question()

    def complete_learning_task(self, task_id: str, result: Optional[Dict[str, Any]] = None) -> bool:
        if not self.learning_system or not hasattr(self.learning_system, "complete_learning_task"):
            return False
        return bool(self.learning_system.complete_learning_task(task_id, result or {}))

    def apply_knowledge_decay(self) -> Dict[str, Any]:
        if not self.learning_system or not hasattr(self.learning_system, "apply_knowledge_decay"):
            return {"error": "学习系统不可用"}
        return self.learning_system.apply_knowledge_decay()

    def get_knowledge_gaps(self, top_n: int = 20) -> List[Dict[str, Any]]:
        if not self.learning_engine:
            return []
        return self.learning_engine.get_knowledge_gaps(top_n)

    def auto_fill_gaps(self, top_n: int = 5) -> int:
        if not self.learning_engine:
            return 0
        return self.learning_engine.auto_fill_high_priority_gaps(top_n=top_n)

    def convert_patterns_to_knowledge(self, min_count: int = 5) -> int:
        if not self.learning_engine:
            return 0
        return self.learning_engine.convert_frequent_patterns_to_knowledge(min_count=min_count)

    def apply_feedback_to_knowledge_base(self) -> None:
        if self.learning_engine:
            self.learning_engine.apply_feedback_to_knowledge_base()

    def get_auto_filled_gap_records(self) -> Dict[str, Dict[str, Any]]:
        if not self.learning_engine:
            return {}
        return self.learning_engine.get_auto_filled_gap_records()

    def get_learning_engine_stats(self) -> Dict[str, Any]:
        if not self.learning_engine:
            return {}
        return self.learning_engine.get_learning_stats()

    def get_engine_stats(self) -> Dict[str, Any]:
        return self.get_learning_engine_stats()

    def submit_learning_feedback(
        self,
        query: str,
        feedback_type: str,
        rating: int = 0,
        comment: str = "",
        knowledge_id: str = "",
        session_id: str = "default",
    ) -> bool:
        if not self.learning_engine:
            return False
        self.learning_engine.on_user_feedback_async(
            query=query,
            feedback_type=feedback_type,
            rating=rating,
            comment=comment,
            knowledge_id=knowledge_id,
            session_id=session_id,
        )
        return True

    def submit_feedback(self, query: str, feedback_type: str, rating: int = 0, comment: str = "", knowledge_id: str = "", session_id: str = "default") -> bool:
        return self.submit_learning_feedback(
            query=query,
            feedback_type=feedback_type,
            rating=rating,
            comment=comment,
            knowledge_id=knowledge_id,
            session_id=session_id,
        )

    def get_learning_query_suggestions(self, query: str = "", top_n: int = 5) -> List[str]:
        if not self.learning_engine or not hasattr(self.learning_engine, "get_query_suggestions"):
            return []
        return self.learning_engine.get_query_suggestions(query, top_n)

    def get_query_suggestions(self, query: str = "", top_n: int = 5) -> List[str]:
        return self.get_learning_query_suggestions(query, top_n)

    def save_learning_data(self) -> bool:
        if not self.learning_engine or not hasattr(self.learning_engine, "save"):
            return False
        self.learning_engine.save()
        return True

    def save_engine_data(self) -> bool:
        return self.save_learning_data()

    def detect_knowledge_conflicts(
        self,
        limit: int = 50,
        offset: int = 0,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        return self.conflict_scan_service.detect(
            limit=limit,
            offset=offset,
            force_refresh=force_refresh,
        )

    def get_memory_tier_stats(self) -> Dict[str, Any]:
        return self.memory_tier_service.get_statistics()

    def list_pending_review_knowledge(self) -> List[LearningWorkItem]:
        items: List[LearningWorkItem] = []
        if not self.learning_repository:
            return items

        for item in self.learning_repository.list_pending_review_items():
            tags = list(getattr(item, "tags", []) or [])
            items.append(
                LearningWorkItem(
                    id=item.id,
                    item_type="knowledge_review",
                    status="pending_review",
                    question=item.question,
                    answer=(item.answer or "")[:200],
                    category=item.category,
                    source=getattr(item, "source", "unknown"),
                    keywords=list(getattr(item, "keywords", []) or []),
                    tags=tags,
                    created_at=self._stringify_dt(getattr(item, "created_at", "")),
                    updated_at=self._stringify_dt(getattr(item, "updated_at", "")),
                    priority=getattr(item, "priority", 0),
                )
            )
        return items

    def review_generated_knowledge(
        self,
        item_id: str,
        approved: bool,
        answer: Optional[str] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        if not self.learning_repository:
            return {"success": False, "message": "知识库不可用"}

        item = self.learning_repository.get_pending_review_item(item_id)
        if not item:
            return {"success": False, "message": f"未找到知识条目 {item_id}"}

        if approved:
            success = self.learning_repository.approve_pending_review_item(item_id, answer=answer or "")
            if success and self.learning_engine:
                self.learning_engine.mark_auto_filled_gap_review(item_id, "approved")
            return {
                "success": success,
                "message": f"知识条目 {item_id} 已审核通过并启用" if success else f"知识条目 {item_id} 审核失败",
            }

        success = self.learning_repository.reject_pending_review_item(item_id)
        if success and self.learning_engine:
            self.learning_engine.mark_auto_filled_gap_review(item_id, "rejected", reason=reason)
        return {
            "success": success,
            "message": f"知识条目 {item_id} 已拒绝并删除" if success else f"知识条目 {item_id} 拒绝失败",
        }

    def _build_pending_validation_item(self, item: Any) -> LearningWorkItem:
        created_at = getattr(item, "created_at", None)
        return LearningWorkItem(
            id=str(getattr(item, "id", "") or ""),
            item_type="pending_validation",
            status=getattr(item, "validation_status", "pending") or "pending",
            question=getattr(item, "question", "") or "",
            answer=getattr(item, "answer", "") or "",
            category=getattr(item, "category", "other") or "other",
            source=getattr(item, "source", "unknown") or "unknown",
            confidence=float(getattr(item, "confidence", 0.0) or 0.0),
            keywords=list(getattr(item, "keywords", []) or []),
            tags=list(getattr(item, "tags", []) or []),
            created_at=self._stringify_dt(created_at),
            updated_at=self._stringify_dt(getattr(item, "validated_at", None)),
            metadata={
                "suggested_intent": getattr(item, "suggested_intent", None),
            },
        )

    def _build_history_item(self, item: Dict[str, Any]) -> LearningHistoryItem:
        action = str(item.get("action", "pending") or "pending")
        status_map = {
            "approved": "approved",
            "rejected": "rejected",
            "auto_approved": "approved",
        }
        return LearningHistoryItem(
            action=action,
            status=status_map.get(action, "pending"),
            question=str(item.get("question", "") or ""),
            answer=str(item.get("answer", "") or ""),
            category=str(item.get("category", "other") or "other"),
            confidence=float(item.get("confidence", 0.0) or 0.0),
            created_at=str(item.get("timestamp", "") or ""),
            metadata={k: v for k, v in item.items() if k not in {"action", "question", "answer", "category", "confidence", "timestamp"}},
        )

    def _stringify_dt(self, value: Any) -> Optional[str]:
        if not value:
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

def get_learning_workflow_service() -> LearningWorkflowService:
    from src.common.learning_dependency_factory import (
        get_learning_workflow_service as _get_learning_workflow_service,
    )

    return _get_learning_workflow_service()


__all__ = ["LearningWorkflowService", "get_learning_workflow_service"]
