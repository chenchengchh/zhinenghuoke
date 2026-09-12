"""
答案评估与自我修正系统

提供答案质量评估、自我修正、用户反馈收集功能

功能：
1. 答案置信度评估
2. 答案质量评分
3. 自我修正机制
4. 用户反馈收集
"""

import time
import logging
import threading
import re
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from collections import defaultdict

from loguru import logger


class AnswerQuality(Enum):
    """答案质量等级"""
    EXCELLENT = "excellent"
    GOOD = "good"
    ACCEPTABLE = "acceptable"
    POOR = "poor"
    INVALID = "invalid"


class FeedbackType(Enum):
    """反馈类型"""
    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"
    INCORRECT = "incorrect"
    INCOMPLETE = "incomplete"
    OFFENSIVE = "offensive"
    OTHER = "other"


@dataclass
class AnswerEvaluation:
    """答案评估结果"""
    query: str
    answer: str
    relevance: float = 0.0
    completeness: float = 0.0
    clarity: float = 0.0
    confidence: float = 0.0
    quality: AnswerQuality = AnswerQuality.ACCEPTABLE
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    need_clarification: bool = False
    need_human: bool = False
    
    def to_dict(self) -> Dict:
        return {
            "query": self.query,
            "relevance": round(self.relevance, 2),
            "completeness": round(self.completeness, 2),
            "clarity": round(self.clarity, 2),
            "confidence": round(self.confidence, 2),
            "quality": self.quality.value,
            "issues": self.issues,
            "suggestions": self.suggestions,
            "need_clarification": self.need_clarification,
            "need_human": self.need_human
        }


@dataclass
class UserFeedback:
    """用户反馈"""
    feedback_id: str
    session_id: str
    query: str
    answer: str
    feedback_type: FeedbackType
    rating: int = 0
    comment: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict:
        return {
            "feedback_id": self.feedback_id,
            "session_id": self.session_id,
            "query": self.query,
            "answer": self.answer[:100] + "..." if len(self.answer) > 100 else self.answer,
            "feedback_type": self.feedback_type.value,
            "rating": self.rating,
            "comment": self.comment,
            "timestamp": self.timestamp.isoformat()
        }


