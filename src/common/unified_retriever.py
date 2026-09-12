# NOTE: 本模块是检索层的统一入口，底层实现已迁移到 src.rag.unified_pipeline。
# 新代码建议直接使用 src.rag.unified_pipeline.UnifiedRetrievalPipeline。

"""
统一检索器 (UnifiedRetriever)

整合所有检索策略的统一入口:
- HybridRetriever: BM25+向量混合检索
- RAGRetriever: 完整RAG流程(查询改写/多查询/HyDE/重排序)
- EnhancedRAGRetriever: 意图分类+策略选择
- AdaptiveRetriever: 自适应策略路由
- MultiHopRetriever: 多跳推理检索 (已移除，逻辑内联为 _multi_hop_search)

设计原则:
1. 策略模式: 不同检索策略作为可插拔组件
2. 自动降级: 高级特性不可用时自动降级
3. 向后兼容: 保留原有工厂函数
"""

import re
import logging
import threading
from typing import List, Dict, Tuple, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from collections import Counter
import jieba
import jieba.analyse
import numpy as np

from src.common.query_preprocessor import (
    AdvancedQueryPreprocessor,
    ProcessedQuery,
    QueryType,
    create_query_preprocessor,
)
from src.common.types.rag import RetrievalResult, RAGResult, RAGStrategy

logger = logging.getLogger(__name__)


# ============== 策略枚举 ==============

class RetrievalStrategy(Enum):
    """检索策略"""
    SIMPLE = "simple"              # 简单关键词检索
    HYBRID = "hybrid"              # 混合检索(BM25+向量)
    SEMANTIC_FIRST = "semantic"     # 语义优先
    EXACT_FIRST = "exact"          # 精确匹配优先
    PROCEDURE_FIRST = "procedure"  # 流程步骤优先
    PRIORITY_FIRST = "priority"    # 优先级优先(投诉等)
    MULTI_QUERY = "multi_query"    # 多查询改写
    MULTI_HOP = "multi_hop"        # 多跳推理
    ADAPTIVE = "adaptive"          # 自适应选择


class QueryComplexity(Enum):
    """查询复杂度"""
    SIMPLE = 1       # 简单查询
    MEDIUM = 2       # 中等查询
    COMPLEX = 3      # 复杂查询
    MULTI_HOP = 4    # 多跳查询


# ============== 配置数据类 ==============

@dataclass
class RetrievalConfig:
    """统一检索配置"""
    strategy: RetrievalStrategy = RetrievalStrategy.ADAPTIVE
    top_k: int = 5
    use_rerank: bool = True
    use_multi_query: bool = True
    use_hyde: bool = True
    enable_adaptive: bool = True
    max_hops: int = 3
    enterprise_id: Optional[str] = None
    enterprise_ids: Optional[List[str]] = None


@dataclass
class UnifiedResult:
    """统一检索结果"""
    results: List[RetrievalResult]
    strategy_used: RetrievalStrategy
    query_complexity: QueryComplexity
    intent_info: Dict[str, Any] = field(default_factory=dict)
    execution_time_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============== 查询改写器 (保留原有功能) ==============

class QueryRewriter:
    """
    查询改写器 (从原rag_retriever.py迁移)
    """

    SYNONYM_MAP = {
        "多少钱": "价格",
        "咋卖": "价格",
        "咋样": "怎么样",
        "好不好": "怎么样",
        "能不": "是否",
        "有没有": "是否有",
        "贵": "价格",
        "便宜": "价格",
        "怎么买": "购买",
        "如何购买": "购买",
        "怎么用": "使用",
    }

    FILLER_WORDS = ["那个", "嗯", "啊", "呢", "嘛", "呀", "哈", "咯", "哎", "吧"]

    def rewrite(self, query: str) -> Tuple[str, List[str]]:
        rewritten = query
        for colloquial, formal in self.SYNONYM_MAP.items():
            if colloquial in rewritten:
                rewritten = rewritten.replace(colloquial, formal)
        for word in self.FILLER_WORDS:
            rewritten = rewritten.replace(word, "")
        keywords = list(jieba.cut(rewritten))
        keywords = [k for k in keywords if len(k) >= 2]
        return rewritten, keywords

    def expand_query(self, query: str) -> List[str]:
        expanded = [query]
        for colloquial, formal in self.SYNONYM_MAP.items():
            if colloquial in query:
                expanded.append(query.replace(colloquial, formal))
        return expanded


