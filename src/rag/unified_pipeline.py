"""
统一检索管道

整合知识库检索、BM25检索、向量检索、知识图谱检索为单一管道
消除KnowledgeBaseManager.search()和HybridRetriever.search()的重叠

管道阶段：预处理 → 多路并行检索 → RRF融合 → 重排序 → 过滤
每个阶段可插拔替换

参考：NirDiamant/RAG_Techniques - Fusion Retrieval + Reranking
"""

from loguru import logger
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional, Any, Callable, Tuple
from dataclasses import dataclass, field
from enum import Enum

from src.common.industry_schema_service import get_industry_schema_service, get_active_schema_with_compat



class RetrievalSource(Enum):
    """检索来源"""
    KEYWORD = "keyword"
    VECTOR = "vector"
    BM25 = "bm25"
    KNOWLEDGE_BASE = "knowledge_base"
    KNOWLEDGE_GRAPH = "knowledge_graph"


@dataclass
class PipelineResult:
    """管道检索结果"""
    doc_id: str
    content: str
    score: float
    source: str
    metadata: Dict = field(default_factory=dict)


@dataclass
class PipelineConfig:
    """管道配置"""
    top_k: int = 5
    # P0-3: 融合权重默认值，可被 from_weights 覆盖
    keyword_weight: float = 0.35
    vector_weight: float = 0.40
    bm25_weight: float = 0.20
    kg_weight: float = 0.20
    rrf_k: int = 60
    enable_reranking: bool = True
    enable_query_rewrite: bool = True
    enable_query_expansion: bool = True
    enable_semantic_dedup: bool = True
    enable_relevance_filter: bool = True
    score_threshold: float = 0.1
    relevance_threshold: float = 0.15
    dedup_similarity_threshold: float = 0.7
    max_expanded_queries: int = 3
    # 并行检索配置
    enable_parallel_retrieval: bool = True
    parallel_max_workers: int = 4
    # 知识图谱检索配置
    kg_max_paths: int = 5

    @classmethod
    def from_weights(
        cls,
        schema_id: str = "",
        enterprise_id: str = "",
        industry: str = "",
        schema: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> "PipelineConfig":
        """从 weight_loader 加载融合权重创建配置。

        优先级：schema rrf_weights > yaml enterprises > yaml industries > yaml default > dataclass 默认值
        其余字段（top_k/enable_*/thresholds）使用 kwargs 或默认值。
        """
        from src.rag.weight_loader import load_rag_weights
        weights = load_rag_weights(
            schema_id=schema_id,
            enterprise_id=enterprise_id,
            industry=industry,
            schema=schema,
        )
        config_kwargs: Dict[str, Any] = {}
        for key in ("keyword_weight", "vector_weight", "bm25_weight", "kg_weight", "rrf_k"):
            if key in weights:
                config_kwargs[key] = weights[key]
        # kwargs 覆盖权重（允许调用方显式指定）
        config_kwargs.update(kwargs)
        return cls(**config_kwargs)


class UnifiedRetrievalPipeline:
    """
    统一检索管道

    整合所有检索路径为单一管道，消除冗余
    """

    GENERIC_SYNONYM_MAP = {
        "价格": ["费用", "多少钱", "收费", "报价"],
        "优惠": ["折扣", "打折", "促销", "减免", "让利"],
        "产品": ["商品", "方案", "服务"],
        "功能": ["作用", "能力", "特点"],
        "服务": ["支持", "售后", "帮助"],
        "联系": ["联系方式", "电话", "微信"],
        "退款": ["退钱", "退费", "退订"],
        # P1-6: "发货"组（配送/快递/物流）是实物电商专用，迁移到电商 schema query_understanding.synonym_groups
    }

    def __init__(
        self,
        knowledge_base=None,
        vector_retriever=None,
        bm25_retriever=None,
        reranker=None,
        query_rewriter=None,
        knowledge_graph=None,
        config: Optional[PipelineConfig] = None
    ):
        """
        初始化统一检索管道

        Args:
            knowledge_base: 知识库管理器
            vector_retriever: 向量检索器
            bm25_retriever: BM25检索器
            reranker: 重排序器
            query_rewriter: 查询改写器
            knowledge_graph: 知识图谱服务（KnowledgeGraphService）
            config: 管道配置
        """
        self.knowledge_base = knowledge_base
        self.vector_retriever = vector_retriever
        self.bm25_retriever = bm25_retriever
        self.reranker = reranker
        self.query_rewriter = query_rewriter
        self.knowledge_graph = knowledge_graph
        self.config = config or PipelineConfig()
        self._filters: List[Callable] = []

    def _get_active_schema(self, context: Optional[Dict[str, Any]] = None):
        context = context or {}
        try:
            return get_active_schema_with_compat(
                get_industry_schema_service(),
                enterprise_id=str(context.get("enterprise_id") or ""),
                preferred_schema_id=str(
                    getattr(context.get("retrieval_plan"), "schema_id", "") or context.get("schema_id") or ""
                ),
            ) or {}
        except Exception:
            return {}

    def _get_domain_keywords(self, context: Optional[Dict[str, Any]] = None):
        schema = self._get_active_schema(context)
        try:
            keywords = schema.get("metadata", {}).get("query_understanding", {}).get("domain_keywords") or []
            return tuple(keywords) if keywords else ()
        except Exception:
            return ()

    def _get_query_understanding_config(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        schema = self._get_active_schema(context)
        try:
            metadata = schema.get("metadata") or {}
            config = metadata.get("query_understanding") or {}
            return config if isinstance(config, dict) else {}
        except Exception:
            return {}

    def _get_runtime_flags(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        schema = self._get_active_schema(context)
        try:
            metadata = schema.get("metadata") or {}
            runtime_flags = metadata.get("runtime_flags") or {}
            return runtime_flags if isinstance(runtime_flags, dict) else {}
        except Exception:
            return {}

    def _get_domain_synonym_map(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
        config = self._get_query_understanding_config(context)
        raw_map = config.get("synonym_map") or {}
        if not isinstance(raw_map, dict):
            return {}
        normalized: Dict[str, List[str]] = {}
        for key, synonyms in raw_map.items():
            term = str(key or "").strip()
            if not term:
                continue
            values = [str(item or "").strip() for item in (synonyms or []) if str(item or "").strip()]
            if values:
                normalized[term] = values
        return normalized

    def add_filter(self, filter_fn: Callable):
        """添加后过滤器"""
        self._filters.append(filter_fn)

    @staticmethod
    def _resolve_plan(context: Optional[Dict[str, Any]] = None):
        if not context:
            return None
        return context.get("retrieval_plan")

    def _resolve_flag(self, context: Optional[Dict[str, Any]], attr_name: str) -> bool:
        plan = self._resolve_plan(context)
        if plan is not None and hasattr(plan, attr_name):
            return bool(getattr(plan, attr_name))
        return bool(getattr(self.config, attr_name))

    def _resolve_enabled_sources(self, context: Optional[Dict[str, Any]]) -> List[str]:
        plan = self._resolve_plan(context)
        if plan is not None and getattr(plan, "enabled_sources", None):
            return [str(source or "").strip() for source in getattr(plan, "enabled_sources", []) if str(source or "").strip()]
        # 默认启用四路检索：知识库 + 向量 + BM25 + 知识图谱
        # 知识图谱仅在实例注入时实际生效（见 _multi_retrieve 中的判断）
        return ["knowledge_base", "vector", "bm25", "knowledge_graph"]

    def _resolve_per_source_top_k(self, top_k: int, context: Optional[Dict[str, Any]]) -> int:
        plan = self._resolve_plan(context)
        if plan is not None and int(getattr(plan, "per_source_top_k", 0) or 0) > 0:
            return max(int(getattr(plan, "per_source_top_k", 0) or 0), top_k)
        return top_k * 2

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        context: Optional[Dict] = None,
        category: Optional[str] = None
    ) -> List[PipelineResult]:
        """
        统一检索入口

        Args:
            query: 查询文本
            top_k: 返回数量
            context: 对话上下文
            category: 分类过滤

        Returns:
            检索结果列表
        """
        top_k = top_k or self.config.top_k

        # 阶段1：查询预处理
        processed_query = self._preprocess(query, context)

        # 阶段1.5：查询扩展（生成同义查询变体，扩大检索覆盖）
        expanded_queries = [processed_query]
        if self._resolve_flag(context, "enable_query_expansion"):
            expanded_queries = self._expand_query(processed_query, context=context)
            if len(expanded_queries) > self.config.max_expanded_queries + 1:
                expanded_queries = expanded_queries[:self.config.max_expanded_queries + 1]

        # 阶段2：多路检索（对每个扩展查询执行检索）
        all_results = {}
        for q in expanded_queries:
            q_results = self._multi_retrieve(q, top_k, category, context=context)
            for source, results in q_results.items():
                if source in all_results:
                    existing_ids = {r.doc_id for r in all_results[source]}
                    for r in results:
                        if r.doc_id not in existing_ids:
                            all_results[source].append(r)
                            existing_ids.add(r.doc_id)
                else:
                    all_results[source] = results

        # 阶段3：RRF融合
        fused = self._rrf_fusion(all_results, top_k)

        # 阶段4：重排序
        if self.config.enable_reranking and self.reranker:
            fused = self._rerank(processed_query, fused, top_k)

        # 阶段4.5：语义去重（消除高度相似的重复结果）
        if self._resolve_flag(context, "enable_semantic_dedup"):
            fused = self._semantic_dedup(fused)

        # 阶段4.6：相关性过滤（过滤与查询不相关的结果）
        if self._resolve_flag(context, "enable_relevance_filter"):
            fused = self._relevance_filter(processed_query, fused)

        # 阶段5：后过滤
        fused = self._post_filter(fused)

        return fused[:top_k]

    def _preprocess(self, query: str, context: Optional[Dict] = None) -> str:
        """查询预处理，包含查询扩展"""
        processed = query

        # 修复 N8：如果查询已被主链改写（context 中标记），跳过 pipeline 内部重复改写
        # 主链 ContextAwareQueryRewriter 已完成改写，pipeline 收到的是改写后的查询
        if context and context.get("query_already_rewritten"):
            logger.debug(f"查询已被主链改写，pipeline 跳过重复改写: '{query}'")
            return processed

        if self._resolve_flag(context, "enable_query_rewrite") and self.query_rewriter:
            if context and context.get('conversation_history'):
                try:
                    result = self.query_rewriter.rewrite(
                        query, context['conversation_history']
                    )
                    if result.rewritten_query and result.confidence > 0.5:
                        logger.debug(f"查询改写: '{query}' → '{result.rewritten_query}'")
                        processed = result.rewritten_query
                except Exception as e:
                    logger.warning(f"查询改写失败: {e}")

        return processed

    def _expand_query(self, query: str, context: Optional[Dict[str, Any]] = None) -> List[str]:
        """
        查询扩展 - 生成同义查询变体
        
        策略：
        1. jieba分词提取核心词组合
        2. 同义词替换（通用基座 + schema 按需注入）
        3. 实体识别增强
        """
        expanded = [query]
        try:
            import jieba
            words = list(jieba.cut(query))
            if len(words) > 2:
                core_words = [w for w in words if len(w.strip()) >= 2]
                if core_words:
                    expanded.append(" ".join(core_words))
        except Exception as e:
            logger.debug(f"查询扩展处理失败: {e}")
        
        try:
            synonym_groups: List[Dict[str, List[str]]] = []
            if self._is_route_domain_context(query, context=context):
                synonym_groups.append(self._get_domain_synonym_map(context))
            synonym_groups.append(dict(self.GENERIC_SYNONYM_MAP))

            for synonym_map in synonym_groups:
                matched = False
                for key, synonyms in synonym_map.items():
                    if key in query:
                        for syn in synonyms[:2]:
                            variant = query.replace(key, syn)
                            if variant not in expanded:
                                expanded.append(variant)
                        matched = True
                        break
                if matched:
                    break
        except Exception as e:
            logger.debug(f"同义词扩展失败: {e}")

        try:
            retrieval_plan = (context or {}).get("retrieval_plan")
            preferred_terms = list(getattr(retrieval_plan, "preferred_terms", []) or [])
            for term in preferred_terms[:3]:
                term = str(term or "").strip()
                if not term:
                    continue
                if term not in expanded:
                    expanded.append(term)
                if term not in query:
                    combined = f"{query} {term}".strip()
                    if combined not in expanded:
                        expanded.append(combined)
        except Exception as e:
            logger.debug(f"检索规划扩展失败: {e}")
        
        return expanded

    def _is_route_domain_context(self, query: str, context: Optional[Dict[str, Any]] = None) -> bool:
        text = str(query or "").strip()
        active_schema = self._get_active_schema(context)
        domain_keywords = self._get_domain_keywords(context)
        if domain_keywords and any(term in text for term in domain_keywords):
            return True
        runtime_flags = self._get_runtime_flags(context)
        return bool(
            runtime_flags.get("route_domain_enabled")
            or runtime_flags.get("domain_query_expansion_enabled")
            or active_schema.get("is_domain_specific", False)
        )

    def _multi_retrieve(
        self,
        query: str,
        top_k: int,
        category: Optional[str] = None,
        context: Optional[Dict] = None,
    ) -> Dict[str, List[PipelineResult]]:
        """
        多路并行检索

        支持四路检索来源：知识库 / 向量 / BM25 / 知识图谱
        默认使用 ThreadPoolExecutor 并行执行，降低端到端延迟；
        任一路失败仅记录 warning 不影响其他路。
        """
        results: Dict[str, List[PipelineResult]] = {}
        enabled_sources = set(self._resolve_enabled_sources(context))
        per_source_top_k = self._resolve_per_source_top_k(top_k, context)

        # 构造检索任务列表
        tasks: List[Tuple[str, Callable[[], List[PipelineResult]]]] = []

        # 路径1：知识库检索
        if self.knowledge_base and "knowledge_base" in enabled_sources:
            def _kb_task() -> List[PipelineResult]:
                try:
                    try:
                        kb_results = self.knowledge_base.search_semantic(
                            query,
                            per_source_top_k,
                            category=category,
                            context=context,
                        )
                    except TypeError:
                        kb_results = self.knowledge_base.search_semantic(query, per_source_top_k, category)
                    pipeline_results = []
                    for item, score in kb_results:
                        pipeline_results.append(PipelineResult(
                            doc_id=item.id,
                            content=f"{item.question} {item.answer}",
                            score=score,
                            source=RetrievalSource.KNOWLEDGE_BASE.value,
                            metadata={"question": item.question, "answer": item.answer, "category": item.category}
                        ))
                    return pipeline_results
                except Exception as e:
                    logger.warning(f"知识库检索失败: {e}")
                    return []
            tasks.append(("knowledge_base", _kb_task))

        # 路径2：向量检索
        if self.vector_retriever and "vector" in enabled_sources:
            def _vec_task() -> List[PipelineResult]:
                try:
                    try:
                        vec_results = self.vector_retriever.search(
                            query,
                            per_source_top_k,
                            category=category,
                            context=context,
                        )
                    except TypeError:
                        vec_results = self.vector_retriever.search(query, per_source_top_k)
                    pipeline_results = []
                    for doc, score in vec_results:
                        pipeline_results.append(PipelineResult(
                            doc_id=doc.id if hasattr(doc, 'id') else str(doc),
                            content=doc.content if hasattr(doc, 'content') else str(doc),
                            score=score,
                            source=RetrievalSource.VECTOR.value,
                            metadata=getattr(doc, 'metadata', {})
                        ))
                    return pipeline_results
                except Exception as e:
                    logger.warning(f"向量检索失败: {e}")
                    return []
            tasks.append(("vector", _vec_task))

        # 路径3：BM25检索
        if self.bm25_retriever and "bm25" in enabled_sources:
            def _bm25_task() -> List[PipelineResult]:
                try:
                    try:
                        bm25_results = self.bm25_retriever.search(
                            query,
                            per_source_top_k,
                            category=category,
                            context=context,
                        )
                    except TypeError:
                        bm25_results = self.bm25_retriever.search(query, per_source_top_k)
                    pipeline_results = []
                    for doc, score in bm25_results:
                        pipeline_results.append(PipelineResult(
                            doc_id=doc.get('id', ''),
                            content=doc.get('content', ''),
                            score=score,
                            source=RetrievalSource.BM25.value,
                            metadata=doc.get('metadata', {})
                        ))
                    return pipeline_results
                except Exception as e:
                    logger.warning(f"BM25检索失败: {e}")
                    return []
            tasks.append(("bm25", _bm25_task))

        # 路径4：知识图谱检索
        if self.knowledge_graph and "knowledge_graph" in enabled_sources:
            def _kg_task() -> List[PipelineResult]:
                try:
                    return self._knowledge_graph_retrieve(query, per_source_top_k, context=context)
                except Exception as e:
                    logger.warning(f"知识图谱检索失败: {e}")
                    return []
            tasks.append(("knowledge_graph", _kg_task))

        if not tasks:
            return results

        # 执行检索：并行或顺序
        if self.config.enable_parallel_retrieval and len(tasks) > 1:
            results = self._run_retrieval_parallel(tasks)
        else:
            for source_name, task_fn in tasks:
                results[source_name] = task_fn()

        return results

    def _run_retrieval_parallel(
        self,
        tasks: List[Tuple[str, Callable[[], List[PipelineResult]]]],
    ) -> Dict[str, List[PipelineResult]]:
        """使用线程池并行执行多路检索"""
        results: Dict[str, List[PipelineResult]] = {}
        max_workers = min(self.config.parallel_max_workers, len(tasks))
        try:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rag-retrieve") as executor:
                future_to_source = {
                    executor.submit(task_fn): source_name
                    for source_name, task_fn in tasks
                }
                for future in as_completed(future_to_source):
                    source_name = future_to_source[future]
                    try:
                        results[source_name] = future.result()
                    except Exception as e:
                        logger.warning(f"并行检索任务 {source_name} 异常: {e}")
                        results[source_name] = []
        except Exception as e:
            logger.warning(f"并行检索执行失败，回退到顺序执行: {e}")
            for source_name, task_fn in tasks:
                if source_name not in results:
                    try:
                        results[source_name] = task_fn()
                    except Exception as ex:
                        logger.warning(f"顺序回退检索 {source_name} 失败: {ex}")
                        results[source_name] = []
        return results

    def _knowledge_graph_retrieve(
        self,
        query: str,
        top_k: int,
        context: Optional[Dict] = None,
    ) -> List[PipelineResult]:
        """
        知识图谱检索

        通过 KnowledgeGraphService.query_with_reasoning 获取实体、关系、推理路径，
        转换为 PipelineResult 参与后续 RRF 融合。
        """
        if not self.knowledge_graph:
            return []

        kg = self.knowledge_graph
        # 兼容两种形态：KnowledgeGraphService 实例 / 包装器
        if hasattr(kg, "query_with_reasoning"):
            try:
                kg_result = kg.query_with_reasoning(query, max_paths=self.config.kg_max_paths)
            except TypeError:
                kg_result = kg.query_with_reasoning(query)
        elif hasattr(kg, "query_related"):
            # 退化路径：仅按关键词查相关实体
            related = kg.query_related(query) or []
            kg_result = {"related_entities": related, "paths": [], "extracted_entities": []}
        else:
            logger.debug("知识图谱对象无可用查询方法，跳过")
            return []

        pipeline_results: List[PipelineResult] = []
        extracted_entities = kg_result.get("extracted_entities", []) or []
        related_entities = kg_result.get("related_entities", []) or []
        paths = kg_result.get("paths", []) or []

        # 将推理路径转换为检索结果（每条路径作为一个文档）
        for rank, path in enumerate(paths):
            if not isinstance(path, dict):
                continue
            source_name = str(path.get("source", "") or "").strip()
            target_name = str(path.get("target", "") or "").strip()
            relation = str(path.get("relation", "") or "").strip()
            weight = float(path.get("weight", 0.5) or 0.5)
            if not source_name and not target_name:
                continue
            content = f"{source_name} {relation} {target_name}".strip()
            doc_id = f"kg_path_{rank}_{hash(content) & 0xFFFFFFFF}"
            pipeline_results.append(PipelineResult(
                doc_id=doc_id,
                content=content,
                score=weight,
                source=RetrievalSource.KNOWLEDGE_GRAPH.value,
                metadata={
                    "kg_type": "path",
                    "source_entity": source_name,
                    "target_entity": target_name,
                    "relation": relation,
                    "extracted_entities": extracted_entities,
                }
            ))
            if len(pipeline_results) >= top_k:
                break

        # 若无路径但有相关实体，将相关实体作为补充结果
        if not pipeline_results and related_entities:
            entity_text = " ".join(str(e) for e in related_entities if e)
            if entity_text.strip():
                pipeline_results.append(PipelineResult(
                    doc_id=f"kg_entities_{hash(entity_text) & 0xFFFFFFFF}",
                    content=entity_text,
                    score=0.4,
                    source=RetrievalSource.KNOWLEDGE_GRAPH.value,
                    metadata={
                        "kg_type": "related_entities",
                        "extracted_entities": extracted_entities,
                        "related_entities": related_entities,
                    }
                ))

        return pipeline_results

    def _rrf_fusion(self, all_results: Dict[str, List[PipelineResult]], top_k: int) -> List[PipelineResult]:
        """RRF融合多路检索结果，带自适应权重和去重"""
        source_weights = {
            "knowledge_base": self.config.keyword_weight,
            "vector": self.config.vector_weight,
            "bm25": self.config.bm25_weight,
            "knowledge_graph": self.config.kg_weight,
        }

        source_counts = {s: len(r) for s, r in all_results.items() if r}
        total_results = sum(source_counts.values())
        if total_results > 0:
            for source in source_counts:
                if source in source_weights and source_counts[source] > 0:
                    density = source_counts[source] / total_results
                    if density > 0.6:
                        source_weights[source] *= 0.8

        doc_scores: Dict[str, float] = {}
        doc_items: Dict[str, PipelineResult] = {}
        doc_sources: Dict[str, set] = {}
        doc_source_scores: Dict[str, Dict[str, float]] = {}

        for source, results in all_results.items():
            weight = source_weights.get(source, 0.3)
            for rank, result in enumerate(results):
                rrf_score = weight / (self.config.rrf_k + rank + 1)
                doc_sources.setdefault(result.doc_id, set()).add(source)
                doc_source_scores.setdefault(result.doc_id, {})[source] = result.score
                if result.doc_id in doc_scores:
                    doc_scores[result.doc_id] += rrf_score
                    existing = doc_items[result.doc_id]
                    if result.score > existing.score:
                        doc_items[result.doc_id] = result
                else:
                    doc_scores[result.doc_id] = rrf_score
                    doc_items[result.doc_id] = result

        sorted_docs = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)

        fused = []
        seen_content = set()
        for doc_id, score in sorted_docs[:top_k * 3]:
            if doc_id in doc_items:
                result = doc_items[doc_id]
                content_key = result.content[:100] if result.content else doc_id
                if content_key in seen_content:
                    continue
                seen_content.add(content_key)
                result.score = score
                result.source = "hybrid"
                result.metadata = dict(result.metadata or {})
                result.metadata["retrieval_sources"] = sorted(doc_sources.get(doc_id, set()))
                result.metadata["source_scores"] = dict(doc_source_scores.get(doc_id, {}))
                fused.append(result)
                if len(fused) >= top_k * 2:
                    break

        return fused

    def _rerank(self, query: str, results: List[PipelineResult], top_k: int) -> List[PipelineResult]:
        """重排序"""
        if not results or not self.reranker:
            return results

        try:
            # 修复 R1：Reranker 类只有 rerank 方法，没有 predict 方法
            # 原代码调用 predict 导致 rerank 从未生效
            if hasattr(self.reranker, 'rerank'):
                # Reranker.rerank 内部会处理 CrossEncoder 和 simple_rerank 两种模式
                reranked = self.reranker.rerank(query, results, top_k)
                if reranked:
                    return reranked
        except Exception as e:
            logger.warning(f"重排序失败: {e}")

        return results

    def _post_filter(self, results: List[PipelineResult]) -> List[PipelineResult]:
        """后过滤"""
        filtered = results

        for filter_fn in self._filters:
            try:
                filtered = filter_fn(filtered)
            except Exception as e:
                logger.warning(f"过滤器执行失败: {e}")

        return [r for r in filtered if r.score >= self.config.score_threshold]

    def _semantic_dedup(self, results: List[PipelineResult]) -> List[PipelineResult]:
        """
        语义去重 - 消除高度相似的重复结果
        
        使用jieba分词后的词级Jaccard相似度作为语义近似，
        对中文文本效果远优于字符级n-gram，同时保持低延迟
        """
        if len(results) <= 1:
            return results
        
        deduped = [results[0]]
        deduped_word_sets = [self._tokenize(results[0].content)]
        
        for result in results[1:]:
            is_duplicate = False
            content_words = self._tokenize(result.content)
            
            if not content_words:
                deduped.append(result)
                deduped_word_sets.append(content_words)
                continue
            
            for i, existing_words in enumerate(deduped_word_sets):
                if not existing_words:
                    continue
                
                intersection = content_words & existing_words
                union = content_words | existing_words
                
                if union:
                    jaccard = len(intersection) / len(union)
                    if jaccard > self.config.dedup_similarity_threshold:
                        is_duplicate = True
                        break
            
            if not is_duplicate:
                deduped.append(result)
                deduped_word_sets.append(content_words)
        
        removed = len(results) - len(deduped)
        if removed > 0:
            logger.debug(f"语义去重移除 {removed} 个相似结果")
        
        return deduped
    
    def _tokenize(self, text: str) -> set:
        """对文本进行分词，返回词集合"""
        if not text:
            return set()
        try:
            import jieba
            words = set(w.strip() for w in jieba.cut(text) if len(w.strip()) >= 2)
            return words
        except Exception:
            return set(text.split())

    def _relevance_filter(self, query: str, results: List[PipelineResult]) -> List[PipelineResult]:
        """
        相关性过滤 - 过滤与查询不相关的结果

        使用关键词覆盖率作为相关性指标
        """
        if not results or not query:
            return results

        query_words = set()
        try:
            import jieba
            query_words = set(w.lower() for w in jieba.cut(query) if len(w.strip()) >= 2)
        except Exception:
            query_words = set(query.lower().split())

        if not query_words:
            return results

        filtered = []
        for result in results:
            content_lower = result.content.lower()

            keyword_hit = sum(1 for w in query_words if w in content_lower)
            keyword_coverage = keyword_hit / len(query_words) if query_words else 0

            if keyword_coverage >= self.config.relevance_threshold or result.score > 0.5:
                result.metadata["relevance"] = round(keyword_coverage, 3)
                filtered.append(result)
            else:
                logger.debug(f"过滤低相关结果: relevance={keyword_coverage:.3f}, content={(result.content or '')[:50]}...")

        removed = len(results) - len(filtered)
        if removed > 0:
            logger.debug(f"相关性过滤移除 {removed} 个不相关结果")

        if not filtered:
            sorted_results = sorted(results, key=lambda r: r.score, reverse=True)
            filtered = sorted_results[:max(3, len(sorted_results) // 2)]
            logger.debug(f"相关性过滤后无结果，保留得分最高的 {len(filtered)} 个结果")

        return filtered


_pipeline: Optional[UnifiedRetrievalPipeline] = None
_pipeline_lock = threading.Lock()


def get_retrieval_pipeline(**kwargs) -> UnifiedRetrievalPipeline:
    """获取统一检索管道单例"""
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                _pipeline = UnifiedRetrievalPipeline(**kwargs)
    return _pipeline
