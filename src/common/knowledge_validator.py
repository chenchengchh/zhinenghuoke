"""
知识验证器
验证抽取的知识是否准确、完整、与现有知识一致
"""
import re
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from loguru import logger


class ValidationStatus(Enum):
    """
    验证状态枚举
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
    验证结果
    
    包含验证状态、原因和详细信息
    """
    knowledge_id: str
    status: ValidationStatus = ValidationStatus.PENDING
    reason: ValidationReason = ValidationReason.AUTO_QUALITY
    
    # 验证详情
    confidence: float = 0.0
    quality_score: float = 0.0
    conflict_score: float = 0.0
    duplicate_score: float = 0.0
    
    # 冲突信息
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    duplicates: List[Dict[str, Any]] = field(default_factory=list)
    
    # 建议
    suggestions: List[str] = field(default_factory=list)
    
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
            'conflicts': self.conflicts,
            'duplicates': self.duplicates,
            'suggestions': self.suggestions,
            'validated_at': self.validated_at.isoformat(),
            'validated_by': self.validated_by
        }


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
    duplicate_similarity_threshold: float = 0.9
    
    # 内容最小长度
    min_question_length: int = 5
    min_answer_length: int = 10


class KnowledgeValidator:
    """
    知识验证器
    
    验证流程：
    1. 内容完整性检查
    2. 质量评分
    3. 冲突检测
    4. 重复检测
    5. 综合判断
    """
    
    # 无效内容模式
    INVALID_PATTERNS = [
        r'^[？?！!。，,\.]+$',           # 只有标点
        r'^(不知道|暂无|待补充|无)',     # 无效标记
        r'^\s*$',                        # 空白
        r'(错误|失败|异常)',             # 错误标记
    ]
    
    # 问题模式
    QUESTION_PATTERNS = [
        r'[怎么如何什么为什么哪是否能不能有没有可以]',
    ]
    
    def __init__(
        self,
        config: ValidationConfig = None,
        knowledge_base=None,
        quality_scorer=None,
        conflict_detector=None
    ):
        """
        初始化知识验证器
        
        Args:
            config: 验证配置
            knowledge_base: 知识库实例
            quality_scorer: 质量评分器
            conflict_detector: 冲突检测器
        """
        self.config = config or ValidationConfig()
        self.knowledge_base = knowledge_base
        self.quality_scorer = quality_scorer
        self.conflict_detector = conflict_detector
        
        # 验证历史
        self._validation_history: Dict[str, List[ValidationResult]] = {}
        
        # 统计
        self._stats = {
            'total_validated': 0,
            'auto_approved': 0,
            'auto_rejected': 0,
            'needs_review': 0,
            'manual_approved': 0,
            'manual_rejected': 0
        }

    def _list_knowledge_items(self) -> List[Any]:
        if not self.knowledge_base:
            return []
        if hasattr(self.knowledge_base, "list_knowledge_items"):
            return list(self.knowledge_base.list_knowledge_items() or [])
        if hasattr(self.knowledge_base, "get_knowledge_list"):
            return list(self.knowledge_base.get_knowledge_list() or [])
        if hasattr(self.knowledge_base, "get_all_items"):
            return list(self.knowledge_base.get_all_items() or [])
        return list(getattr(self.knowledge_base, "knowledge_items", []) or [])
    
    def validate(
        self,
        knowledge_item: Any,
        check_conflicts: bool = True,
        check_duplicates: bool = True
    ) -> ValidationResult:
        """
        验证知识条目
        
        Args:
            knowledge_item: 知识条目
            check_conflicts: 是否检查冲突
            check_duplicates: 是否检查重复
            
        Returns:
            验证结果
        """
        knowledge_id = getattr(knowledge_item, 'id', str(id(knowledge_item)))
        
        result = ValidationResult(knowledge_id=knowledge_id)
        
        # 1. 内容完整性检查
        completeness = self._check_completeness(knowledge_item)
        if not completeness['is_complete']:
            result.status = ValidationStatus.REJECTED
            result.reason = ValidationReason.INCOMPLETE
            result.suggestions = completeness['issues']
            self._record_validation(result)
            return result
        
        # 2. 质量评分
        quality_result = self._evaluate_quality(knowledge_item)
        result.quality_score = quality_result['score']
        result.confidence = getattr(knowledge_item, 'confidence', 0.5)
        result.suggestions.extend(quality_result.get('suggestions', []))
        
        # 3. 冲突检测
        if check_conflicts and self.conflict_detector:
            conflict_result = self._check_conflicts(knowledge_item)
            result.conflict_score = conflict_result['score']
            result.conflicts = conflict_result['conflicts']
        
        # 4. 重复检测
        if check_duplicates and self.knowledge_base:
            duplicate_result = self._check_duplicates(knowledge_item)
            result.duplicate_score = duplicate_result['score']
            result.duplicates = duplicate_result['duplicates']
        
        # 5. 综合判断
        status, reason = self._make_decision(result)
        result.status = status
        result.reason = reason
        
        self._record_validation(result)
        return result
    
    def _check_completeness(self, knowledge_item: Any) -> Dict[str, Any]:
        """
        检查内容完整性
        
        Args:
            knowledge_item: 知识条目
            
        Returns:
            完整性检查结果
        """
        issues = []
        is_complete = True
        
        question = getattr(knowledge_item, 'question', '')
        answer = getattr(knowledge_item, 'answer', '')
        
        # 检查问题
        if not question or len(question.strip()) < self.config.min_question_length:
            issues.append("问题内容过短或为空")
            is_complete = False
        
        # 检查答案
        if not answer or len(answer.strip()) < self.config.min_answer_length:
            issues.append("答案内容过短或为空")
            is_complete = False
        
        # 检查无效内容
        for pattern in self.INVALID_PATTERNS:
            if re.search(pattern, answer, re.IGNORECASE):
                issues.append(f"答案包含无效内容")
                is_complete = False
                break
        
        return {
            'is_complete': is_complete,
            'issues': issues
        }
    
    def _evaluate_quality(self, knowledge_item: Any) -> Dict[str, Any]:
        """
        评估知识质量
        
        Args:
            knowledge_item: 知识条目
            
        Returns:
            质量评估结果
        """
        if self.quality_scorer:
            try:
                score_result = self.quality_scorer.score(knowledge_item)
                return {
                    'score': score_result.overall_score,
                    'suggestions': score_result.suggestions
                }
            except Exception as e:
                logger.warning(f"质量评分失败: {e}")
        
        # 简单质量评估
        question = getattr(knowledge_item, 'question', '')
        answer = getattr(knowledge_item, 'answer', '')
        
        score = 0.5
        
        # 问题质量
        if len(question) >= 10:
            score += 0.1
        if any(re.search(p, question) for p in self.QUESTION_PATTERNS):
            score += 0.1
        
        # 答案质量
        if len(answer) >= 30:
            score += 0.1
        if len(answer) >= 50:
            score += 0.1
        if '。' in answer:
            score += 0.1
        
        return {
            'score': min(score, 1.0),
            'suggestions': []
        }
    
    def _check_conflicts(self, knowledge_item: Any) -> Dict[str, Any]:
        """
        检查知识冲突
        
        Args:
            knowledge_item: 知识条目
            
        Returns:
            冲突检测结果
        """
        conflicts = []
        
        if self.conflict_detector and self.knowledge_base:
            try:
                all_conflicts = self.conflict_detector.detect_all_conflicts(
                    [knowledge_item] + self._list_knowledge_items()
                )
                knowledge_id = getattr(knowledge_item, 'id', str(id(knowledge_item)))
                conflicts = [
                    c.to_dict() for c in all_conflicts
                    if knowledge_id in c.knowledge_ids
                ]
            except Exception as e:
                logger.warning(f"冲突检测失败: {e}")
        
        conflict_score = 1.0 - (len(conflicts) * 0.2)
        
        return {
            'score': max(conflict_score, 0.0),
            'conflicts': conflicts
        }
    
    def _check_duplicates(self, knowledge_item: Any) -> Dict[str, Any]:
        """
        检查重复知识
        
        Args:
            knowledge_item: 知识条目
            
        Returns:
            重复检测结果
        """
        duplicates = []
        
        if self.knowledge_base:
            question = getattr(knowledge_item, 'question', '')
            answer = getattr(knowledge_item, 'answer', '')
            
            for existing in self._list_knowledge_items():
                existing_question = getattr(existing, 'question', '')
                existing_answer = getattr(existing, 'answer', '')
                
                # 计算相似度
                q_sim = self._calculate_similarity(question, existing_question)
                a_sim = self._calculate_similarity(answer, existing_answer)
                
                if q_sim > self.config.duplicate_similarity_threshold and a_sim > 0.8:
                    duplicates.append({
                        'id': getattr(existing, 'id', ''),
                        'question': existing_question,
                        'question_similarity': q_sim,
                        'answer_similarity': a_sim
                    })
        
        duplicate_score = 1.0 - (len(duplicates) * 0.3)
        
        return {
            'score': max(duplicate_score, 0.0),
            'duplicates': duplicates
        }
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """
        计算文本相似度
        
        Args:
            text1: 文本1
            text2: 文本2
            
        Returns:
            相似度 (0-1)
        """
        if not text1 or not text2:
            return 0.0
        
        # 简单的Jaccard相似度
        set1 = set(text1)
        set2 = set(text2)
        
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        
        if union == 0:
            return 0.0
        
        return intersection / union
    
    def _make_decision(self, result: ValidationResult) -> Tuple[ValidationStatus, ValidationReason]:
        """
        做出验证决策
        
        Args:
            result: 验证结果
            
        Returns:
            (状态, 原因)
        """
        # 自动批准条件
        if (result.quality_score >= self.config.auto_approve_quality_threshold and
            result.confidence >= self.config.auto_approve_confidence_threshold and
            result.conflict_score >= 0.7 and
            result.duplicate_score >= 0.7):
            return ValidationStatus.APPROVED, ValidationReason.AUTO_QUALITY
        
        # 自动拒绝条件
        if (result.quality_score < self.config.auto_reject_quality_threshold or
            result.confidence < self.config.auto_reject_confidence_threshold):
            return ValidationStatus.REJECTED, ValidationReason.LOW_CONFIDENCE
        
        # 存在冲突
        if result.conflicts:
            return ValidationStatus.CONFLICT, ValidationReason.AUTO_CONFLICT
        
        # 存在重复
        if result.duplicates:
            return ValidationStatus.NEEDS_REVIEW, ValidationReason.AUTO_DUPLICATE
        
        # 需要人工审核
        return ValidationStatus.NEEDS_REVIEW, ValidationReason.AUTO_QUALITY
    
    def _record_validation(self, result: ValidationResult):
        """
        记录验证结果
        
        Args:
            result: 验证结果
        """
        knowledge_id = result.knowledge_id
        
        if knowledge_id not in self._validation_history:
            self._validation_history[knowledge_id] = []
        
        self._validation_history[knowledge_id].append(result)
        
        # 更新统计
        self._stats['total_validated'] += 1
        
        if result.status == ValidationStatus.APPROVED:
            if result.validated_by == "auto":
                self._stats['auto_approved'] += 1
            else:
                self._stats['manual_approved'] += 1
        elif result.status == ValidationStatus.REJECTED:
            if result.validated_by == "auto":
                self._stats['auto_rejected'] += 1
            else:
                self._stats['manual_rejected'] += 1
        elif result.status == ValidationStatus.NEEDS_REVIEW:
            self._stats['needs_review'] += 1
    
    def approve(self, knowledge_id: str, approver: str = "human") -> bool:
        """
        人工批准知识
        
        Args:
            knowledge_id: 知识ID
            approver: 审批人
            
        Returns:
            是否成功
        """
        history = self._validation_history.get(knowledge_id, [])
        if history:
            result = history[-1]
            result.status = ValidationStatus.APPROVED
            result.reason = ValidationReason.MANUAL_APPROVE
            result.validated_by = approver
            result.validated_at = datetime.now()
            self._stats['manual_approved'] += 1
            return True
        return False
    
    def reject(self, knowledge_id: str, reason: str = "", rejector: str = "human") -> bool:
        """
        人工拒绝知识
        
        Args:
            knowledge_id: 知识ID
            reason: 拒绝原因
            rejector: 拒绝人
            
        Returns:
            是否成功
        """
        history = self._validation_history.get(knowledge_id, [])
        if history:
            result = history[-1]
            result.status = ValidationStatus.REJECTED
            result.reason = ValidationReason.MANUAL_REJECT
            result.validated_by = rejector
            result.validated_at = datetime.now()
            if reason:
                result.suggestions.append(f"拒绝原因: {reason}")
            self._stats['manual_rejected'] += 1
            return True
        return False
    
    def batch_validate(
        self,
        knowledge_items: List[Any],
        check_conflicts: bool = True,
        check_duplicates: bool = True
    ) -> Dict[str, ValidationResult]:
        """
        批量验证知识
        
        Args:
            knowledge_items: 知识条目列表
            check_conflicts: 是否检查冲突
            check_duplicates: 是否检查重复
            
        Returns:
            ID到验证结果的映射
        """
        results = {}
        
        for item in knowledge_items:
            item_id = getattr(item, 'id', str(id(item)))
            results[item_id] = self.validate(item, check_conflicts, check_duplicates)
        
        return results
    
    def get_validation_history(self, knowledge_id: str) -> List[ValidationResult]:
        """
        获取验证历史
        
        Args:
            knowledge_id: 知识ID
            
        Returns:
            验证结果列表
        """
        return self._validation_history.get(knowledge_id, [])
    
    def get_pending_review(self) -> List[ValidationResult]:
        """
        获取待审核的知识
        
        Returns:
            待审核列表
        """
        pending = []
        
        for history in self._validation_history.values():
            if history:
                latest = history[-1]
                if latest.status in [ValidationStatus.PENDING, ValidationStatus.NEEDS_REVIEW, ValidationStatus.CONFLICT]:
                    pending.append(latest)
        
        return pending
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        获取验证统计
        
        Returns:
            统计信息
        """
        return {
            'stats': self._stats.copy(),
            'pending_count': len(self.get_pending_review()),
            'history_size': sum(len(h) for h in self._validation_history.values())
        }


# 全局实例
_knowledge_validator = None


def get_knowledge_validator(
    config: ValidationConfig = None,
    knowledge_base=None,
    quality_scorer=None,
    conflict_detector=None
) -> KnowledgeValidator:
    """
    获取知识验证器单例
    
    Args:
        config: 验证配置
        knowledge_base: 知识库实例
        quality_scorer: 质量评分器
        conflict_detector: 冲突检测器
        
    Returns:
        知识验证器实例
    """
    global _knowledge_validator
    if _knowledge_validator is None:
        _knowledge_validator = KnowledgeValidator(
            config, knowledge_base, quality_scorer, conflict_detector
        )
    return _knowledge_validator