# ============== 重排序器 (保留原有功能) ==============

# 修复 A3：_simple_rerank 退化路径使用的停用词表，
# 过滤 jieba.cut 产生的高频虚词，避免关键词匹配噪声主导重排序。
_SIMPLE_RERANK_STOP_WORDS = {
    "您好", "你好", "请问", "一下", "可以", "能否", "是否", "怎么", "怎样",
    "如何", "多少", "多久", "哪里", "哪个", "哪些", "什么", "一般", "通常",
    "提前", "当天", "时间", "时候", "现在", "今天", "明天", "我们", "你们",
    "他们", "的话", "是的", "不是", "没有", "这个", "那个", "就是", "还是",
    "或者", "但是", "因为", "所以", "如果", "虽然", "不过", "然后", "其实",
    "觉得", "感觉", "知道", "明白", "了解", "需要", "想要", "谢谢", "感谢",
}


class Reranker:
    """
    重排序器 (从原rag_retriever.py迁移)
    使用BGE中文重排模型或简单关键词匹配
    """

    def __init__(self, model_path: str = ""):
        # P0-2: 从硬编码 MODEL_PATH 迁移到 settings.py 配置化
        from src.config.settings import RERANKER_MODEL_PATH, RERANKER_ENABLED, RERANKER_MAX_LENGTH
        self.model_path = model_path or RERANKER_MODEL_PATH
        self.enabled = RERANKER_ENABLED
        self.max_length = RERANKER_MAX_LENGTH
        self.reranker = None
        if self.enabled:
            self._init_reranker()

    def _init_reranker(self):
        try:
            from sentence_transformers import CrossEncoder
            import os
            if os.path.exists(self.model_path):
                self.reranker = CrossEncoder(self.model_path, max_length=self.max_length)
                logger.info(f"Reranker模型加载成功: {self.model_path}")
            else:
                logger.warning(f"模型路径不存在: {self.model_path}，降级到简单重排序")
        except ImportError:
            logger.warning("sentence-transformers未安装，使用简单重排序")
        except Exception as e:
            logger.warning(f"Reranker初始化失败: {e}")

    def rerank(self, query: str, results: List[RetrievalResult], top_k: int = 5) -> List[RetrievalResult]:
        if not results:
            return []
        if self.reranker:
            try:
                pairs = [(query, r.content) for r in results]
                scores = self.reranker.predict(pairs)
                scored_results = list(zip(results, scores))
                scored_results.sort(key=lambda x: x[1], reverse=True)
                return [r for r, s in scored_results[:top_k]]
            except Exception as e:
                logger.warning(f"重排序失败: {e}")
        return self._simple_rerank(query, results, top_k)

    def _simple_rerank(self, query: str, results: List[RetrievalResult], top_k: int) -> List[RetrievalResult]:
        # 修复 A3：jieba.cut 产生大量单字 token 引入匹配噪声，
        # 过滤单字与停用词，并按词长加权（长词更具区分度）。
        query_terms = {
            t.strip().lower()
            for t in jieba.cut(query.lower())
            if len(t.strip()) >= 2 and t.strip().lower() not in _SIMPLE_RERANK_STOP_WORDS
        }
        if not query_terms:
            # 全被过滤时退回原查询整体匹配，避免空集导致顺序无变化
            query_terms = {query.lower()}
        scored = []
        for result in results:
            content_lower = result.content.lower()
            # 按词长加权：长词匹配贡献更高，避免短词噪声主导排序
            match_weight = sum(len(t) for t in query_terms if t in content_lower)
            scored.append((result, match_weight))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [r for r, _ in scored[:top_k]]