class AnswerEvaluator:
    """
    答案评估器
    
    评估答案的多个维度：
    1. 相关性 - 答案是否与问题相关
    2. 完整性 - 答案是否完整回答了问题
    3. 清晰度 - 答案是否清晰易懂
    4. 置信度 - 对答案的信心程度
    """
    
    MIN_ANSWER_LENGTH = 10
    MAX_ANSWER_LENGTH = 2000
    
    IRRELEVANT_PATTERNS = [
        r"我不知道",
        r"暂无.*信息",
        r"没有找到",
        r"无法回答",
        r"请咨询.*客服"
    ]
    
    INCOMPLETE_PATTERNS = [
        r"\.\.\.",
        r"待补充",
        r"暂未提供",
        r"需要进一步"
    ]
    
    UNCLEAR_PATTERNS = [
        r"可能",
        r"也许",
        r"大概",
        r"应该"
    ]
    
    def __init__(self):
        pass
    
    def evaluate(
        self,
        query: str,
        answer: str,
        sources: List[Any] = None,
        context: Dict = None
    ) -> AnswerEvaluation:
        """
        评估答案质量
        
        Args:
            query: 用户问题
            answer: 生成的答案
            sources: 检索来源
            context: 上下文信息
            
        Returns:
            AnswerEvaluation: 评估结果
        """
        evaluation = AnswerEvaluation(query=query, answer=answer)
        
        evaluation.relevance = self._evaluate_relevance(query, answer, sources)
        evaluation.completeness = self._evaluate_completeness(query, answer)
        evaluation.clarity = self._evaluate_clarity(answer)
        evaluation.confidence = self._calculate_confidence(evaluation, sources)
        
        evaluation.quality = self._determine_quality(evaluation.confidence)
        
        self._identify_issues(evaluation)
        self._generate_suggestions(evaluation)
        
        return evaluation
    
    def _evaluate_relevance(
        self,
        query: str,
        answer: str,
        sources: List[Any]
    ) -> float:
        """评估相关性"""
        score = 50.0
        
        if not answer:
            return 0.0
        
        query_lower = query.lower()
        answer_lower = answer.lower()
        
        query_words = set(re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]+', query_lower))
        answer_words = set(re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]+', answer_lower))
        
        if query_words:
            overlap = len(query_words & answer_words) / len(query_words)
            score += overlap * 30
        
        for pattern in self.IRRELEVANT_PATTERNS:
            if re.search(pattern, answer):
                score -= 20
        
        if sources and len(sources) > 0:
            score += 10
        
        return max(0, min(100, score))
    
    def _evaluate_completeness(self, query: str, answer: str) -> float:
        """评估完整性"""
        score = 70.0
        
        if not answer:
            return 0.0
        
        if len(answer) < self.MIN_ANSWER_LENGTH:
            score -= 30
        elif len(answer) > self.MAX_ANSWER_LENGTH:
            score -= 10
        
        for pattern in self.INCOMPLETE_PATTERNS:
            if re.search(pattern, answer):
                score -= 15
        
        if "？" in query or "?" in query:
            if "。" in answer or "！" in answer:
                score += 10
        
        return max(0, min(100, score))
    
    def _evaluate_clarity(self, answer: str) -> float:
        """评估清晰度"""
        score = 70.0
        
        if not answer:
            return 0.0
        
        for pattern in self.UNCLEAR_PATTERNS:
            matches = re.findall(pattern, answer)
            score -= len(matches) * 5
        
        sentences = re.split(r'[。！？]', answer)
        if len(sentences) > 5:
            score -= 10
        
        if re.search(r'[一二三四五六七八九十]、', answer):
            score += 10
        
        return max(0, min(100, score))
    
    def _calculate_confidence(
        self,
        evaluation: AnswerEvaluation,
        sources: List[Any]
    ) -> float:
        """计算置信度"""
        confidence = (
            evaluation.relevance * 0.4 +
            evaluation.completeness * 0.3 +
            evaluation.clarity * 0.3
        )
        
        if sources:
            source_confidence = min(len(sources) * 5, 20)
            confidence += source_confidence
        
        return max(0, min(100, confidence))
    
    def _determine_quality(self, confidence: float) -> AnswerQuality:
        """确定质量等级"""
        if confidence >= 85:
            return AnswerQuality.EXCELLENT
        elif confidence >= 70:
            return AnswerQuality.GOOD
        elif confidence >= 50:
            return AnswerQuality.ACCEPTABLE
        elif confidence >= 30:
            return AnswerQuality.POOR
        else:
            return AnswerQuality.INVALID
    
    def _identify_issues(self, evaluation: AnswerEvaluation):
        """识别问题"""
        if evaluation.relevance < 50:
            evaluation.issues.append("答案与问题相关性较低")
        
        if evaluation.completeness < 50:
            evaluation.issues.append("答案不够完整")
        
        if evaluation.clarity < 50:
            evaluation.issues.append("答案不够清晰")
        
        if len(evaluation.answer) < self.MIN_ANSWER_LENGTH:
            evaluation.issues.append("答案过短")
        
        for pattern in self.IRRELEVANT_PATTERNS:
            if re.search(pattern, evaluation.answer):
                evaluation.issues.append("答案包含无效信息")
                break
        
        if evaluation.confidence < 50:
            evaluation.need_clarification = True
        
        if evaluation.confidence < 30:
            evaluation.need_human = True
    
    def _generate_suggestions(self, evaluation: AnswerEvaluation):
        """生成改进建议"""
        if evaluation.relevance < 70:
            evaluation.suggestions.append("尝试重新检索更相关的内容")
        
        if evaluation.completeness < 70:
            evaluation.suggestions.append("补充更多细节信息")
        
        if evaluation.clarity < 70:
            evaluation.suggestions.append("使用更清晰的表达方式")


