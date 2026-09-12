"""
RAG评估模块（统一版）

整合RAGAS评估框架、幻觉检测、多维度评估以及简化评估能力。

基于业界最佳实践:
- RAGAS: Retrieval Augmented Generation Assessment
- Faithfulness: 答案忠实度评估
- Answer Relevance: 答案相关性评估
- Context Relevance: 上下文相关性评估
- Hallucination Detection: 幻觉检测

合并自:
- src/common/rag_evaluator.py (RAGAS版，含幻觉检测)
- src/common/rag_evaluator_ext.py (扩展版，含SimpleRAGEvaluator和EvaluationMetric)
- src/rag/rag_evaluator.py (原简化版，已替换)
"""

import re
import logging
import time
from typing import List, Dict, Tuple, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from collections import Counter
from datetime import datetime

import jieba
import jieba.analyse
import numpy as np

logger = logging.getLogger(__name__)


# ==================== 评估指标枚举 ====================

class EvaluationMetric(Enum):
    """评估指标"""
    ANSWER_RELEVANCE = "answer_relevance"
    CONTEXT_UTILIZATION = "context_utilization"
    ANSWER_ACCURACY = "answer_accuracy"
    OVERALL = "overall"


# ==================== 数据类 ====================

@dataclass
class EvaluationResult:
    """评估结果"""
    query: str
    answer: str
    contexts: List[str]

    faithfulness: float = 0.0
    answer_relevance: float = 0.0
    context_relevance: float = 0.0
    context_recall: float = 0.0

    hallucination_score: float = 0.0
    hallucination_segments: List[str] = field(default_factory=list)

    overall_score: float = 0.0

    details: Dict = field(default_factory=dict)


# ==================== 子评估器 ====================

class FaithfulnessEvaluator:
    """
    忠实度评估器

    评估答案是否基于提供的上下文生成
    忠实度 = 答案中可从上下文推断的陈述比例
    """

    def evaluate(self, answer: str, contexts: List[str]) -> Tuple[float, Dict]:
        """
        评估答案忠实度

        Args:
            answer: 生成的答案
            contexts: 检索到的上下文列表

        Returns:
            (忠实度分数, 详细信息)
        """
        if not answer or not contexts:
            return 0.0, {"error": "答案或上下文为空"}

        combined_context = " ".join(contexts)

        answer_sentences = self._split_sentences(answer)

        supported_count = 0
        unsupported = []

        for sentence in answer_sentences:
            if self._is_supported(sentence, combined_context):
                supported_count += 1
            else:
                unsupported.append(sentence)

        faithfulness = supported_count / len(answer_sentences) if answer_sentences else 0.0

        return faithfulness, {
            "total_sentences": len(answer_sentences),
            "supported_sentences": supported_count,
            "unsupported_sentences": unsupported
        }

    def _split_sentences(self, text: str) -> List[str]:
        """分割句子"""
        sentences = re.split(r'[。！？\n]+', text)
        return [s.strip() for s in sentences if s.strip() and len(s.strip()) > 5]

    def _is_supported(self, sentence: str, context: str) -> bool:
        """
        判断句子是否被上下文支持

        Args:
            sentence: 待判断的句子
            context: 上下文

        Returns:
            是否被支持
        """
        sentence_words = set(jieba.cut(sentence.lower()))
        sentence_words = {w for w in sentence_words if len(w) >= 2}

        context_words = set(jieba.cut(context.lower()))
        context_words = {w for w in context_words if len(w) >= 2}

        if not sentence_words:
            return True

        overlap = sentence_words & context_words
        overlap_ratio = len(overlap) / len(sentence_words)

        return overlap_ratio >= 0.5