_reranker_instance = None
_reranker_lock = threading.Lock()


def get_reranker(reset: bool = False, model_path: str = "") -> Reranker:
    global _reranker_instance
    if _reranker_instance is not None and not reset:
        return _reranker_instance
    with _reranker_lock:
        if _reranker_instance is not None and not reset:
            return _reranker_instance
        _reranker_instance = Reranker(model_path=model_path)
    return _reranker_instance


# ============== 意图分类器 (从原EnhancedRAGRetriever迁移) ==============

class IntentClassifier:
    """
    检索意图分类器 (从原_RAGIntentClassifier迁移)
    根据用户查询识别检索意图类型，选择最佳检索策略
    """

    INTENT_PATTERNS = {
        "price": {
            "keywords": ["多少钱", "价格", "费用", "收费", "贵", "便宜", "成本", "付费", "免费", "优惠", "折扣"],
            "category": "price",
            "strategy": RetrievalStrategy.EXACT_FIRST,
        },
        "product": {
            "keywords": ["功能", "能做什么", "特性", "有什么用", "作用", "产品", "系统", "平台"],
            "category": "product",
            "strategy": RetrievalStrategy.SEMANTIC_FIRST,
        },
        "faq": {
            "keywords": ["怎么用", "如何", "操作", "使用", "步骤", "教程", "指南", "方法"],
            "category": "faq",
            "strategy": RetrievalStrategy.PROCEDURE_FIRST,
        },
        "complaint": {
            "keywords": ["投诉", "退款", "问题", "故障", "无法", "错误", "失败", "不工作"],
            "category": "complaint",
            "strategy": RetrievalStrategy.PRIORITY_FIRST,
        },
        "policy": {
            "keywords": ["政策", "规定", "规则", "条款", "协议", "合同"],
            "category": "policy",
            "strategy": RetrievalStrategy.EXACT_FIRST,
        },
        "service": {
            "keywords": ["客服", "联系", "服务", "支持", "帮助", "咨询", "售后"],
            "category": "service",
            "strategy": RetrievalStrategy.SEMANTIC_FIRST,
        },
        "general": {
            "keywords": [],
            "category": "general",
            "strategy": RetrievalStrategy.HYBRID,
        },
    }

    def __init__(self):
        self.intent_weights = {}
        self._build_weights()

    def _build_weights(self):
        for intent, config in self.INTENT_PATTERNS.items():
            self.intent_weights[intent] = {}
            for keyword in config["keywords"]:
                self.intent_weights[intent][keyword] = 1.0

    def classify(self, query: str) -> Dict[str, Any]:
        query_lower = query.lower()
        scores = {}
        matched_keywords = {}

        for intent, keywords in self.intent_weights.items():
            score = 0
            matched = []
            for keyword, weight in keywords.items():
                if keyword in query_lower:
                    score += weight
                    matched.append(keyword)
            if score > 0:
                scores[intent] = score
                matched_keywords[intent] = matched

        if not scores:
            return {
                "intent": "general",
                "confidence": 0.0,
                "category": "general",
                "strategy": RetrievalStrategy.HYBRID,
                "matched_keywords": [],
            }

        sorted_intents = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_intent = sorted_intents[0][0]
        confidence = scores[top_intent] / (scores[top_intent] + 1)

        config = self.INTENT_PATTERNS[top_intent]
        return {
            "intent": top_intent,
            "confidence": confidence,
            "category": config["category"],
            "strategy": config["strategy"],
            "matched_keywords": matched_keywords.get(top_intent, []),
        }


# ============== 查询复杂度分析器 (从原AdaptiveRetriever迁移) ==============

