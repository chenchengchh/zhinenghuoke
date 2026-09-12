"""
知识质量管理系统

提供知识库质量评估、审核、优化功能

功能：
1. 知识质量评分
2. 自动审核流程
3. 知识去重检测
4. 质量报告生成
5. 改进建议生成
"""

import time
import logging
import re
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from collections import defaultdict

from loguru import logger


class QualityLevel(Enum):
    """质量等级"""
    EXCELLENT = "excellent"
    GOOD = "good"
    ACCEPTABLE = "acceptable"
    POOR = "poor"
    CRITICAL = "critical"


class AuditStatus(Enum):
    """审核状态"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


@dataclass
class KnowledgeQuality:
    """知识质量评估结果"""
    completeness: float = 0.0
    accuracy: float = 0.0
    freshness: float = 0.0
    usage_rate: float = 0.0
    feedback_score: float = 0.0
    total_score: float = 0.0
    level: QualityLevel = QualityLevel.ACCEPTABLE
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    
    @property
    def score(self) -> float:
        return self.total_score
    
    
@dataclass
class AuditRecord:
    """审核记录"""
    item_id: str
    status: AuditStatus = AuditStatus.PENDING
    score: float = 0.0
    reviewer: str = ""
    comment: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)


    def to_dict(self) -> Dict:
        return {
            "item_id": self.item_id,
            "status": self.status.value,
            "score": self.score,
            "reviewer": self.reviewer,
            "comment": self.comment,
            "timestamp": self.timestamp.isoformat(),
            "issues": self.issues,
            "suggestions": self.suggestions
        }


class LegacyQualityEvaluator:
    """
    旧版知识质量评分器（已废弃，请使用 knowledge_quality_scorer.KnowledgeQualityScorer）
    
    评估知识条目的多个维度：
    """
    
    MIN_QUESTION_LENGTH = 5
    MIN_ANSWER_LENGTH = 10
    MIN_KEYWORDS_COUNT = 3
    
    MAX_DUPLICATE_RATIO = 0.8
    
    FRESHNESS_DAYS = 90
    
    QUALITY_WEIGHTS = {
        "completeness": 0.25,
        "accuracy": 0.30,
        "freshness": 0.15,
        "usage_rate": 0.15,
        "feedback_score": 0.15
    }
    
    def __init__(self, knowledge_base=None):
        """
        初始化质量评分器
        
        Args:
            knowledge_base: 知识库实例
        """
        self.knowledge_base = knowledge_base
        self._duplicate_detector = DuplicateDetector()
    
    def evaluate(self, item: Any) -> KnowledgeQuality:
        """
        评估知识条目质量
        
        Args:
            item: 知识条目
            
        Returns:
            KnowledgeQuality: 质量评估结果
        """
        quality = KnowledgeQuality()
        
        quality.completeness = self._evaluate_completeness(item)
        quality.accuracy = self._evaluate_accuracy(item)
        quality.freshness = self._evaluate_freshness(item)
        quality.usage_rate = self._evaluate_usage_rate(item)
        quality.feedback_score = self._evaluate_feedback(item)

        quality.total_score = (
            quality.completeness * self.QUALITY_WEIGHTS.get('completeness', 0.2) +
            quality.accuracy * self.QUALITY_WEIGHTS.get('accuracy', 0.2) +
            quality.freshness * self.QUALITY_WEIGHTS.get('freshness', 0.2) +
            quality.usage_rate * self.QUALITY_WEIGHTS.get('usage_rate', 0.2) +
            quality.feedback_score * self.QUALITY_WEIGHTS.get('feedback_score', 0.2)
        )
        quality.level = self._determine_level(quality.total_score)
        self._identify_issues(item, quality)
        self._generate_suggestions(item, quality)
        
        return quality
    
    def _evaluate_completeness(self, item: Any) -> float:
        """评估完整性"""
        score = 100.0
        
        question = self._get_field(item, "question", "")
        answer = self._get_field(item, "answer", "")
        keywords = self._get_field(item, "keywords", [])
        tags = self._get_field(item, "tags", [])
        aliases = self._get_field(item, "aliases", [])
        
        if not question:
            score -= 50
        elif len(question) < self.MIN_QUESTION_LENGTH:
            score -= 30
        
        else:
            score += 20
        
        if not answer:
            score -= 50
        elif len(answer) < self.MIN_ANSWER_LENGTH:
            score -= 30
        else:
            score += 20
        
        if not keywords and not tags and not aliases:
            score -= 20
        elif keywords and len(keywords) >= self.MIN_KEYWORDS_COUNT:
            score += 10
        
        if tags and len(tags) >= 2:
            score += 5
        
        if aliases and len(aliases) >= 1:
            score += 5
        
        return max(0, min(100, score))
    
    def _evaluate_accuracy(self, item: Any) -> float:
        """评估准确性"""
        score = 60.0
        
        question = self._get_field(item, "question", "")
        answer = self._get_field(item, "answer", "")
        keywords = self._get_field(item, "keywords", [])
        
        if not question or not answer:
            return 0.0
        
        question_lower = question.lower() if question else ""
        answer_lower = answer.lower() if answer else ""
        
        if "..." in answer:
            score -= 20
        
        if "不知道" in answer:
            score -= 30
        
        if "暂无" in answer:
            score -= 20
        
        if keywords:
            for kw in keywords:
                if kw.lower() in question_lower or kw.lower() in answer_lower:
                    score += 10
        
        return max(0, min(100, score))
    
    def _evaluate_freshness(self, item: Any) -> float:
        """评估时效性"""
        updated_at = self._get_field(item, "updated_at", None)
        
        if not updated_at:
            return 50.0
        
        try:
            if isinstance(updated_at, str):
                update_time = datetime.fromisoformat(updated_at)
            elif isinstance(updated_at, datetime):
                update_time = updated_at
            else:
                return 50.0
            
            
            days_diff = (datetime.now() - update_time).days
            
            if days_diff > self.FRESHNESS_DAYS:
                return 20.0
            elif days_diff > self.FRESHNESS_DAYS // 2:
                return 40.0
            elif days_diff > 30:
                return 60.0
            elif days_diff > 7:
                return 80.0
            else:
                return 100.0
        except Exception:
            return 50.0
    
    def _evaluate_usage_rate(self, item: Any) -> float:
        """评估使用率"""
        use_count = self._get_field(item, "use_count", 0)
        
        if use_count == 0:
            return 30.0
        elif use_count < 5:
            return 50.0
        elif use_count < 20:
            return 70.0
        elif use_count < 50:
            return 85.0
        else:
            return 100.0
    
    def _evaluate_feedback(self, item: Any) -> float:
        """评估反馈评分"""
        if hasattr(item, "feedback_score"):
            feedback = item.feedback_score
            if feedback > 0:
                return min(feedback * 20, 100)
        
        if hasattr(item, "rating"):
            rating = item.rating
            if rating > 0:
                return rating * 20
        
        return 50.0
    
    def _determine_level(self, score: float) -> QualityLevel:
        """确定质量等级"""
        if score >= 85:
            return QualityLevel.EXCELLENT
        elif score >= 70:
            return QualityLevel.GOOD
        elif score >= 50:
            return QualityLevel.ACCEPTABLE
        elif score >= 30:
            return QualityLevel.POOR
        else:
            return QualityLevel.CRITICAL
    
    def _identify_issues(self, item: Any, quality: KnowledgeQuality):
        """识别问题"""
        question = self._get_field(item, "question", "")
        answer = self._get_field(item, "answer", "")
        keywords = self._get_field(item, "keywords", [])
        
        if not question:
            quality.issues.append("问题为空")
        elif len(question) < self.MIN_QUESTION_LENGTH:
            quality.issues.append("问题过短")
        
        if not answer:
            quality.issues.append("答案为空")
        elif len(answer) < self.MIN_ANSWER_LENGTH:
            quality.issues.append("答案过短")
        
        if not keywords:
            quality.issues.append("缺少关键词")
        
        if "..." in answer:
            quality.issues.append("答案不完整")
        
        if quality.freshness < 30:
            quality.issues.append("内容过时")
    
    def _generate_suggestions(self, item: Any, quality: KnowledgeQuality):
        """生成改进建议"""
        if quality.completeness < 70:
            quality.suggestions.append("完善问题的答案内容")
        
        if quality.accuracy < 70:
            quality.suggestions.append("检查内容相关性，确保答案准确")
        
    def _get_field(self, item: Any, field_name: str, default: Any = None) -> Any:
        """获取字段值"""
        if hasattr(item, field_name):
                return getattr(item, field_name)
        elif isinstance(item, dict):
                return item.get(field_name, default)
        return default


    
    def batch_evaluate(self, items: List[Any]) -> List[KnowledgeQuality]:
        """批量评估"""
        return [self.evaluate(item) for item in items]


class DuplicateDetector:
    """
    重复检测器
    
    检测知识库中的重复条目
    """
    
    def __init__(self, similarity_threshold: float = 0.85):
        self.similarity_threshold = similarity_threshold
    
    def detect(self, items: List[Any]) -> List[Dict[str, List[int]]]:
        """
        检测重复条目
        
        Args:
            items: 知识条目列表
            
        Returns:
            {问题文本: [重复条目索引列表]}
        """
        duplicates = defaultdict(list)
        
        for i, item in enumerate(items):
            question = self._get_question(item)
            if question:
                question_normalized = self._normalize(question)
                duplicates[question_normalized].append(i)
        
        result = {}
        for question, indices in duplicates.items():
            if len(indices) > 1:
                result[question] = indices
        
        return result
    
    def _get_question(self, item: Any) -> str:
        """获取问题"""
        if hasattr(item, "question"):
            return item.question
        elif isinstance(item, dict):
                return item.get("question", "")
        return ""
    
    def _normalize(self, text: str) -> str:
        """标准化文本"""
        text = text.lower().strip()
        text = re.sub(r'[^\w\s]', '', text)
        text = ' '.join(text.split())
        return text


    
    def get_duplicate_groups(self, items: List[Any]) -> List[Tuple[Any, List[Any]]]:
        """
        获取重复组
        
        Returns:
            [(代表条目, [重复条目列表])]
        """
        duplicates = self.detect(items)
        result = []
        
        for question, indices in duplicates.items():
            group = [items[i] for i in indices]
            if group:
                result.append((group[0], group[1:]))
        
        return result


class KnowledgeAuditManager:
    """
    知识审核管理器
    
    管理知识条目的审核流程
    """
    
    AUTO_APPROVE_THRESHOLD = 80.0
    AUTO_REJECT_THRESHOLD = 30.0
    
    NEEDS_REVIEW_THRESHOLD = 50.0
    
    def __init__(self, knowledge_base=None, auto_approve: bool = True):
        """
        初始化审核管理器
        
        Args:
            knowledge_base: 知识库实例
            auto_approve: 是否自动审批
        """
        self.knowledge_base = knowledge_base
        self.auto_approve = auto_approve
        self.scorer = LegacyQualityEvaluator(knowledge_base)
        self.duplicate_detector = DuplicateDetector()
        
        self._audit_records: Dict[str, AuditRecord] = {}
        self._pending_audit: List[str] = []
    
    def audit_item(self, item: Any) -> AuditRecord:
        """
        审核单个条目
        
        Args:
            item: 知识条目
            
        Returns:
            AuditRecord: 审核记录
        """
        item_id = self._get_item_id(item)
        
        if item_id in self._audit_records:
            return self._audit_records[item_id]
        
        quality = self.scorer.evaluate(item)
        
        record = AuditRecord(
            item_id=item_id,
            score=quality.total_score,
            status=AuditStatus.PENDING,
            issues=quality.issues.copy(),
            suggestions=quality.suggestions.copy()
        )
        
        if self.auto_approve:
            record = self._auto_decide(record, quality)
        else:
            record.status = AuditStatus.PENDING
        
        
        self._audit_records[item_id] = record
        
        return record
    
    def _auto_decide(self, record: AuditRecord, quality: KnowledgeQuality) -> AuditRecord:
        """自动决策"""
        if quality.total_score >= self.AUTO_APPROVE_THRESHOLD:
            record.status = AuditStatus.APPROVED
            record.comment = "质量达标，自动通过"
        elif quality.total_score < self.AUTO_REJECT_THRESHOLD:
            record.status = AuditStatus.REJECTED
            record.comment = "质量不达标，自动拒绝"
        elif quality.total_score < self.NEEDS_REVIEW_THRESHOLD:
            record.status = AuditStatus.NEEDS_REVISION
            record.comment = "需要人工审核"
        else:
            record.status = AuditStatus.PENDING
            record.comment = "等待人工审核"
        
        return record
    
    def batch_audit(self, items: List[Any]) -> List[AuditRecord]:
        """批量审核"""
        return [self.audit_item(item) for item in items]
    
    def get_audit_record(self, item_id: str) -> Optional[AuditRecord]:
        """获取审核记录"""
        return self._audit_records.get(item_id)
    
    def approve(self, item_id: str, reviewer: str = "system", comment: str = "") -> bool:
        """批准条目"""
        if item_id not in self._audit_records:
            return False
        
        record = self._audit_records[item_id]
        record.status = AuditStatus.APPROVED
        record.reviewer = reviewer
        record.comment = comment or record.comment
        record.timestamp = datetime.now()
        
        return True
    
    def reject(self, item_id: str, reviewer: str = "system", comment: str = "") -> bool:
        """拒绝条目"""
        if item_id not in self._audit_records:
            return False
        
        record = self._audit_records[item_id]
        record.status = AuditStatus.REJECTED
        record.reviewer = reviewer
        record.comment = comment or record.comment
        record.timestamp = datetime.now()
        
        return True
    
    def request_revision(self, item_id: str, comment: str = "") -> bool:
        """请求修订"""
        if item_id not in self._audit_records:
            return False
        
        record = self._audit_records[item_id]
        record.status = AuditStatus.NEEDS_REVISION
        record.comment = comment or record.comment
        record.timestamp = datetime.now()
        
        return True
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取审核统计"""
        total = len(self._audit_records)
        approved = sum(1 for r in self._audit_records.values() if r.status == AuditStatus.APPROVED)
        rejected = sum(1 for r in self._audit_records.values() if r.status == AuditStatus.REJECTED)
        pending = sum(1 for r in self._audit_records.values() if r.status == AuditStatus.PENDING)
        needs_revision = sum(1 for r in self._audit_records.values() if r.status == AuditStatus.NEEDS_REVISION)
        
        avg_score = sum(r.score for r in self._audit_records.values()) / max(total, 1) if total > 0 else 0
        
        return {
            "total": total,
            "approved": approved,
            "rejected": rejected,
            "pending": pending,
            "needs_revision": needs_revision,
            "average_score": round(avg_score, 2),
            "approval_rate": round(approved / total * 100, 2) if total > 0 else 0
        }
    
    def _get_item_id(self, item: Any) -> str:
        """获取条目ID"""
        if hasattr(item, "id"):
            return str(item.id)
        elif isinstance(item, dict):
            return str(item.get("id", ""))
        return str(hash(str(item)))


    
    def generate_quality_report(self, items: List[Any] = None) -> Dict[str, Any]:
        """生成质量报告"""
        if not items:
            return None
        
        qualities = self.scorer.batch_evaluate(items)
        duplicates = self.duplicate_detector.detect(items)
        
        total_score = sum(q.total_score for q in qualities) / len(qualities)
        avg_completeness = sum(q.completeness for q in qualities) / len(qualities)
        avg_accuracy = sum(q.accuracy for q in qualities) / len(qualities)
        avg_freshness = sum(q.freshness for q in qualities) / len(qualities)
        
        excellent_count = sum(1 for q in qualities if q.level == QualityLevel.EXCELLENT)
        good_count = sum(1 for q in qualities if q.level == QualityLevel.GOOD)
        acceptable_count = sum(1 for q in qualities if q.level == QualityLevel.ACCEPTABLE)
        poor_count = sum(1 for q in qualities if q.level == QualityLevel.POOR)
        critical_count = sum(1 for q in qualities if q.level == QualityLevel.CRITICAL)
        
        return {
            "total_items": len(items),
            "average_score": round(total_score, 2),
            "average_completeness": round(avg_completeness, 2),
            "average_accuracy": round(avg_accuracy, 2),
            "average_freshness": round(avg_freshness, 2),
            "quality_distribution": {
                "excellent": excellent_count,
                "good": good_count,
                "acceptable": acceptable_count,
                "poor": poor_count,
                "critical": critical_count
            },
            "duplicate_groups": len(duplicates),
            "recommendations": [
                "建议扩充高质量知识条目",
                "建议清理低质量知识条目",
                "建议定期更新过时内容"
            ]
        }


from functools import lru_cache

@lru_cache(maxsize=1)
def get_quality_manager(knowledge_base=None, auto_approve: bool = True) -> KnowledgeAuditManager:
    return KnowledgeAuditManager(knowledge_base, auto_approve)



