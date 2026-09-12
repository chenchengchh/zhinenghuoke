"""
统一验证类型定义

整合所有验证相关的数据模型，避免重复定义
"""
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from datetime import datetime
from enum import Enum


class ValidationStatus(Enum):
    """
    验证状态枚举
    
    整合了 knowledge_validator.py 中的定义
    """
    PENDING = "pending"           # 待验证
    APPROVED = "approved"         # 已批准
    REJECTED = "rejected"         # 已拒绝
    NEEDS_REVIEW = "needs_review" # 需要人工审核
    CONFLICT = "conflict"         # 存在冲突


class ValidationReason(Enum):
    """
    验证原因枚举
    """
    AUTO_QUALITY = "auto_quality"         # 自动质量检查通过
    AUTO_CONFLICT = "auto_conflict"       # 自动检测到冲突
    AUTO_DUPLICATE = "auto_duplicate"     # 自动检测到重复
    MANUAL_APPROVE = "manual_approve"     # 人工批准
    MANUAL_REJECT = "manual_reject"       # 人工拒绝
    LOW_CONFIDENCE = "low_confidence"     # 置信度过低
    INCOMPLETE = "incomplete"             # 内容不完整


@dataclass
class ValidationResult:
    """
    统一验证结果数据模型
    
    整合了 knowledge_validator.py 和 types/knowledge.py 中的定义
    """
    knowledge_id: str
    status: ValidationStatus = ValidationStatus.PENDING
    reason: ValidationReason = ValidationReason.AUTO_QUALITY
    
    # 验证详情
    confidence: float = 0.0
    quality_score: float = 0.0
    conflict_score: float = 0.0
    duplicate_score: float = 0.0
    score: float = 0.0
    
    # 冲突信息
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    duplicates: List[Dict[str, Any]] = field(default_factory=list)
    
    # 建议
    suggestions: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    
    # 验证时间
    validated_at: datetime = field(default_factory=datetime.now)
    validated_by: str = "auto"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'knowledge_id': self.knowledge_id,
            'status': self.status.value,
            'reason': self.reason.value,
            'confidence': self.confidence,
            'quality_score': self.quality_score,
            'conflict_score': self.conflict_score,
            'duplicate_score': self.duplicate_score,
            'score': self.score,
            'conflicts': self.conflicts,
            'duplicates': self.duplicates,
            'suggestions': self.suggestions,
            'issues': self.issues,
            'validated_at': self.validated_at.isoformat(),
            'validated_by': self.validated_by
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'ValidationResult':
        """从字典创建"""
        return cls(
            knowledge_id=data.get("knowledge_id", ""),
            status=ValidationStatus(data.get("status", "pending")),
            reason=ValidationReason(data.get("reason", "auto_quality")),
            confidence=data.get("confidence", 0.0),
            quality_score=data.get("quality_score", 0.0),
            conflict_score=data.get("conflict_score", 0.0),
            duplicate_score=data.get("duplicate_score", 0.0),
            score=data.get("score", 0.0),
            conflicts=data.get("conflicts", []),
            duplicates=data.get("duplicates", []),
            suggestions=data.get("suggestions", []),
            issues=data.get("issues", []),
            validated_by=data.get("validated_by", "auto")
        )


@dataclass
class ValidationConfig:
    """
    验证配置
    
    定义验证的各项阈值
    """
    # 自动批准阈值
    auto_approve_quality_threshold: float = 0.8
    auto_approve_confidence_threshold: float = 0.85
    
    # 自动拒绝阈值
    auto_reject_quality_threshold: float = 0.3
    auto_reject_confidence_threshold: float = 0.3
    
    # 冲突检测阈值
    conflict_similarity_threshold: float = 0.7
    
    # 重复检测阈值
    duplicate_similarity_threshold: float = 0.85
    
    # 需要人工审核的置信度范围
    manual_review_confidence_range: tuple = (0.5, 0.8)