class QueryComplexityAnalyzer:
    """
    查询复杂度分析器 (从原AdaptiveRetriever迁移)
    分析查询复杂度，决定使用何种检索策略
    """

    COMPLEX_INDICATORS = {
        QueryComplexity.SIMPLE: [
            r"^.{1,20}$",
            r"是什么",
            r"有没有",
            r"多少钱"
        ],
        QueryComplexity.MEDIUM: [
            r".{20,50}",
            r"怎么.*",
            r"如何.*",
            r"比较.*",
            r"区别"
        ],
        QueryComplexity.COMPLEX: [
            r".{50,}",
            r"然后.*并且",
            r"首先.*然后.*最后",
            r"为什么.*而且",
        ],
        QueryComplexity.MULTI_HOP: [
            r".*首先.*然后.*",
            r".*之后.*再.*",
        ],
    }

    def analyze(self, query: str) -> QueryComplexity:
        for complexity, patterns in reversed(list(self.COMPLEX_INDICATORS.items())):
            for pattern in patterns:
                if re.search(pattern, query):
                    return complexity
        return QueryComplexity.SIMPLE


# ============== 核心统一检索器 ==============

class UnifiedRetriever:
    """
    统一检索器

    整合所有检索策略的统一入口:
    - 自动策略选择 (基于意图+复杂度)
    - 混合检索 (BM25 + 向量语义)
    - 多查询改写
    - HyDE假设文档
    - 重排序
    - 多跳推理
    - 企业隔离

    用法示例:
        retriever = UnifiedRetriever(knowledge_items, vector_store)
        result = retriever.retrieve("产品价格是多少?")
        print(result.results)
        print(result.strategy_used)
    """

    def __init__(
        self,
        knowledge_items: List,
        vector_store: Any = None,
        llm_client: Any = None,
        config: Optional[RetrievalConfig] = None,
    ):
        self.knowledge_items = knowledge_items
        self.vector_store = vector_store
        self.llm_client = llm_client
        self.config = config or RetrievalConfig()

        # 初始化子组件
        self.query_rewriter = QueryRewriter()
        self.query_preprocessor = create_query_preprocessor(llm_client)
        self.intent_classifier = IntentClassifier()
        self.complexity_analyzer = QueryComplexityAnalyzer()
        self.reranker = None

        # 企业ID列表
        self.enterprise_ids = self.config.enterprise_ids or ["default", "main", "enterprise"]

        # 文档索引缓存
        self.doc_texts = {}
        self._init_index()

    def _init_index(self):
        """初始化文档索引"""
        self.doc_texts = {}
        for item in self.knowledge_items:
            self.doc_texts[item.id] = f"{item.question} {item.answer}"

    @staticmethod
    def _normalize_enterprise_ids(
        enterprise_id: Optional[str] = None,
        enterprise_ids: Optional[List[str]] = None,
    ) -> List[str]:
        ordered: List[str] = []
        raw_values = list(enterprise_ids or [])
        if enterprise_id:
            raw_values.insert(0, enterprise_id)
        for value in raw_values:
            normalized = str(value or "").strip()
            if normalized and normalized not in ordered:
                ordered.append(normalized)
        return ordered

    @staticmethod
    def _item_matches_enterprise(item: Any, enterprise_ids: List[str]) -> bool:
        if not enterprise_ids:
            return True
        item_enterprise_id = str(getattr(item, "enterprise_id", "") or "").strip()
        if not item_enterprise_id:
            return any(eid in {"default", "main", "enterprise"} for eid in enterprise_ids)
        return item_enterprise_id in enterprise_ids

    def refresh_index(self, knowledge_items: List = None):
        """刷新索引"""
        if knowledge_items is not None:
            self.knowledge_items = knowledge_items
        self._init_index()

    # ============== 主入口方法 ==============

    def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        strategy: Optional[RetrievalStrategy] = None,
        **kwargs,
    ) -> UnifiedResult:
        """
        执行统一检索

        Args:
            query: 用户查询
            top_k: 返回数量 (默认使用配置值)
            strategy: 强制指定策略 (None则自动选择)

        Returns:
            UnifiedResult: 包含结果、使用的策略等信息
        """
        import time
        start_time = time.time()

        top_k = top_k or self.config.top_k
        strategy = strategy or self.config.strategy

        # 1. 分析查询
        intent_info = self.intent_classifier.classify(query)
        complexity = self.complexity_analyzer.analyze(query)

        # 2. 确定最终策略
        if strategy == RetrievalStrategy.ADAPTIVE:
            final_strategy = self._select_strategy(intent_info, complexity)
        elif strategy == RetrievalStrategy.ADAPTIVE and intent_info.get("strategy"):
            final_strategy = intent_info["strategy"]
        else:
            final_strategy = strategy

        # 3. 根据策略执行检索
        allowed_enterprise_ids = self._normalize_enterprise_ids(
            self.config.enterprise_id,
            self.config.enterprise_ids,
        )

        if final_strategy == RetrievalStrategy.SIMPLE:
            results = self._simple_search(query, top_k, allowed_enterprise_ids)
        elif final_strategy == RetrievalStrategy.EXACT_FIRST:
            results = self._exact_first_search(query, top_k, intent_info.get("category"), allowed_enterprise_ids)
        elif final_strategy == RetrievalStrategy.SEMANTIC_FIRST:
            results = self._semantic_first_search(query, top_k, allowed_enterprise_ids)
        elif final_strategy == RetrievalStrategy.PROCEDURE_FIRST:
            results = self._procedure_first_search(query, top_k, allowed_enterprise_ids)
        elif final_strategy == RetrievalStrategy.PRIORITY_FIRST:
            results = self._priority_first_search(query, top_k, allowed_enterprise_ids)
        elif final_strategy == RetrievalStrategy.MULTI_HOP:
            results = self._multi_hop_search(query, top_k, allowed_enterprise_ids)
        else:
            # 默认使用混合检索
            results = self._hybrid_search(query, top_k, allowed_enterprise_ids)

        # 4. 重排序
        if self.config.use_rerank and len(results) > top_k:
            if self.reranker is None:
                self.reranker = get_reranker()
            results = self.reranker.rerank(query, results, top_k)
        else:
            results = results[:top_k]

        execution_time = (time.time() - start_time) * 1000

        return UnifiedResult(
            results=results,
            strategy_used=final_strategy,
            query_complexity=complexity,
            intent_info=intent_info,
            execution_time_ms=execution_time,
        )

    def _select_strategy(
        self,
        intent_info: Dict[str, Any],
        complexity: QueryComplexity,
    ) -> RetrievalStrategy:
        """自动选择最佳策略"""
        # 优先使用意图推荐的策略
        intent_strategy = intent_info.get("strategy")
        if intent_strategy and intent_strategy != RetrievalStrategy.HYBRID:
            return intent_strategy

        # 根据复杂度选择
        if complexity == QueryComplexity.MULTI_HOP:
            return RetrievalStrategy.MULTI_HOP
        elif complexity == QueryComplexity.COMPLEX:
            return RetrievalStrategy.MULTI_QUERY
        else:
            return RetrievalStrategy.HYBRID

    # ============== 具体检索策略实现 ==============

    def _hybrid_search(
        self,
        query: str,
        top_k: int,
        enterprise_ids: Optional[List[str]],
    ) -> List[RetrievalResult]:
        """混合检索: BM25 + 向量语义"""
        keyword_results = self._keyword_search(query, top_k * 2, enterprise_ids)
        vector_results = self._vector_search(query, top_k * 2, enterprise_ids)
        return self._rrf_fusion(keyword_results, vector_results, top_k)

    def _keyword_search(
        self,
        query: str,
        top_k: int,
        enterprise_ids: Optional[List[str]],
    ) -> List[RetrievalResult]:
        """关键词检索"""
        query_terms = list(jieba.cut(query.lower()))
        results = []

        for item in self.knowledge_items:
            if not self._item_matches_enterprise(item, enterprise_ids or []):
                continue
            doc_text = f"{item.question} {item.answer}"
            score = self._calculate_keyword_score(query_terms, doc_text, item)

            result = RetrievalResult(
                doc_id=item.id,
                content=doc_text,
                score=score,
                source="keyword",
                metadata={
                    "question": item.question,
                    "answer": item.answer,
                    "category": getattr(item, 'category', ''),
                    "keywords": getattr(item, 'keywords', []),
                }
            )
            results.append(result)

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]

    def _vector_search(
        self,
        query: str,
        top_k: int,
        enterprise_ids: Optional[List[str]],
    ) -> List[Tuple]:
        """向量语义检索"""
        if not self.vector_store:
            return []

        all_results = []
        for eid in enterprise_ids or self.enterprise_ids:
            try:
                eid_results = self.vector_store.search(
                    enterpriseId=eid,
                    query=query,
                    topK=top_k,
                )
                all_results.extend(eid_results)
            except Exception:
                continue
        return all_results

    def _rrf_fusion(
        self,
        keyword_results: List[RetrievalResult],
        vector_results: List,
        top_k: int,
        k: int = 60,
    ) -> List[RetrievalResult]:
        """RRF融合合并结果"""
        doc_scores = {}
        doc_items = {}

        for rank, result in enumerate(keyword_results):
            rrf_score = 1.0 / (k + rank + 1)
            # 修复 P9：统一权重，与 PipelineConfig 和 rag_weights.yaml 一致
            doc_scores[result.doc_id] = doc_scores.get(result.doc_id, 0) + rrf_score * 0.35
            doc_items[result.doc_id] = result

        for rank, (doc, score) in enumerate(vector_results):
            doc_id = doc.get("id", "")
            if doc_id:
                rrf_score = 1.0 / (k + rank + 1)
                doc_scores[doc_id] = doc_scores.get(doc_id, 0) + rrf_score * 0.40
                if doc_id not in doc_items:
                    doc_items[doc_id] = RetrievalResult(
                        doc_id=doc_id,
                        content=doc.get("content", ""),
                        score=score,
                        source="vector",
                        metadata=doc,
                    )

        sorted_docs = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for doc_id, rrf_score in sorted_docs[:top_k]:
            if doc_id in doc_items:
                result = doc_items[doc_id]
                result.score = rrf_score * 100
                result.source = "hybrid"
                results.append(result)

        return results

    def _simple_search(
        self,
        query: str,
        top_k: int,
        enterprise_ids: Optional[List[str]],
    ) -> List[RetrievalResult]:
        """简单关键词检索"""
        return self._keyword_search(query, top_k, enterprise_ids)

    def _exact_first_search(
        self,
        query: str,
        top_k: int,
        category: Optional[str],
        enterprise_ids: Optional[List[str]],
    ) -> List[RetrievalResult]:
        """精确匹配优先"""
        results = []
        query_lower = query.lower()

        for item in self.knowledge_items:
            if category and getattr(item, 'category', '') != category:
                continue
            if not self._item_matches_enterprise(item, enterprise_ids or []):
                continue

            question_lower = item.question.lower()
            if query_lower in question_lower or question_lower in query_lower:
                results.append(RetrievalResult(
                    doc_id=item.id,
                    content=f"{item.question} {item.answer}",
                    score=100.0,
                    source="exact_match",
                    metadata={"question": item.question, "answer": item.answer},
                ))

        if len(results) < top_k:
            results.extend(self._keyword_search(query, top_k - len(results), enterprise_ids))

        return results[:top_k]

    def _semantic_first_search(
        self,
        query: str,
        top_k: int,
        enterprise_ids: Optional[List[str]],
    ) -> List[RetrievalResult]:
        """语义检索优先"""
        vector_results = self._vector_search(query, top_k, enterprise_ids)
        if vector_results:
            results = []
            for doc, score in vector_results:
                results.append(RetrievalResult(
                    doc_id=doc.get("id", ""),
                    content=doc.get("content", ""),
                    score=score,
                    source="semantic",
                    metadata=doc.get("metadata", {}),
                ))
            results.sort(key=lambda x: x.score, reverse=True)
            return results[:top_k]
        return self._hybrid_search(query, top_k, enterprise_ids)

    def _procedure_first_search(
        self,
        query: str,
        top_k: int,
        enterprise_ids: Optional[List[str]],
    ) -> List[RetrievalResult]:
        """流程步骤优先"""
        procedure_keywords = ["步骤", "方法", "流程", "教程", "指南", "操作"]
        results = []

        for item in self.knowledge_items:
            if not self._item_matches_enterprise(item, enterprise_ids or []):
                continue
            answer_lower = item.answer.lower()
            procedure_score = sum(1 for kw in procedure_keywords if kw in answer_lower)
            if procedure_score > 0:
                results.append(RetrievalResult(
                    doc_id=item.id,
                    content=f"{item.question} {item.answer}",
                    score=procedure_score * 10,
                    source="procedure",
                    metadata={"question": item.question, "answer": item.answer},
                ))

        results.sort(key=lambda x: x.score, reverse=True)
        if len(results) < top_k:
            results.extend(self._keyword_search(query, top_k - len(results), enterprise_ids))

        return results[:top_k]

    def _priority_first_search(
        self,
        query: str,
        top_k: int,
        enterprise_ids: Optional[List[str]],
    ) -> List[RetrievalResult]:
        """优先级检索(投诉问题)"""
        priority_keywords = ["退款", "投诉", "问题", "故障", "异常"]
        results = []

        for item in self.knowledge_items:
            if not self._item_matches_enterprise(item, enterprise_ids or []):
                continue
            question_lower = item.question.lower()
            priority_score = sum(1 for kw in priority_keywords if kw in question_lower)
            if priority_score > 0:
                results.append(RetrievalResult(
                    doc_id=item.id,
                    content=f"{item.question} {item.answer}",
                    score=priority_score * 20,
                    source="priority",
                    metadata={"question": item.question, "answer": item.answer},
                ))

        results.sort(key=lambda x: x.score, reverse=True)
        if len(results) < top_k:
            results.extend(self._keyword_search(query, top_k - len(results), enterprise_ids))

        return results[:top_k]

    def _multi_hop_search(
        self,
        query: str,
        top_k: int,
        enterprise_ids: Optional[List[str]],
    ) -> List[RetrievalResult]:
        """多跳推理检索"""
        # 分解查询为子查询
        sub_queries = self._decompose_query(query)
        if not sub_queries:
            return self._hybrid_search(query, top_k, enterprise_ids)

        all_results = []
        seen_doc_ids = set()

        for sub_q in sub_queries[:3]:  # 限制最多3个子查询
            sub_results = self._hybrid_search(sub_q, top_k // 2, enterprise_ids)
            for r in sub_results:
                if r.doc_id not in seen_doc_ids:
                    seen_doc_ids.add(r.doc_id)
                    r.metadata["sub_query"] = sub_q
                    all_results.append(r)

        # 补充原始查询结果
        base_results = self._hybrid_search(query, top_k, enterprise_ids)
        for r in base_results:
            if r.doc_id not in seen_doc_ids:
                seen_doc_ids.add(r.doc_id)
                all_results.append(r)

        all_results.sort(key=lambda x: x.score, reverse=True)
        return all_results[:top_k]

    def _decompose_query(self, query: str) -> List[str]:
        """分解复杂查询为子查询"""
        decomposition_patterns = [
            (r"(.+?)然后(.+)",),
            (r"(.+?)之后(.+)",),
            (r"(.+?)并且(.+)",),
            (r"(.+?)同时(.+)",),
            (r"(.+?)还是(.+)",),
        ]

        for pattern in decomposition_patterns:
            match = re.search(pattern, query)
            if match:
                return [group.strip() for group in match.groups() if group.strip()]

        return []

    def _calculate_keyword_score(self, query_terms: List[str], doc_text: str, item: Any) -> float:
        """计算关键词匹配分数"""
        if not query_terms:
            return 0.0

        doc_lower = doc_text.lower()
        score = 0.0

        for term in query_terms:
            if len(term) < 2:
                continue
            if term in doc_lower:
                count = doc_lower.count(term)
                score += count * 10.0
            if hasattr(item, 'keywords') and item.keywords:
                for keyword in item.keywords:
                    if term in keyword.lower():
                        score += 15.0

        if hasattr(item, 'question') and item.question:
            question_lower = item.question.lower()
            for term in query_terms:
                if term in question_lower:
                    score += 20.0

        return score


# ============== 工厂函数 (向后兼容) ==============

_unified_retriever_instance = None
_retriever_lock = threading.Lock()


def get_unified_retriever(reset: bool = False) -> UnifiedRetriever:
    global _unified_retriever_instance
    if _unified_retriever_instance is not None and not reset:
        return _unified_retriever_instance
    with _retriever_lock:
        if _unified_retriever_instance is not None and not reset:
            return _unified_retriever_instance
        from src.common.knowledge_base_adapter import get_learning_knowledge_base_adapter
        adapter = get_learning_knowledge_base_adapter()
        items = adapter.get_knowledge_list()
        _unified_retriever_instance = UnifiedRetriever(items)
    return _unified_retriever_instance


def create_unified_retriever(
    knowledge_items: List,
    vector_store: Any = None,
    llm_client: Any = None,
    config: Optional[RetrievalConfig] = None,
) -> UnifiedRetriever:
    """创建统一检索器实例"""
    return UnifiedRetriever(knowledge_items, vector_store, llm_client, config)


# ============== 向后兼容的别名 ==============

# 原有类名别名 (用于类型注解和 isinstance 检查的向后兼容)
RAGRetriever = UnifiedRetriever

# 原有函数保持可用，内部委托给UnifiedRetriever
def create_rag_retriever(
    knowledge_items: List,
    vector_store: Any = None,
    llm_client: Any = None,
    enterprise_ids: Optional[List[str]] = None,
) -> UnifiedRetriever:
    """向后兼容: 创建RAG检索器 (实际返回UnifiedRetriever)"""
    config = RetrievalConfig(
        enterprise_ids=enterprise_ids,
        strategy=RetrievalStrategy.HYBRID,
    )
    return UnifiedRetriever(knowledge_items, vector_store, llm_client, config)


def create_enhanced_rag_retriever(
    knowledgeItems: List,
    vectorStore: Any = None,
    llmClient: Any = None,
) -> UnifiedRetriever:
    """向后兼容: 创建增强型RAG检索器 (实际返回UnifiedRetriever)"""
    config = RetrievalConfig(strategy=RetrievalStrategy.ADAPTIVE)
    return UnifiedRetriever(knowledgeItems, vectorStore, llmClient, config)


# 导出主要类和函数
__all__ = [
    'UnifiedRetriever',
    'UnifiedResult',
    'RetrievalConfig',
    'RetrievalStrategy',
    'QueryComplexity',
    'QueryRewriter',
    'Reranker',
    'IntentClassifier',
    'QueryComplexityAnalyzer',
    'create_unified_retriever',
    'get_unified_retriever',
    'get_reranker',
    # 向后兼容
    'RAGRetriever',
    'create_rag_retriever',
    'create_enhanced_rag_retriever',
]
