"""
BM25关键词检索模块
替代简单的TF-IDF，实现工业级关键词检索

BM25 (Best Matching 25) 是一种基于概率模型的检索算法
是TF-IDF的改进版，核心是词频饱和函数和文档长度归一化
"""

import math
import re
from typing import List, Dict, Tuple, Optional, Set
from collections import Counter, defaultdict
from dataclasses import dataclass
from loguru import logger
import numpy as np


@dataclass
class BM25Config:
    """BM25配置"""
    k1: float = 1.5
    b: float = 0.75
    min_term_length: int = 1  # 修复 P11：2→1，保留"价、费、买、退"等高频单字关键词
    max_term_length: int = 50


class BM25Document:
    """BM25文档"""

    def __init__(self, doc_id: str, content: str, metadata: Optional[Dict] = None):
        self.id = doc_id
        self.content = content
        self.metadata = metadata or {}
        self.terms: List[str] = []
        self.term_freqs: Dict[str, int] = {}
        self.length: int = 0


class BM25Retriever:
    """
    BM25检索器

    特点：
    1. 词频饱和：使用k1参数控制词频饱和度，避免词频过高时得分过高
    2. 文档长度归一化：使用b参数归一化文档长度
    3. IDF权重：使用逆文档频率降低常见词权重
    """

    def __init__(self, config: Optional[BM25Config] = None):
        self.config = config or BM25Config()
        self.documents: Dict[str, BM25Document] = {}
        self.corpus_size: int = 0
        self.avgdl: float = 0.0
        self.doc_freqs: Dict[str, int] = defaultdict(int)
        self.idf: Dict[str, float] = {}
        self._initialized: bool = False

    def _tokenize(self, text: str) -> List[str]:
        """
        文本分词

        使用jieba进行中文分词，英文按空格分割
        """
        import jieba

        text = text.lower()
        text = re.sub(r'[^\w\s\u4e00-\u9fff]', ' ', text)

        tokens = list(jieba.cut(text))
        tokens = [t.strip() for t in tokens if t.strip()]

        tokens = [
            t for t in tokens
            if self.config.min_term_length <= len(t) <= self.config.max_term_length
        ]

        return tokens

    def _calculate_idf(self) -> Dict[str, float]:
        """
        计算IDF（逆文档频率）

        IDF = log((N - n + 0.5) / (n + 0.5) + 1)

        N: 语料库文档总数
        n: 包含该词的文档数
        """
        idf = {}
        for term, df in self.doc_freqs.items():
            idf[term] = math.log(
                (self.corpus_size - df + 0.5) / (df + 0.5) + 1
            )
        return idf

    def index(self, documents: List[Dict], batch_size: int = 1000):
        """
        构建BM25索引

        Args:
            documents: 文档列表，每个文档包含id、content、metadata
            batch_size: 批处理大小
        """
        self.documents.clear()
        self.doc_freqs.clear()

        all_terms = set()

        for i, doc in enumerate(documents):
            doc_id = doc.get('id', str(i))
            content = doc.get('content', '')
            metadata = doc.get('metadata', {})

            bm25_doc = BM25Document(doc_id, content, metadata)
            terms = self._tokenize(content)

            bm25_doc.terms = terms
            bm25_doc.term_freqs = Counter(terms)
            bm25_doc.length = len(terms)

            self.documents[doc_id] = bm25_doc

            for term in set(terms):
                self.doc_freqs[term] += 1

            all_terms.update(terms)

        self.corpus_size = len(self.documents)
        total_length = sum(d.length for d in self.documents.values())
        self.avgdl = total_length / self.corpus_size if self.corpus_size > 0 else 0

        self.idf = self._calculate_idf()
        self._initialized = True

        return self

    def add_documents(self, documents: List[Dict]):
        if not documents:
            return self

        for i, doc in enumerate(documents):
            doc_id = doc.get('id', str(self.corpus_size + i))
            content = doc.get('content', '')
            metadata = doc.get('metadata', {})

            bm25_doc = BM25Document(doc_id, content, metadata)
            terms = self._tokenize(content)

            bm25_doc.terms = terms
            bm25_doc.term_freqs = Counter(terms)
            bm25_doc.length = len(terms)

            self.documents[doc_id] = bm25_doc

            for term in set(terms):
                self.doc_freqs[term] += 1

        self.corpus_size = len(self.documents)
        total_length = sum(d.length for d in self.documents.values())
        self.avgdl = total_length / self.corpus_size if self.corpus_size > 0 else 0

        self.idf = self._calculate_idf()
        self._initialized = True

        return self

    def _bm25_score(self, query_terms: List[str], doc: BM25Document) -> float:
        """
        计算单个文档的BM25分数

        BM25公式:
        score(D, Q) = sum(IDF(qi) * f(qi, D) * (k1 + 1)) / (f(qi, D) + k1 * (1 - b + b * |D|/avgdl))

        Args:
            query_terms: 查询词列表
            doc: BM25Document对象

        Returns:
            BM25分数
        """
        score = 0.0
        doc_len = doc.length

        for term in query_terms:
            if term not in self.idf:
                continue

            tf = doc.term_freqs.get(term, 0)
            if tf == 0:
                continue

            idf = self.idf[term]

            numerator = tf * (self.config.k1 + 1)
            denominator = tf + self.config.k1 * (
                1 - self.config.b + self.config.b * doc_len / self.avgdl
            )

            score += idf * numerator / denominator

        return score

    def search(
        self,
        query: str,
        top_k: int = 10,
        min_score: float = 0.0,
        filter_metadata: Optional[Dict] = None
    ) -> List[Tuple[Dict, float]]:
        """
        BM25检索

        Args:
            query: 查询文本
            top_k: 返回结果数量
            min_score: 最低分数阈值
            filter_metadata: 元数据过滤条件

        Returns:
            [(文档, 分数), ...] 按分数降序排列
        """
        if not self._initialized:
            return []

        query_terms = self._tokenize(query)
        if not query_terms:
            return []

        doc_scores = []

        for doc_id, doc in self.documents.items():
            if filter_metadata:
                match = all(
                    doc.metadata.get(k) == v
                    for k, v in filter_metadata.items()
                )
                if not match:
                    continue

            score = self._bm25_score(query_terms, doc)

            if score >= min_score:
                doc_scores.append((doc_id, score))

        doc_scores.sort(key=lambda x: x[1], reverse=True)

        results = []
        for doc_id, score in doc_scores[:top_k]:
            doc = self.documents[doc_id]
            result = {
                'id': doc.id,
                'content': doc.content,
                'metadata': doc.metadata,
                'terms': doc.terms
            }
            results.append((result, score))

        return results

    def search_with_expansion(
        self,
        query: str,
        top_k: int = 10,
        use_synonym_expansion: bool = True,
        use_phrase_expansion: bool = True,
        filter_metadata: Optional[Dict] = None,
    ) -> List[Tuple[Dict, float]]:
        """
        带查询扩展的BM25检索

        扩展词直接参与BM25评分计算，避免被search()内部重新分词丢弃

        Args:
            query: 查询文本
            top_k: 返回结果数量
            use_synonym_expansion: 是否使用同义词扩展
            use_phrase_expansion: 是否使用短语扩展

        Returns:
            [(文档, 分数), ...]
        """
        if not self._initialized:
            return []

        expanded_terms = self._expand_query(
            query,
            synonym=use_synonym_expansion,
            phrase=use_phrase_expansion
        )

        query_terms = self._tokenize(query)
        all_terms = query_terms + expanded_terms
        all_terms = list(dict.fromkeys(all_terms))

        if not all_terms:
            return []

        doc_scores = []
        for doc_id, doc in self.documents.items():
            if filter_metadata:
                match = all(
                    doc.metadata.get(k) == v
                    for k, v in filter_metadata.items()
                )
                if not match:
                    continue
            score = self._bm25_score(all_terms, doc)
            if score > 0:
                doc_scores.append((doc_id, score))

        doc_scores.sort(key=lambda x: x[1], reverse=True)

        results = []
        for doc_id, score in doc_scores[:top_k]:
            doc = self.documents[doc_id]
            result = {
                'id': doc.id,
                'content': doc.content,
                'metadata': doc.metadata,
                'terms': doc.terms
            }
            results.append((result, score))

        return results

    def _expand_query(
        self,
        query: str,
        synonym: bool = True,
        phrase: bool = True
    ) -> List[str]:
        """
        查询扩展

        生成额外的查询词以提高召回率
        """
        expanded = []
        terms = self._tokenize(query)

        if synonym:
            for term in terms:
                synonyms = self._get_synonyms(term)
                expanded.extend(synonyms)

        if phrase:
            for i in range(len(terms) - 1):
                phrase_term = f"{terms[i]}{terms[i + 1]}"
                expanded.append(phrase_term)

        return expanded

    def _get_synonyms(self, term: str) -> List[str]:
        """
        获取同义词（简化实现）

        实际项目中应使用专业的中文同义词词典
        """
        synonyms_map = {
            '产品': ['商品', '东西'],
            '服务': ['售后', '支持'],
            '价格': ['费用', '收费', '价钱'],
            '公司': ['企业', '厂商'],
            '购买': ['买', '订购', '下单'],
            '退款': ['退钱', '退货'],
            '合作': ['加盟', '代理'],
        }

        return synonyms_map.get(term, [])

    def get_relevant_terms(self, query: str, top_n: int = 10) -> List[Tuple[str, float, int]]:
        """
        获取与查询相关的关键词及其权重

        用于调试和理解检索结果
        """
        if not self._initialized:
            return []

        query_terms = self._tokenize(query)
        term_weights = []

        for term in query_terms:
            if term in self.idf:
                df = self.doc_freqs.get(term, 0)
                term_weights.append((term, self.idf[term], df))

        term_weights.sort(key=lambda x: x[1], reverse=True)

        return [(t, idf, df) for t, idf, df in term_weights[:top_n]]

    def get_statistics(self) -> Dict:
        """获取BM25索引统计信息"""
        return {
            'corpus_size': self.corpus_size,
            'avg_document_length': self.avgdl,
            'unique_terms': len(self.idf),
            'indexed_documents': len(self.documents)
        }


