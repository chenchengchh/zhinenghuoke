"""
知识服务适配器 (Knowledge Service Adapter)

统一入口，整合以下冗余知识库服务模块：
  - UnifiedKnowledgeService       → 核心知识检索 / RAG 混合搜索（主服务）
  - KnowledgeGraphService         → 实体关系图谱 / 推理查询（可选插件）
  - KnowledgeBaseManager          → 遗留 CRUD 层（已由 UnifiedKnowledgeService 覆盖）
  - KnowledgeNormalizationService → 规范化配置（仅内部使用，不对外暴露）
  - KnowledgeUnderstandingService → 文档理解 / 知识画像生成（按需加载）

设计原则：
  1. 单一入口 —— 外部模块只需 from .knowledge_service_adapter import get_knowledge_service
  2. 委托模式 —— 核心操作委托给 UnifiedKnowledgeService，图谱操作委托给 KnowledgeGraphService
  3. 向后兼容 —— 保留旧接口标记 @deprecated，不删除任何原有代码
  4. 懒加载     —— 图谱服务和理解服务仅在首次调用时初始化

使用示例::

    from src.common.knowledge_service_adapter import get_knowledge_service

    ks = get_knowledge_service()

    # 核心：混合检索
    results = ks.search("产品价格", top_k=5)

    # 核心：结构化证据包
    bundle = ks.search_evidence_bundle("跟团游", top_k=5)

    # 图谱：实体关系推理
    entities = ks.query_graph_entities("产品A")

    # 文档：生成企业知识画像
    profile = ks.understand_document(enterprise_id="xxx", content="...")

迁移指南::
    旧代码                              →  新代码
    ──────────────────────────────────────────────────
    get_unified_knowledge_service()      →  get_knowledge_service()
    get_knowledge_graph_service()        →  get_knowledge_service().graph
    get_knowledge_understanding_service()→  get_knowledge_service().understanding
    KnowledgeBaseManager().search(...)   →  get_knowledge_service().search(...)
"""

import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


# ---------------------------------------------------------------------------
# 内部懒加载引用（避免循环导入 + 启动时重量级依赖）
# ---------------------------------------------------------------------------

_core_service = None
_graph_service = None
_understanding_service = None


def _get_core():
    """获取核心知识服务 (UnifiedKnowledgeService)"""
    global _core_service
    if _core_service is None:
        from .unified_knowledge_service import get_unified_knowledge_service
        _core_service = get_unified_knowledge_service()
    return _core_service


def _get_graph(enterprise_id: str = ""):
    """获取图谱服务 (KnowledgeGraphService)，懒加载"""
    global _graph_service
    if _graph_service is None:
        from .knowledge_graph import get_knowledge_graph_service
        _graph_service = get_knowledge_graph_service(enterprise_id=enterprise_id)
    return _graph_service


def _get_understanding():
    """获取文档理解服务 (KnowledgeUnderstandingService)，懒加载"""
    global _understanding_service
    if _understanding_service is None:
        from src.rag.knowledge_understanding_service import (
            get_knowledge_understanding_service,
        )
        _understanding_service = get_knowledge_understanding_service()
    return _understanding_service


# ---------------------------------------------------------------------------
# 适配器类
# ---------------------------------------------------------------------------