class SelfCorrector:
    """
    自我修正器
    
    当答案质量不达标时进行自我修正
    """
    
    MAX_CORRECTION_ATTEMPTS = 3
    CONFIDENCE_THRESHOLD = 50.0
    
    def __init__(self, retriever=None, evaluator=None):
        self.retriever = retriever
        self.evaluator = evaluator or AnswerEvaluator()
        self._correction_history: Dict[str, int] = {}
    
    def correct(
        self,
        query: str,
        answer: str,
        sources: List[Any] = None,
        context: Dict = None
    ) -> Tuple[str, AnswerEvaluation, bool]:
        """
        自我修正
        
        Args:
            query: 用户问题
            answer: 原始答案
            sources: 检索来源
            context: 上下文
            
        Returns:
            (修正后的答案, 评估结果, 是否修正成功)
        """
        evaluation = self.evaluator.evaluate(query, answer, sources, context)
        
        if evaluation.confidence >= self.CONFIDENCE_THRESHOLD:
            return answer, evaluation, True
        
        query_hash = str(hash(query))
        attempts = self._correction_history.get(query_hash, 0)
        
        if attempts >= self.MAX_CORRECTION_ATTEMPTS:
            evaluation.need_human = True
            return self._generate_fallback_answer(query, evaluation), evaluation, False
        
        self._correction_history[query_hash] = attempts + 1
        
        corrected_answer = self._attempt_correction(query, answer, evaluation, sources)
        
        if corrected_answer != answer:
            new_evaluation = self.evaluator.evaluate(query, corrected_answer, sources, context)
            
            if new_evaluation.confidence > evaluation.confidence:
                return corrected_answer, new_evaluation, True
        
        return answer, evaluation, False
    
    def _attempt_correction(
        self,
        query: str,
        answer: str,
        evaluation: AnswerEvaluation,
        sources: List[Any]
    ) -> str:
        """尝试修正答案"""
        corrected = answer
        
        if "答案不够完整" in evaluation.issues:
            corrected = self._expand_answer(answer, sources)
        
        if "答案不够清晰" in evaluation.issues:
            corrected = self._clarify_answer(answer)
        
        if "答案与问题相关性较低" in evaluation.issues:
            corrected = self._improve_relevance(query, answer, sources)
        
        return corrected
    
    def _expand_answer(self, answer: str, sources: List[Any]) -> str:
        """扩展答案"""
        if not sources:
            return answer
        
        additional_info = []
        for source in sources[1:3]:
            if hasattr(source, "answer"):
                additional_info.append(source.answer[:100])
            elif isinstance(source, dict):
                content = source.get("answer", source.get("content", ""))
                if content:
                    additional_info.append(content[:100])
        
        if additional_info:
            return answer + "\n\n补充信息：" + " ".join(additional_info)
        
        return answer
    
    def _clarify_answer(self, answer: str) -> str:
        """澄清答案"""
        unclear_words = ["可能", "也许", "大概", "应该"]
        clarified = answer
        
        for word in unclear_words:
            clarified = clarified.replace(word, "")
        
        clarified = re.sub(r'\s+', ' ', clarified).strip()
        
        return clarified
    
    def _improve_relevance(
        self,
        query: str,
        answer: str,
        sources: List[Any]
    ) -> str:
        """提高相关性"""
        if not sources:
            return answer
        
        best_source = sources[0] if sources else None
        
        if best_source:
            if hasattr(best_source, "answer"):
                return best_source.answer
            elif isinstance(best_source, dict):
                return best_source.get("answer", answer)
        
        return answer
    
    def _generate_fallback_answer(
        self,
        query: str,
        evaluation: AnswerEvaluation
    ) -> str:
        """生成后备答案"""
        return (
            f"抱歉，我无法准确回答您的问题\"{query[:30]}...\"。\n"
            f"建议您：\n"
            f"1. 换一种方式描述您的问题\n"
            f"2. 联系人工客服获取帮助\n"
            f"3. 查看相关帮助文档"
        )