class BM25HybridSearcher:
    """
    BM25混合检索器

    结合BM25关键词检索和向量语义检索
    """

    def __init__(
        self,
        bm25_retriever: BM25Retriever,
        vector_store=None,
        embedding_service=None,
        rrf_k: int = 60
    ):
        self.bm25 = bm25_retriever
        self.vector_store = vector_store
        self.embedding = embedding_service
        self.rrf_k = rrf_k

    def hybrid_search(
        self,
        query: str,
        top_k: int = 10,
        bm25_weight: float = 0.4,
        vector_weight: float = 0.6,
        *,
        enterprise_id: str = "",
        filter_metadata: Optional[Dict] = None,
    ) -> List[Tuple[Dict, float]]:
        """
        混合检索

        Args:
            query: 查询文本
            top_k: 返回结果数量
            bm25_weight: BM25检索权重
            vector_weight: 向量检索权重

        Returns:
            [(文档, 融合分数), ...]
        """
        effective_enterprise_id, effective_filter = self._resolve_search_scope(
            enterprise_id=enterprise_id,
            filter_metadata=filter_metadata,
        )
        if not effective_enterprise_id:
            return []
        bm25_results = self.bm25.search(
            query,
            top_k=top_k * 2,
            filter_metadata=effective_filter,
        )
        bm25_scores = {doc['id']: (doc, score) for doc, score in bm25_results}

        vector_results = []
        if self.vector_store and self.embedding:
            try:
                vector_results = self.vector_store.search(
                    enterpriseId=effective_enterprise_id,
                    query=query,
                    topK=top_k * 2,
                    filterConditions=effective_filter,
                )
            except Exception:
                vector_results = []

        vector_scores = {doc['id']: (doc, score) for doc, score in vector_results}

        all_doc_ids = set(bm25_scores.keys()) | set(vector_scores.keys())

        fused_scores = {}
        for doc_id in all_doc_ids:
            bm25_doc, bm25_score = bm25_scores.get(doc_id, (None, 0))
            vector_doc, vector_score = vector_scores.get(doc_id, (None, 0))

            doc = bm25_doc or vector_doc
            if doc is None:
                continue

            fused_score = bm25_weight * bm25_score + vector_weight * vector_score
            fused_scores[doc_id] = (doc, fused_score)

        results = sorted(
            fused_scores.items(),
            key=lambda x: x[1][1],
            reverse=True
        )

        return [(doc, score) for doc, score in [r[1] for r in results[:top_k]]]

    def rrf_fusion(
        self,
        query: str,
        top_k: int = 10,
        k: int = 60,
        *,
        enterprise_id: str = "",
        filter_metadata: Optional[Dict] = None,
    ) -> List[Tuple[Dict, float]]:
        """
        RRF (Reciprocal Rank Fusion) 融合

        RRF公式: score(d) = sum(1 / (k + rank(d, Ri)))

        Args:
            query: 查询文本
            top_k: 返回结果数量
            k: RRF平滑参数

        Returns:
            [(文档, RRF分数), ...]
        """
        effective_enterprise_id, effective_filter = self._resolve_search_scope(
            enterprise_id=enterprise_id,
            filter_metadata=filter_metadata,
        )
        if not effective_enterprise_id:
            return []
        bm25_results = self.bm25.search(
            query,
            top_k=top_k * 2,
            filter_metadata=effective_filter,
        )
        vector_results = []

        if self.vector_store and self.embedding:
            try:
                vector_results = self.vector_store.search(
                    enterpriseId=effective_enterprise_id,
                    query=query,
                    topK=top_k * 2,
                    filterConditions=effective_filter,
                )
            except Exception:
                vector_results = []

        scores = defaultdict(float)

        for rank, (doc, _) in enumerate(bm25_results):
            scores[doc['id']] += 1 / (k + rank + 1)

        for rank, (doc, _) in enumerate(vector_results):
            scores[doc['id']] += 1 / (k + rank + 1)

        all_docs = {}
        for doc, _ in bm25_results:
            all_docs[doc['id']] = doc
        for doc, _ in vector_results:
            all_docs[doc['id']] = doc

        results = [
            (all_docs[doc_id], score)
            for doc_id, score in scores.items()
        ]

        results.sort(key=lambda x: x[1], reverse=True)

        return results[:top_k]

    @staticmethod
    def _resolve_search_scope(
        *,
        enterprise_id: str = "",
        filter_metadata: Optional[Dict] = None,
    ) -> Tuple[str, Optional[Dict]]:
        effective_filter = dict(filter_metadata or {})
        normalized_enterprise_id = str(
            enterprise_id or effective_filter.get("enterprise_id") or ""
        ).strip()
        if not normalized_enterprise_id:
            effective_filter.pop("enterprise_id", None)
            return "", effective_filter or None
        effective_filter["enterprise_id"] = normalized_enterprise_id
        return normalized_enterprise_id, effective_filter or None


def create_bm25_retriever(documents: Optional[List[Dict]] = None) -> BM25Retriever:
    """
    创建BM25检索器实例

    Args:
        documents: 初始文档列表

    Returns:
        BM25Retriever实例
    """
    config = BM25Config(k1=1.5, b=0.75)
    retriever = BM25Retriever(config)

    if documents:
        retriever.index(documents)

    return retriever