class KnowledgeServiceAdapter:
    """
    知识服务统一适配器

    将分散在多个模块中的知识操作聚合为单一对象，
    内部通过委托模式调用各子服务的实际实现。

    Attributes:
        core: UnifiedKnowledgeService 实例（核心检索/CRUD/RAG）
        graph: KnowledgeGraphService 实例（实体关系图谱，可选）
        understanding: KnowledgeUnderstandingService 实例（文档理解，可选）
    """

    def __init__(
        self,
        enterprise_id: str = "",
        enable_graph: bool = True,
        enable_understanding: bool = True,
    ):
        """
        Args:
            enterprise_id: 企业ID，用于图谱和多租户隔离
            enable_graph: 是否启用知识图谱插件
            enable_understanding: 是否启用文档理解服务
        """
        self._enterprise_id = enterprise_id
        self._enable_graph = enable_graph
        self._enable_understanding = enable_understanding

        # 核心服务立即初始化（它是必须的）
        self._core = _get_core()

        # 可选插件延迟到属性访问时初始化
        self._graph = None
        self._understanding = None

    # ===================================================================
    # 属性访问 — 懒加载可选插件
    # ===================================================================

    @property
    def core(self):
        """核心知识服务 (UnifiedKnowledgeService) 实例"""
        return self._core

    @property
    def graph(self):
        """
        知识图谱服务 (KnowledgeGraphService) 实例

        首次访问时自动初始化。若禁用则返回 None。
        """
        if not self._enable_graph:
            return None
        if self._graph is None:
            self._graph = _get_graph(enterprise_id=self._enterprise_id)
        return self._graph

    @property
    def understanding(self):
        """
        文档理解服务 (KnowledgeUnderstandingService) 实例

        首次访问时自动初始化。若禁用则返回 None。
        """
        if not self._enable_understanding:
            return None
        if self._understanding is None:
            self._understanding = _get_understanding()
        return self._understanding

    # ===================================================================
    # 一、核心检索 API（委托给 UnifiedKnowledgeService）
    # ===================================================================

    def search(
        self,
        query: str,
        top_k: int = 5,
        category: Optional[str] = None,
        source: str = "all",
        use_vector: bool = True,
        min_score: float = 18.0,
        business_stage: str = "",
        intent: str = "",
        enterprise_id: str = "",
        schema_id: str = "",
        allow_vector_init: bool = True,
        retrieval_options: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[Any, float]]:
        """
        混合检索知识条目

        结合向量语义检索、关键词 BM25 检索和重排序策略，
        返回最相关的知识条目及其匹配分数。

        Args:
            query: 用户查询文本
            top_k: 返回结果数量上限
            category: 分类过滤（如 "product"、"faq"）
            source: 数据源过滤 ("all" | "main" | "enterprise")
            use_vector: 是否启用向量语义检索
            min_score: 最小匹配分数阈值（低于此值的结果被过滤）
            business_stage: 业务阶段（影响排序偏置）
            intent: 意图类型（影响检索策略选择）
            enterprise_id: 企业ID（多租户隔离）
            schema_id: 行业方案ID（影响领域术语识别）
            allow_vector_init: 是否允许冷启动向量存储
            retrieval_options: 高级检索选项字典

        Returns:
            List[Tuple[KnowledgeItem, float]]: (知识条目, 匹配分数) 列表，按分数降序
        """
        return self._core.search(
            query=query,
            top_k=top_k,
            category=category,
            source=source,
            use_vector=use_vector,
            min_score=min_score,
            business_stage=business_stage,
            intent=intent,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
            allow_vector_init=allow_vector_init,
            retrieval_options=retrieval_options,
        )

    def search_evidence_bundle(
        self,
        query: str,
        top_k: int = 5,
        source: str = "all",
        business_stage: str = "",
        intent: str = "",
        enterprise_id: str = "",
        schema_id: str = "",
    ):
        """
        结构化证据包检索

        按线路/对比主题聚合多维度结构化证据，
        用于 LLM 上下文增强和主回复链路。

        Args:
            query: 用户查询文本
            top_k: 候选结果数量
            source: 数据源过滤
            business_stage: 业务阶段
            intent: 意图类型
            enterprise_id: 企业ID
            schema_id: 行业方案ID

        Returns:
            StructuredEvidenceBundle | None: 结构化证据包，无匹配时返回 None
        """
        return self._core.search_evidence_bundle(
            query=query,
            top_k=top_k,
            source=source,
            business_stage=business_stage,
            intent=intent,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )

    def get_all_items(self) -> List[Any]:
        """获取全部知识条目列表"""
        return self._core.get_all_items()

    def get_knowledge_items(self) -> List[Any]:
        """获取全部知识条目（别名，兼容旧接口）"""
        return self._core.knowledge_items

    def warmup_retrieval_chain(
        self, force: bool = False, preload_reranker: bool = True
    ) -> Dict[str, Any]:
        """
        后台预热语义检索链

        避免首条真实消息承担向量/reranker 冷启动开销。

        Args:
            force: 强制重新预热
            preload_reranker: 是否预加载 reranker 模型

        Returns:
            Dict: 包含 success/status/warmed/error 等字段的状态信息
        """
        return self._core.warmup_retrieval_chain(
            force=force, preload_reranker=preload_reranker
        )

    def resync_to_vector(self, force_full: bool = False) -> Dict[str, Any]:
        """
        将知识条目同步到向量数据库

        Args:
            force_full: 是否全量重建索引

        Returns:
            Dict: 同步结果状态
        """
        return self._core.resync_to_vector(force_full=force_full)

    def refresh_if_external_change(self, resync_vector: bool = False):
        """检测并响应知识库文件的外部变更"""
        self._core._refresh_if_external_change(resync_vector=resync_vector)

    # ===================================================================
    # 二、知识图谱 API（委托给 KnowledgeGraphService）
    # ===================================================================

    def build_graph_from_knowledge(self, knowledge_items: List[Any] = None) -> int:
        """
        从知识条目构建/更新实体关系图谱

        Args:
            knowledge_items: 知识条目列表；为 None 时自动从核心服务取全部条目

        Returns:
            int: 构建或新增的实体数量

        Raises:
            RuntimeError: 图谱服务未启用时调用
        """
        graph = self.graph
        if graph is None:
            raise RuntimeError(
                "知识图谱服务未启用，请在初始化时设置 enable_graph=True"
            )
        items = knowledge_items if knowledge_items is not None else self._core.knowledge_items
        return graph.build_graph_from_knowledge(items)

    def query_with_reasoning(
        self,
        query: str,
        top_k: int = 5,
        max_depth: int = 3,
    ) -> List[Any]:
        """
        带推理的图谱查询

        在实体关系图中执行多跳推理，返回与查询相关的实体路径。

        Args:
            query: 查询文本
            top_k: 返回结果数量
            max_depth: 最大推理深度

        Returns:
            List[GraphPath]: 推理路径列表

        Raises:
            RuntimeError: 图谱服务未启用时调用
        """
        graph = self.graph
        if graph is None:
            raise RuntimeError(
                "知识图谱服务未启用，请在初始化时设置 enable_graph=True"
            )
        return graph.query_with_reasoning(query=query, top_k=top_k, max_depth=max_depth)

    def query_related(self, entity_name: str, relation_type: str = None) -> List[Any]:
        """
        查询与指定实体相关的其他实体

        Args:
            entity_name: 实体名称
            relation_type: 关系类型过滤（可选）

        Returns:
            List[Entity]: 相关实体列表

        Raises:
            RuntimeError: 图谱服务未启用时调用
        """
        graph = self.graph
        if graph is None:
            raise RuntimeError(
                "知识图谱服务未启用，请在初始化时设置 enable_graph=True"
            )
        return graph.query_related(entity_name=entity_name, relation_type=relation_type)

    def get_graph_stats(self) -> Dict[str, Any]:
        """
        获取图谱统计信息

        Returns:
            Dict: 包含 entity_count / relation_count 等统计字段

        Raises:
            RuntimeError: 图谱服务未启用时调用
        """
        graph = self.graph
        if graph is None:
            raise RuntimeError(
                "知识图谱服务未启用，请在初始化时设置 enable_graph=True"
            )
        return graph.get_graph_stats()

    # ===================================================================
    # 三、文档理解 API（委托给 KnowledgeUnderstandingService）
    # ===================================================================

    def understand_document(
        self,
        enterprise_id: str,
        content: str,
        document_name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """
        从原始文档内容生成企业级知识画像

        上传文档后调用此方法，生成结构化的 DomainProfile，
        作为后续意图识别、检索路由和回复约束的基础产物。

        Args:
            enterprise_id: 企业ID
            content: 文档原始文本内容
            document_name: 文件名（用于提示词上下文）
            metadata: 附加元数据

        Returns:
            DomainProfile | None: 生成的知识画像，失败时返回 None

        Raises:
            RuntimeError: 文档理解服务未启用时调用
        """
        svc = self.understanding
        if svc is None:
            raise RuntimeError(
                "文档理解服务未启用，请在初始化时设置 enable_understanding=True"
            )
        return svc.build_and_store_profile(
            enterprise_id=enterprise_id,
            content=content,
            document_name=document_name,
            metadata=metadata or {},
        )

    # ===================================================================
    # 四、便捷方法：一站式操作
    # ===================================================================

    def search_with_graph_enhancement(
        self,
        query: str,
        top_k: int = 5,
        enterprise_id: str = "",
        **search_kwargs,
    ) -> Dict[str, Any]:
        """
        检索 + 图谱增强的一站式方法

        同时执行文本检索和图谱推理，合并返回综合结果。

        Args:
            query: 查询文本
            top_k: 结果数量
            enterprise_id: 企业ID
            **search_kwargs: 传递给 search() 的额外参数

        Returns:
            Dict: {
                "retrieval_results": [...],    # 文本检索结果
                "graph_results": [...],        # 图谱推理结果（可能为空）
                "graph_stats": {...},          # 图谱统计
                "has_graph": bool,             # 图谱是否可用
            }
        """
        # 1. 核心检索
        retrieval_results = self.search(query, top_k=top_k, enterprise_id=enterprise_id, **search_kwargs)

        # 2. 图谱增强（如果可用）
        graph_results = []
        graph_stats = {}
        has_graph = False

        if self._enable_graph and self.graph is not None:
            try:
                graph_results = self.query_with_reasoning(query, top_k=max(top_k // 2, 2))
                graph_stats = self.get_graph_stats()
                has_graph = True
            except Exception as e:
                logger.warning(f"图谱增强查询失败（非致命）: {e}")

        return {
            "retrieval_results": retrieval_results,
            "graph_results": graph_results,
            "graph_stats": graph_stats,
            "has_graph": has_graph,
        }


# ---------------------------------------------------------------------------
# 工厂函数（全局单例风格，与现有工厂函数保持一致）
# ---------------------------------------------------------------------------

_adapter_instance: Optional[KnowledgeServiceAdapter] = None
_adapter_lock_initialized = False


def get_knowledge_service(
    enterprise_id: str = "",
    enable_graph: bool = True,
    enable_understanding: bool = True,
    reset: bool = False,
) -> KnowledgeServiceAdapter:
    """
    获取知识服务适配器实例（推荐的全局入口）

    这是整个知识服务体系的唯一推荐入口点。
    所有外部模块应优先使用此函数获取服务实例。

    Args:
        enterprise_id: 企业ID，用于图谱多租户隔离
        enable_graph: 是否启用知识图谱插件（默认启用）
        enable_understanding: 是否启用文档理解服务（默认启用）
        reset: 是否强制重置实例

    Returns:
        KnowledgeServiceAdapter: 统一知识服务适配器实例

    Example::

        from src.common.knowledge_service_adapter import get_knowledge_service

        ks = get_knowledge_service()
        results = ks.search("产品价格", top_k=5)
    """
    global _adapter_instance, _adapter_lock_initialized

    if reset:
        _adapter_instance = None

    if _adapter_instance is None:
        _adapter_instance = KnowledgeServiceAdapter(
            enterprise_id=enterprise_id,
            enable_graph=enable_graph,
            enable_understanding=enable_understanding,
        )
        logger.info("知识服务适配器初始化完成")

    return _adapter_instance


def reset_knowledge_service():
    """重置知识服务适配器实例（测试/热重载场景）"""
    global _adapter_instance
    _adapter_instance = None
    logger.info("知识服务适配器实例已重置")


# ===========================================================================
# 向后兼容：已废弃的旧接口导出（保持可用但触发 DeprecationWarning）
# ===========================================================================

def _deprecated_get_unified_knowledge_service(data_path=None, reset=False):
    """
    [已废弃] 请使用 get_knowledge_service() 替代

    此函数保留仅为向后兼容，将在未来版本中移除。
    """
    warnings.warn(
        "get_unified_knowledge_service() 已废弃，请使用 get_knowledge_service() 替代。"
        " 旧接口将在未来版本移除。",
        DeprecationWarning,
        stacklevel=2,
    )
    from .unified_knowledge_service import get_unified_knowledge_service as _original
    return _original(data_path=data_path, reset=reset)


def _deprecated_get_knowledge_graph_service(enterprise_id=""):
    """
    [已废弃] 请使用 get_knowledge_service().graph 替代

    此函数保留仅为向后兼容，将在未来版本中移除。
    """
    warnings.warn(
        "get_knowledge_graph_service() 已废弃，请使用 get_knowledge_service().graph 替代。"
        " 旧接口将在未来版本移除。",
        DeprecationWarning,
        stacklevel=2,
    )
    from .knowledge_graph import get_knowledge_graph_service as _original
    return _original(enterprise_id=enterprise_id)


def _deprecated_get_knowledge_understanding_service(llm_service=None, domain_profile_service=None):
    """
    [已废弃] 请使用 get_knowledge_service().understanding 替代

    此函数保留仅为向后兼容，将在未来版本中移除。
    """
    warnings.warn(
        "get_knowledge_understanding_service() 已废弃，请使用 get_knowledge_service().understanding 替代。"
        " 旧接口将在未来版本移除。",
        DeprecationWarning,
        stacklevel=2,
    )
    from src.rag.knowledge_understanding_service import (
        get_knowledge_understanding_service as _original,
    )
    return _original(llm_service=llm_service, domain_profile_service=domain_profile_service)
