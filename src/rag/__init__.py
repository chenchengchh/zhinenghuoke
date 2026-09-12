"""
RAG模块初始化文件
"""
from .embedding_service import EmbeddingService, EmbeddingConfig, LocalEmbedding, DashScopeEmbedding
from .vector_store import ChromaVectorStore, ChromaConfig
from .graph_extraction_adapter import KnowledgeExtractor, Entity, Relation, EntityType, RelationType
from .enterprise_service import EnterpriseService, Enterprise, EnterpriseConfig, TenantContext, DataIsolationManager
from .context_query_rewriter import ContextAwareQueryRewriter, get_query_rewriter
from .coreference_resolver import EnhancedCoreferenceResolver, get_coreference_resolver
from .rag_evaluator import (
    RAGASEvaluator,
    EvaluationResult,
    EvaluationMetric,
    FaithfulnessEvaluator,
    AnswerRelevanceEvaluator,
    ContextRelevanceEvaluator,
    HallucinationDetector,
    RAGMetricsCollector,
    SimpleRAGEvaluator,
    create_ragas_evaluator,
    create_metrics_collector,
    create_simple_evaluator,
)
from .unified_pipeline import UnifiedRetrievalPipeline, PipelineConfig, PipelineResult, get_retrieval_pipeline

__all__ = [
    'EmbeddingService', 'EmbeddingConfig', 'LocalEmbedding', 'DashScopeEmbedding',
    'ChromaVectorStore', 'ChromaConfig',
    'KnowledgeExtractor', 'Entity', 'Relation', 'EntityType', 'RelationType',
    'EnterpriseService', 'Enterprise', 'EnterpriseConfig', 'TenantContext', 'DataIsolationManager',
    'ContextAwareQueryRewriter', 'get_query_rewriter',
    'EnhancedCoreferenceResolver', 'get_coreference_resolver',
    'RAGASEvaluator', 'EvaluationResult', 'EvaluationMetric',
    'FaithfulnessEvaluator', 'AnswerRelevanceEvaluator',
    'ContextRelevanceEvaluator', 'HallucinationDetector',
    'RAGMetricsCollector', 'SimpleRAGEvaluator',
    'create_ragas_evaluator', 'create_metrics_collector', 'create_simple_evaluator',
    'UnifiedRetrievalPipeline', 'PipelineConfig', 'PipelineResult', 'get_retrieval_pipeline',
]
