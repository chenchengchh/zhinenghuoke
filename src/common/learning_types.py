from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class LearningWorkItem:
    id: str
    item_type: str
    status: str
    question: str = ""
    answer: str = ""
    category: str = "other"
    source: str = "unknown"
    confidence: float = 0.0
    keywords: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "item_type": self.item_type,
            "status": self.status,
            "question": self.question,
            "answer": self.answer,
            "category": self.category,
            "source": self.source,
            "confidence": self.confidence,
            "keywords": list(self.keywords),
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "priority": self.priority,
            "metadata": dict(self.metadata),
        }


@dataclass
class LearningHistoryItem:
    action: str
    status: str
    question: str = ""
    answer: str = ""
    category: str = "other"
    confidence: float = 0.0
    created_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "status": self.status,
            "question": self.question,
            "answer": self.answer,
            "category": self.category,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "timestamp": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass
class LearningStatsSnapshot:
    learning_enabled: bool = True
    total_learned: int = 0
    pending_validation: int = 0
    approved_knowledge: int = 0
    rejected_knowledge: int = 0
    auto_approved: int = 0
    unknown_questions_detected: int = 0
    accuracy_improvement: float = 0.0
    recent_learning: List[Dict[str, Any]] = field(default_factory=list)
    human_approved: int = 0
    rejected: int = 0
    pending: int = 0
    patterns_learned: int = 0
    gaps_auto_filled: int = 0
    knowledge_auto_created: int = 0
    feedback_collected: int = 0
    pending_review: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "learning_enabled": self.learning_enabled,
            "total_learned": self.total_learned,
            "pending_validation": self.pending_validation,
            "approved_knowledge": self.approved_knowledge,
            "rejected_knowledge": self.rejected_knowledge,
            "auto_approved": self.auto_approved,
            "unknown_questions_detected": self.unknown_questions_detected,
            "accuracy_improvement": self.accuracy_improvement,
            "recent_learning": list(self.recent_learning),
            "human_approved": self.human_approved,
            "rejected": self.rejected,
            "pending": self.pending,
            "patterns_learned": self.patterns_learned,
            "gaps_auto_filled": self.gaps_auto_filled,
            "knowledge_auto_created": self.knowledge_auto_created,
            "feedback_collected": self.feedback_collected,
            "pending_review": self.pending_review,
        }


__all__ = ["LearningWorkItem", "LearningHistoryItem", "LearningStatsSnapshot"]