class AnswerRelevanceEvaluator:
    """
    答案相关性评估器

    评估答案与问题的相关程度
    """

    def evaluate(self, query: str, answer: str) -> Tuple[float, Dict]:
        """
        评估答案相关性

        Args:
            query: 用户问题
            answer: 生成的答案

        Returns:
            (相关性分数, 详细信息)
        """
        if not query or not answer:
            return 0.0, {"error": "问题或答案为空"}

        query_keywords = set(jieba.analyse.extract_tags(query, topK=10))
        answer_keywords = set(jieba.analyse.extract_tags(answer, topK=20))

        if not query_keywords:
            return 0.5, {"note": "问题无关键词"}

        overlap = query_keywords & answer_keywords
        relevance = len(overlap) / len(query_keywords)

        query_intent = self._detect_intent(query)
        answer_intent = self._detect_intent(answer)
        intent_match = 1.0 if query_intent == answer_intent else 0.5

        final_score = relevance * 0.7 + intent_match * 0.3

        return final_score, {
            "query_keywords": list(query_keywords),
            "answer_keywords": list(answer_keywords),
            "overlap": list(overlap),
            "query_intent": query_intent,
            "answer_intent": answer_intent
        }

    def _detect_intent(self, text: str) -> str:
        """检测文本意图"""
        if any(w in text for w in ["价格", "多少钱", "费用"]):
            return "price"
        if any(w in text for w in ["功能", "特点", "介绍"]):
            return "product"
        if any(w in text for w in ["怎么", "如何", "方法"]):
            return "howto"
        if any(w in text for w in ["购买", "订购", "开通"]):
            return "purchase"
        return "general"


class ContextRelevanceEvaluator:
    """
    上下文相关性评估器

    评估检索到的上下文与问题的相关程度
    """

    def evaluate(self, query: str, contexts: List[str]) -> Tuple[float, Dict]:
        """
        评估上下文相关性

        Args:
            query: 用户问题
            contexts: 检索到的上下文列表

        Returns:
            (相关性分数, 详细信息)
        """
        if not query or not contexts:
            return 0.0, {"error": "问题或上下文为空"}

        query_keywords = set(jieba.analyse.extract_tags(query, topK=10))

        if not query_keywords:
            return 0.5, {"note": "问题无关键词"}

        context_scores = []
        for i, context in enumerate(contexts):
            context_keywords = set(jieba.analyse.extract_tags(context, topK=20))
            overlap = query_keywords & context_keywords
            score = len(overlap) / len(query_keywords)
            context_scores.append({
                "index": i,
                "score": score,
                "overlap": list(overlap)
            })

        avg_score = sum(c["score"] for c in context_scores) / len(context_scores)

        return avg_score, {
            "context_scores": context_scores,
            "average_score": avg_score
        }


class HallucinationDetector:
    """
    幻觉检测器

    检测答案中的幻觉内容:
    1. 无法从上下文推断的陈述
    2. 与事实矛盾的内容
    3. 过度推断的内容
    """

    HALLUCINATION_INDICATORS = [
        r"绝对.{0,5}是",
        r"一定.{0,5}是",
        r"肯定.{0,5}是",
        r"百分之百",
        r"所有.{0,5}都",
        r"没有任何",
        r"完全不可能",
    ]

    FACTUAL_PATTERNS = {
        "price": r"\d+元|\d+块|免费",
        "date": r"\d{4}年|\d{1,2}月|\d{1,2}日",
        "number": r"\d+个|\d+次|\d+条",
    }

    def detect(self, answer: str, contexts: List[str]) -> Tuple[float, List[str], Dict]:
        """
        检测幻觉内容

        Args:
            answer: 生成的答案
            contexts: 检索到的上下文

        Returns:
            (幻觉分数, 幻觉片段列表, 详细信息)
        """
        if not answer:
            return 0.0, [], {"error": "答案为空"}

        combined_context = " ".join(contexts) if contexts else ""

        hallucination_segments = []

        for pattern in self.HALLUCINATION_INDICATORS:
            matches = re.findall(pattern, answer)
            for match in matches:
                if not self._is_supported_in_context(match, combined_context):
                    hallucination_segments.append(match)

        factual_claims = self._extract_factual_claims(answer)
        for claim in factual_claims:
            if not self._verify_claim(claim, combined_context):
                hallucination_segments.append(claim)

        answer_sentences = re.split(r'[。！？\n]+', answer)
        answer_sentences = [s.strip() for s in answer_sentences if s.strip()]

        hallucination_ratio = len(hallucination_segments) / max(len(answer_sentences), 1)
        hallucination_score = min(hallucination_ratio, 1.0)

        return hallucination_score, hallucination_segments, {
            "total_claims": len(answer_sentences),
            "hallucination_count": len(hallucination_segments),
            "factual_claims": factual_claims
        }

    def _is_supported_in_context(self, text: str, context: str) -> bool:
        """判断文本是否被上下文支持"""
        text_words = set(jieba.cut(text.lower()))
        text_words = {w for w in text_words if len(w) >= 2}

        context_words = set(jieba.cut(context.lower()))
        context_words = {w for w in context_words if len(w) >= 2}

        return bool(text_words & context_words)

    def _extract_factual_claims(self, text: str) -> List[str]:
        """提取事实性陈述"""
        claims = []

        for claim_type, pattern in self.FACTUAL_PATTERNS.items():
            matches = re.findall(f'.{{0,20}}{pattern}.{{0,20}}', text)
            claims.extend(matches)

        return claims

    def _verify_claim(self, claim: str, context: str) -> bool:
        """验证事实性陈述"""
        claim_words = set(jieba.cut(claim.lower()))
        claim_words = {w for w in claim_words if len(w) >= 2}

        context_words = set(jieba.cut(context.lower()))
        context_words = {w for w in context_words if len(w) >= 2}

        overlap = claim_words & context_words
        return len(overlap) >= len(claim_words) * 0.5


