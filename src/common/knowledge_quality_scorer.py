"""
知识质量评分系统
评估知识条目的完整性、准确性、相关性和时效性
"""
import re
import math
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from loguru import logger


@dataclass
class QualityScore:
    """
    知识质量评分结果
    
    包含各维度的评分和综合评分
    """
    # 综合评分
    overall_score: float = 0.0
    
    # 各维度评分
    completeness_score: float = 0.0  # 完整性
    accuracy_score: float = 0.0      # 准确性
    relevance_score: float = 0.0     # 相关性
    timeliness_score: float = 0.0    # 时效性
    
    # 评分详情
    details: Dict[str, Any] = field(default_factory=dict)
    
    # 改进建议
    suggestions: List[str] = field(default_factory=list)


class KnowledgeQualityScorer:
    """
    知识质量评分器
    
    评估知识条目的质量，包括：
    - 完整性：问题是否清晰，答案是否完整
    - 准确性：内容是否正确，是否有矛盾
    - 相关性：是否与业务相关
    - 时效性：知识是否过时
    """
    
    # 评分权重
    WEIGHTS = {
        'completeness': 0.30,  # 完整性权重
        'accuracy': 0.30,      # 准确性权重
        'relevance': 0.25,     # 相关性权重
        'timeliness': 0.15     # 时效性权重
    }
    
    # 业务关键词
    BUSINESS_KEYWORDS = [
        '营销', '推广', '获客', '转化', '客户', '用户',
        '价格', '费用', '套餐', '功能', '产品', '系统',
        '服务', '售后', '合作', '代理', '直播', '抖音',
        '活动', '策划', '方案', '效果', '数据'
    ]
    
    # 时效性关键词（容易过时的内容）
    TIME_SENSITIVE_KEYWORDS = [
        '最新', '当前', '现在', '目前', '今年', '本月',
        '优惠', '活动', '促销', '限时', '特价'
    ]
    
    def __init__(self, knowledge_base=None):
        """
        初始化知识质量评分器
        
        Args:
            knowledge_base: 知识库实例（用于冲突检测）
        """
        self.knowledge_base = knowledge_base
        self._score_cache: Dict[str, QualityScore] = {}
    
    def score(self, knowledge_item) -> QualityScore:
        """
        评估知识条目的质量
        
        Args:
            knowledge_item: 知识条目
            
        Returns:
            QualityScore: 质量评分结果
        """
        # 计算各维度评分
        completeness = self._score_completeness(knowledge_item)
        accuracy = self._score_accuracy(knowledge_item)
        relevance = self._score_relevance(knowledge_item)
        timeliness = self._score_timeliness(knowledge_item)
        
        # 计算综合评分
        overall = (
            completeness * self.WEIGHTS['completeness'] +
            accuracy * self.WEIGHTS['accuracy'] +
            relevance * self.WEIGHTS['relevance'] +
            timeliness * self.WEIGHTS['timeliness']
        )
        
        # 生成改进建议
        suggestions = self._generate_suggestions(
            completeness, accuracy, relevance, timeliness
        )
        
        return QualityScore(
            overall_score=round(overall, 2),
            completeness_score=round(completeness, 2),
            accuracy_score=round(accuracy, 2),
            relevance_score=round(relevance, 2),
            timeliness_score=round(timeliness, 2),
            details={
                'completeness_details': self._get_completeness_details(knowledge_item),
                'accuracy_details': self._get_accuracy_details(knowledge_item),
                'relevance_details': self._get_relevance_details(knowledge_item),
                'timeliness_details': self._get_timeliness_details(knowledge_item)
            },
            suggestions=suggestions
        )
    
    def _score_completeness(self, knowledge_item) -> float:
        """
        评分完整性
        
        检查：
        - 问题是否清晰
        - 答案是否完整
        - 关键词是否充足
        """
        score = 0.0
        
        question = getattr(knowledge_item, 'question', '')
        answer = getattr(knowledge_item, 'answer', '')
        keywords = getattr(knowledge_item, 'keywords', [])
        
        # 问题完整性 (0.4)
        if len(question) >= 5:
            score += 0.1
        if len(question) >= 10:
            score += 0.1
        if any(word in question for word in ['怎么', '如何', '什么', '为什么', '哪']):
            score += 0.1
        if question.endswith('？') or question.endswith('?'):
            score += 0.1
        
        # 答案完整性 (0.5)
        if len(answer) >= 10:
            score += 0.1
        if len(answer) >= 30:
            score += 0.1
        if len(answer) >= 50:
            score += 0.1
        if '。' in answer or '！' in answer:
            score += 0.1
        if len(answer.split('。')) >= 2:  # 多句话
            score += 0.1
        
        # 关键词完整性 (0.1)
        if len(keywords) >= 2:
            score += 0.05
        if len(keywords) >= 3:
            score += 0.05
        
        return min(score, 1.0)
    
    def _score_accuracy(self, knowledge_item) -> float:
        """
        评分准确性
        
        检查：
        - 内容是否合理
        - 是否有明显错误
        - 是否有矛盾
        """
        score = 0.7  # 基础分
        
        question = getattr(knowledge_item, 'question', '')
        answer = getattr(knowledge_item, 'answer', '')
        
        # 检查是否有明显的错误模式
        error_patterns = [
            r'错误',
            r'不正确',
            r'无法回答',
            r'不知道',
            r'暂无',
            r'待补充',
        ]
        
        for pattern in error_patterns:
            if re.search(pattern, answer):
                score -= 0.1
        
        # 检查答案是否与问题相关
        question_keywords = set(question)
        answer_keywords = set(answer)
        common = question_keywords & answer_keywords
        
        if len(common) >= 2:
            score += 0.1
        elif len(common) == 0:
            score -= 0.1
        
        # 检查是否有数字/日期等具体信息（更可信）
        if re.search(r'\d+', answer):
            score += 0.05
        
        # 检查是否有具体步骤（更可信）
        if any(word in answer for word in ['步骤', '首先', '然后', '最后', '第一', '第二']):
            score += 0.05
        
        return max(min(score, 1.0), 0.0)
    
    def _score_relevance(self, knowledge_item) -> float:
        """
        评分相关性
        
        检查：
        - 是否与业务相关
        - 是否有业务关键词
        """
        score = 0.0
        
        question = getattr(knowledge_item, 'question', '')
        answer = getattr(knowledge_item, 'answer', '')
        category = getattr(knowledge_item, 'category', '')
        
        combined_text = question + " " + answer
        
        # 检查业务关键词
        matched_keywords = []
        for keyword in self.BUSINESS_KEYWORDS:
            if keyword in combined_text:
                matched_keywords.append(keyword)
        
        # 根据匹配的关键词数量评分
        if len(matched_keywords) >= 1:
            score += 0.3
        if len(matched_keywords) >= 2:
            score += 0.2
        if len(matched_keywords) >= 3:
            score += 0.2
        if len(matched_keywords) >= 5:
            score += 0.2
        
        # 分类相关性
        if category and category != '其他':
            score += 0.1
        
        return min(score, 1.0)
    
    def _score_timeliness(self, knowledge_item) -> float:
        """
        评分时效性
        
        检查：
        - 知识创建时间
        - 最后使用时间
        - 是否有时效性关键词
        """
        score = 0.8  # 默认高分
        
        # 检查创建时间
        created_at = getattr(knowledge_item, 'created_at', None)
        if created_at:
            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at)
                except (ValueError, TypeError) as e:
                    logger.debug(f"created_at解析失败: {e}")
                    created_at = None
            
            if created_at:
                days_old = (datetime.now() - created_at).days
                
                if days_old > 180:
                    score -= 0.2
                elif days_old > 90:
                    score -= 0.1
                elif days_old > 30:
                    score -= 0.05
        
        # 检查最后使用时间
        last_used = getattr(knowledge_item, 'last_used', None)
        if last_used:
            if isinstance(last_used, str):
                try:
                    last_used = datetime.fromisoformat(last_used)
                except (ValueError, TypeError) as e:
                    logger.debug(f"last_used解析失败: {e}")
                    last_used = None
            
            if last_used:
                days_unused = (datetime.now() - last_used).days
                
                if days_unused > 60:
                    score -= 0.2
                elif days_unused > 30:
                    score -= 0.1
        
        # 检查时效性关键词
        question = getattr(knowledge_item, 'question', '')
        answer = getattr(knowledge_item, 'answer', '')
        
        for keyword in self.TIME_SENSITIVE_KEYWORDS:
            if keyword in question or keyword in answer:
                score -= 0.1
                break
        
        return max(min(score, 1.0), 0.0)
    
    def _get_completeness_details(self, knowledge_item) -> Dict[str, Any]:
        """获取完整性详情"""
        question = getattr(knowledge_item, 'question', '')
        answer = getattr(knowledge_item, 'answer', '')
        keywords = getattr(knowledge_item, 'keywords', [])
        
        return {
            'question_length': len(question),
            'answer_length': len(answer),
            'keywords_count': len(keywords),
            'has_question_mark': question.endswith('？') or question.endswith('?'),
            'has_multiple_sentences': len(answer.split('。')) >= 2
        }
    
    def _get_accuracy_details(self, knowledge_item) -> Dict[str, Any]:
        """获取准确性详情"""
        answer = getattr(knowledge_item, 'answer', '')
        
        return {
            'has_numbers': bool(re.search(r'\d+', answer)),
            'has_steps': any(word in answer for word in ['步骤', '首先', '然后', '最后']),
            'answer_length': len(answer)
        }
    
    def _get_relevance_details(self, knowledge_item) -> Dict[str, Any]:
        """获取相关性详情"""
        question = getattr(knowledge_item, 'question', '')
        answer = getattr(knowledge_item, 'answer', '')
        
        combined = question + " " + answer
        matched = [kw for kw in self.BUSINESS_KEYWORDS if kw in combined]
        
        return {
            'matched_keywords': matched,
            'keywords_count': len(matched),
            'category': getattr(knowledge_item, 'category', '')
        }
    
    def _get_timeliness_details(self, knowledge_item) -> Dict[str, Any]:
        """获取时效性详情"""
        created_at = getattr(knowledge_item, 'created_at', None)
        last_used = getattr(knowledge_item, 'last_used', None)
        
        details = {
            'created_at': str(created_at) if created_at else None,
            'last_used': str(last_used) if last_used else None,
            'is_time_sensitive': False
        }
        
        question = getattr(knowledge_item, 'question', '')
        answer = getattr(knowledge_item, 'answer', '')
        
        for keyword in self.TIME_SENSITIVE_KEYWORDS:
            if keyword in question or keyword in answer:
                details['is_time_sensitive'] = True
                break
        
        return details
    
    def _generate_suggestions(
        self,
        completeness: float,
        accuracy: float,
        relevance: float,
        timeliness: float
    ) -> List[str]:
        """生成改进建议"""
        suggestions = []
        
        if completeness < 0.6:
            suggestions.append("建议完善问题和答案内容，添加更多关键词")
        
        if accuracy < 0.6:
            suggestions.append("建议核实答案准确性，添加具体数据和步骤")
        
        if relevance < 0.6:
            suggestions.append("建议添加更多业务相关关键词，明确分类")
        
        if timeliness < 0.6:
            suggestions.append("建议更新知识内容，检查是否有过时信息")
        
        if not suggestions:
            suggestions.append("知识质量良好，建议定期维护")
        
        return suggestions
    
    def batch_score(self, knowledge_items: List[Any]) -> Dict[str, QualityScore]:
        """
        批量评分
        
        Args:
            knowledge_items: 知识条目列表
            
        Returns:
            Dict[str, QualityScore]: ID到评分的映射
        """
        results = {}
        
        for item in knowledge_items:
            item_id = getattr(item, 'id', str(id(item)))
            results[item_id] = self.score(item)
        
        return results
    
    def get_quality_stats(self, knowledge_items: List[Any]) -> Dict[str, Any]:
        """
        获取质量统计
        
        Args:
            knowledge_items: 知识条目列表
            
        Returns:
            统计信息
        """
        scores = self.batch_score(knowledge_items)
        
        if not scores:
            return {'total': 0}
        
        overall_scores = [s.overall_score for s in scores.values()]
        completeness_scores = [s.completeness_score for s in scores.values()]
        accuracy_scores = [s.accuracy_score for s in scores.values()]
        relevance_scores = [s.relevance_score for s in scores.values()]
        timeliness_scores = [s.timeliness_score for s in scores.values()]
        
        return {
            'total': len(scores),
            'average_overall': round(sum(overall_scores) / len(overall_scores), 2),
            'average_completeness': round(sum(completeness_scores) / len(completeness_scores), 2),
            'average_accuracy': round(sum(accuracy_scores) / len(accuracy_scores), 2),
            'average_relevance': round(sum(relevance_scores) / len(relevance_scores), 2),
            'average_timeliness': round(sum(timeliness_scores) / len(timeliness_scores), 2),
            'high_quality_count': sum(1 for s in overall_scores if s >= 0.8),
            'low_quality_count': sum(1 for s in overall_scores if s < 0.5)
        }


# 全局实例 - 阶段三：使用 lru_cache 替代手写单例
from functools import lru_cache


@lru_cache(maxsize=4)
def _get_quality_scorer_cached(kb_id: str):
    """缓存的知识质量评分器工厂（线程安全 + 自动单例）。

    kb_id 用作缓存键（必须是字符串），
    解决不同知识库需要不同评分器的问题。
    """
    return KnowledgeQualityScorer(knowledge_base=None)


def get_quality_scorer(knowledge_base=None) -> KnowledgeQualityScorer:
    """获取知识质量评分器实例（向后兼容 API）。

    实现方式：lru_cache 包装的工厂函数。
    - 无参数调用：返回默认评分器（kb_id="_default"）
    - 不同 knowledge_base 对象：按 id 区分返回不同评分器
    """
    if knowledge_base is None:
        return _get_quality_scorer_cached("_default")
    kb_id = str(getattr(knowledge_base, "id", None) or id(knowledge_base))
    return _get_quality_scorer_cached(kb_id)


def score_knowledge(knowledge_item) -> "QualityScore":
    """便捷函数：对单个知识条目评分。

    Returns:
        QualityScore 对象
    """
    return get_quality_scorer().score(knowledge_item)