class FeedbackCollector:
    """
    用户反馈收集器
    
    收集和管理用户反馈
    """
    
    def __init__(self):
        self._feedbacks: Dict[str, UserFeedback] = {}
        self._session_feedbacks: Dict[str, List[str]] = defaultdict(list)
        self._lock = threading.Lock()
    
    def collect(
        self,
        session_id: str,
        query: str,
        answer: str,
        feedback_type: FeedbackType,
        rating: int = 0,
        comment: str = ""
    ) -> UserFeedback:
        """
        收集用户反馈
        
        Args:
            session_id: 会话ID
            query: 用户问题
            answer: 生成的答案
            feedback_type: 反馈类型
            rating: 评分 (1-5)
            comment: 评论
            
        Returns:
            UserFeedback: 反馈记录
        """
        import uuid
        feedback_id = str(uuid.uuid4())[:8]
        
        feedback = UserFeedback(
            feedback_id=feedback_id,
            session_id=session_id,
            query=query,
            answer=answer,
            feedback_type=feedback_type,
            rating=max(1, min(5, rating)),
            comment=comment
        )
        
        with self._lock:
            self._feedbacks[feedback_id] = feedback
            self._session_feedbacks[session_id].append(feedback_id)
        
        return feedback
    
    def get_feedback(self, feedback_id: str) -> Optional[UserFeedback]:
        """获取反馈"""
        return self._feedbacks.get(feedback_id)
    
    def get_session_feedbacks(self, session_id: str) -> List[UserFeedback]:
        """获取会话的所有反馈"""
        feedback_ids = self._session_feedbacks.get(session_id, [])
        return [self._feedbacks[fid] for fid in feedback_ids if fid in self._feedbacks]
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取反馈统计"""
        total = len(self._feedbacks)
        
        if total == 0:
            return {"total": 0}
        
        type_counts = defaultdict(int)
        rating_sum = 0
        rating_count = 0
        
        for feedback in self._feedbacks.values():
            type_counts[feedback.feedback_type.value] += 1
            if feedback.rating > 0:
                rating_sum += feedback.rating
                rating_count += 1
        
        avg_rating = rating_sum / rating_count if rating_count > 0 else 0
        
        helpful_rate = (
            type_counts[FeedbackType.HELPFUL.value] / total * 100
            if total > 0 else 0
        )
        
        return {
            "total": total,
            "by_type": dict(type_counts),
            "average_rating": round(avg_rating, 2),
            "helpful_rate": round(helpful_rate, 2)
        }


class AnswerQualityManager:
    """
    答案质量管理器
    
    整合评估、修正、反馈功能
    """
    
    def __init__(self, retriever=None):
        self.retriever = retriever
        self.evaluator = AnswerEvaluator()
        self.corrector = SelfCorrector(retriever, self.evaluator)
        self.feedback_collector = FeedbackCollector()
    
    def process_answer(
        self,
        query: str,
        answer: str,
        sources: List[Any] = None,
        session_id: str = "",
        auto_correct: bool = True
    ) -> Tuple[str, AnswerEvaluation]:
        """
        处理答案
        
        Args:
            query: 用户问题
            answer: 生成的答案
            sources: 检索来源
            session_id: 会话ID
            auto_correct: 是否自动修正
            
        Returns:
            (处理后的答案, 评估结果)
        """
        if auto_correct:
            final_answer, evaluation, corrected = self.corrector.correct(
                query, answer, sources
            )
        else:
            evaluation = self.evaluator.evaluate(query, answer, sources)
            final_answer = answer
        
        return final_answer, evaluation
    
    def collect_feedback(
        self,
        session_id: str,
        query: str,
        answer: str,
        feedback_type: FeedbackType,
        rating: int = 0,
        comment: str = ""
    ) -> UserFeedback:
        """收集反馈"""
        return self.feedback_collector.collect(
            session_id=session_id,
            query=query,
            answer=answer,
            feedback_type=feedback_type,
            rating=rating,
            comment=comment
        )
    
    def get_quality_report(self) -> Dict[str, Any]:
        """获取质量报告"""
        feedback_stats = self.feedback_collector.get_statistics()
        
        return {
            "feedback_statistics": feedback_stats,
            "quality_metrics": {
                "average_confidence": 0.0,
                "correction_rate": 0.0,
                "human_escalation_rate": 0.0
            }
        }


_answer_quality_manager: Optional[AnswerQualityManager] = None


def get_answer_quality_manager(retriever=None) -> AnswerQualityManager:
    """获取答案质量管理器实例"""
    global _answer_quality_manager
    
    if _answer_quality_manager is None:
        _answer_quality_manager = AnswerQualityManager(retriever)
    
    return _answer_quality_manager