# ==================== 主评估器 ====================

class RAGASEvaluator:
    """
    RAGAS评估器

    整合所有评估维度:
    1. Faithfulness (忠实度)
    2. Answer Relevance (答案相关性)
    3. Context Relevance (上下文相关性)
    4. Context Recall (上下文召回率)
    5. Hallucination Detection (幻觉检测)
    """

    def __init__(self, llm_client: Any = None):
        """
        初始化RAGAS评估器

        Args:
            llm_client: LLM客户端（可选，用于更精确的评估）
        """
        self.llm_client = llm_client
        self.faithfulness_evaluator = FaithfulnessEvaluator()
        self.answer_relevance_evaluator = AnswerRelevanceEvaluator()
        self.context_relevance_evaluator = ContextRelevanceEvaluator()
        self.hallucination_detector = HallucinationDetector()
        self.metrics_collector = RAGMetricsCollector()

    def evaluate(
        self,
        query: str,
        answer: str,
        contexts: List[str],
        ground_truth: str = None
    ) -> EvaluationResult:
        """
        执行完整评估

        Args:
            query: 用户问题
            answer: 生成的答案
            contexts: 检索到的上下文
            ground_truth: 真实答案（可选）

        Returns:
            评估结果
        """
        faithfulness, faith_details = self.faithfulness_evaluator.evaluate(answer, contexts)

        answer_relevance, ans_details = self.answer_relevance_evaluator.evaluate(query, answer)

        context_relevance, ctx_details = self.context_relevance_evaluator.evaluate(query, contexts)

        context_recall = self._calculate_context_recall(contexts, ground_truth) if ground_truth else 0.5

        hallucination_score, hallucination_segments, hall_details = self.hallucination_detector.detect(answer, contexts)

        overall_score = self._calculate_overall_score(
            faithfulness=faithfulness,
            answer_relevance=answer_relevance,
            context_relevance=context_relevance,
            context_recall=context_recall,
            hallucination_score=hallucination_score
        )

        result = EvaluationResult(
            query=query,
            answer=answer,
            contexts=contexts,
            faithfulness=faithfulness,
            answer_relevance=answer_relevance,
            context_relevance=context_relevance,
            context_recall=context_recall,
            hallucination_score=hallucination_score,
            hallucination_segments=hallucination_segments,
            overall_score=overall_score,
            details={
                "faithfulness": faith_details,
                "answer_relevance": ans_details,
                "context_relevance": ctx_details,
                "hallucination": hall_details
            }
        )
        self.metrics_collector.record_evaluation(result)
        return result

    def evaluate_batch(
        self,
        test_cases: List[Dict]
    ) -> List[EvaluationResult]:
        """
        批量评估

        Args:
            test_cases: 测试用例列表
                [{'query': ..., 'answer': ..., 'contexts': [...], 'ground_truth': ...}, ...]

        Returns:
            评估结果列表
        """
        results = []
        for case in test_cases:
            result = self.evaluate(
                query=case.get('query', ''),
                answer=case.get('answer', ''),
                contexts=case.get('contexts', []),
                ground_truth=case.get('ground_truth')
            )
            results.append(result)

        return results

    def get_metrics_summary(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """返回指定时间范围内的评估指标汇总。"""
        return self.metrics_collector.get_statistics(start_date=start_date, end_date=end_date)

    def get_daily_stats(self, target_date: datetime) -> Dict[str, Any]:
        """返回指定日期的评估指标统计。"""
        return self.metrics_collector.get_daily_stats(target_date)

    def _calculate_context_recall(self, contexts: List[str], ground_truth: str) -> float:
        """计算上下文召回率"""
        if not ground_truth or not contexts:
            return 0.5

        truth_words = set(jieba.cut(ground_truth.lower()))
        truth_words = {w for w in truth_words if len(w) >= 2}

        combined_context = " ".join(contexts)
        context_words = set(jieba.cut(combined_context.lower()))
        context_words = {w for w in context_words if len(w) >= 2}

        overlap = truth_words & context_words
        return len(overlap) / len(truth_words) if truth_words else 0.0

    def _calculate_overall_score(
        self,
        faithfulness: float,
        answer_relevance: float,
        context_relevance: float,
        context_recall: float,
        hallucination_score: float
    ) -> float:
        """
        计算综合得分

        权重分配:
        - 忠实度: 30%
        - 答案相关性: 30%
        - 上下文相关性: 20%
        - 上下文召回率: 10%
        - 幻觉惩罚: 10%
        """
        weights = {
            "faithfulness": 0.30,
            "answer_relevance": 0.30,
            "context_relevance": 0.20,
            "context_recall": 0.10,
            "hallucination_penalty": 0.10
        }

        base_score = (
            weights["faithfulness"] * faithfulness +
            weights["answer_relevance"] * answer_relevance +
            weights["context_relevance"] * context_relevance +
            weights["context_recall"] * context_recall
        )

        hallucination_penalty = weights["hallucination_penalty"] * hallucination_score

        return max(0.0, base_score - hallucination_penalty)


# ==================== 指标收集器 ====================

class RAGMetricsCollector:
    """
    RAG指标收集器

    收集和统计RAG系统运行指标
    """

    def __init__(self):
        """初始化指标收集器"""
        self.metrics = {
            "queries": [],
            "latencies": [],
            "retrieval_scores": [],
            "generation_scores": [],
            "evaluations": []
        }

    def record_query(self, query: str, latency: float):
        """记录查询"""
        self.metrics["queries"].append(query)
        self.metrics["latencies"].append(latency)

    def record_retrieval_score(self, score: float):
        """记录检索分数"""
        self.metrics["retrieval_scores"].append(score)

    def record_evaluation(self, result: EvaluationResult):
        """记录评估结果"""
        self.metrics["evaluations"].append({
            "timestamp": time.time(),
            "result": result,
        })

    def _extract_evaluation_entries(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        start_ts = start_date.timestamp() if start_date else None
        end_ts = end_date.timestamp() if end_date else None

        for item in self.metrics["evaluations"]:
            if isinstance(item, dict) and "result" in item:
                timestamp = float(item.get("timestamp", time.time()))
                result = item["result"]
            else:
                timestamp = time.time()
                result = item

            if start_ts is not None and timestamp < start_ts:
                continue
            if end_ts is not None and timestamp > end_ts:
                continue

            entries.append({
                "timestamp": timestamp,
                "result": result,
            })

        return entries

    def get_statistics(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict:
        """获取统计信息"""
        entries = self._extract_evaluation_entries(start_date=start_date, end_date=end_date)
        if not entries:
            return {"message": "暂无评估数据"}

        evaluations = [entry["result"] for entry in entries]

        return {
            "total_evaluations": len(evaluations),
            "total_queries": len(self.metrics["queries"]),
            "average_latency": np.mean(self.metrics["latencies"]) if self.metrics["latencies"] else 0,
            "average_faithfulness": np.mean([e.faithfulness for e in evaluations]),
            "average_answer_relevance": np.mean([e.answer_relevance for e in evaluations]),
            "average_context_relevance": np.mean([e.context_relevance for e in evaluations]),
            "average_hallucination_score": np.mean([e.hallucination_score for e in evaluations]),
            "average_overall_score": np.mean([e.overall_score for e in evaluations]),
        }

    def get_daily_stats(self, target_date: datetime) -> Dict[str, Any]:
        """返回指定自然日的评估统计。"""
        day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        stats = self.get_statistics(start_date=day_start, end_date=day_end)
        stats["date"] = day_start.strftime("%Y-%m-%d")
        return stats


# ==================== 简化版评估器 ====================

class SimpleRAGEvaluator:
    """
    简化版RAG评估器

    适用于没有LLM服务时的快速评估
    """

    def evaluate(
        self,
        question: str,
        answer: str,
        context: List[str],
        retrieval_scores: List[float] = None
    ) -> Dict:
        """
        快速评估

        Returns:
            包含主要指标的字典
        """
        result = {
            "answer_relevance": self._quick_answer_relevance(question, answer),
            "context_utilization": self._quick_context_utilization(context, answer),
            "retrieval_quality": self._quick_retrieval_quality(retrieval_scores)
        }

        # 计算综合分数
        result["overall"] = (
            result["answer_relevance"] * 0.3 +
            result["context_utilization"] * 0.3 +
            result["retrieval_quality"] * 0.4
        )

        return result

    def _quick_answer_relevance(self, question: str, answer: str) -> float:
        """快速答案相关性评估"""
        score = 0.5

        # 关键词匹配
        q_keywords = set(self._extract_keywords(question))
        a_keywords = set(self._extract_keywords(answer))

        if q_keywords:
            match_ratio = len(q_keywords & a_keywords) / len(q_keywords)
            score = 0.5 + match_ratio * 0.4

        # 长度检查
        if len(answer) < 20:
            score -= 0.2
        elif 50 <= len(answer) <= 200:
            score += 0.1

        return max(0.0, min(1.0, score))

    def _quick_context_utilization(self, context: List[str], answer: str) -> float:
        """快速上下文利用率评估"""
        if not context:
            return 0.0

        score = 0.5

        # 检查上下文关键词在答案中的出现
        ctx_keywords = set()
        for ctx in context:
            ctx_keywords.update(self._extract_keywords(ctx))

        a_keywords = set(self._extract_keywords(answer))

        if ctx_keywords:
            overlap = len(ctx_keywords & a_keywords) / len(ctx_keywords)
            score = min(overlap * 1.2, 1.0)

        return score

    def _quick_retrieval_quality(self, retrieval_scores: List[float]) -> float:
        """快速检索质量评估"""
        if not retrieval_scores:
            return 0.0

        avg_score = sum(retrieval_scores) / len(retrieval_scores)

        # 将相似度分数转换为0-1的质量分数
        return min(avg_score, 1.0) if avg_score <= 1 else min(avg_score / 100, 1.0)

    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词"""
        if not text:
            return []

        try:
            import jieba as _jieba
            words = _jieba.cut(text)
            return [w for w in words if len(w) >= 2 and w.isalpha()]
        except ImportError:
            words = re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{2,}', text)
            return [w for w in words if len(w) >= 2]


# ==================== 工厂函数 ====================

def create_ragas_evaluator(llm_client: Any = None) -> RAGASEvaluator:
    """创建RAGAS评估器实例"""
    return RAGASEvaluator(llm_client)


def create_metrics_collector() -> RAGMetricsCollector:
    """创建指标收集器实例"""
    return RAGMetricsCollector()


def create_simple_evaluator() -> SimpleRAGEvaluator:
    """创建简化评估器"""
    return SimpleRAGEvaluator()
