"""
统一RAG类型定义

整合所有RAG相关的数据模型，避免重复定义
"""
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from enum import Enum


class RAGStage(Enum):
    """RAG处理阶段"""
    INTENT_RECOGNITION = "intent_recognition"
    MODULAR_RAG = "modular_rag"
    AGENTIC_RAG = "agentic_rag"
    REPLY_GENERATION = "reply_generation"
    QUALITY_CHECK = "quality_check"


class RAGStrategy(Enum):
    """RAG策略类型"""
    DIRECT_REPLY = "direct_reply"
    SIMPLE_RETRIEVAL = "simple_retrieval"
    HYBRID_SEARCH = "hybrid_search"
    MULTI_HOP_REASONING = "multi_hop_reasoning"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    LLM_GENERATION = "llm_generation"


class RetrievalDecision(Enum):
    """检索决策类型"""
    NEED_RETRIEVE = "need_retrieve"
    NO_RETRIEVE = "no_retrieve"
    UNCERTAIN = "uncertain"


class RetrievalType(Enum):
    """检索类型"""
    VECTOR = "vector"
    KEYWORD = "keyword"
    HYBRID = "hybrid"
    GRAPH = "graph"


@dataclass
class RoutingDecision:
    """统一路由决策结构，用于记录主链路由与后续 agent routing 扩展。"""
    route_name: str
    reason: str
    confidence: float = 0.0
    stage: str = "process_message"
    selected_strategy: str = ""
    fallback_route: str = ""
    executor: str = ""
    handoff_reason: str = ""
    route_scores: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_name": self.route_name,
            "reason": self.reason,
            "confidence": self.confidence,
            "stage": self.stage,
            "selected_strategy": self.selected_strategy,
            "fallback_route": self.fallback_route,
            "executor": self.executor,
            "handoff_reason": self.handoff_reason,
            "route_scores": self.route_scores,
            "metadata": self.metadata,
        }


@dataclass
class RAGResult:
    """
    统一RAG结果数据模型
    
    整合了 unified_rag_service.py 和 enhanced_rag.py 中的定义
    """
    answer: str
    strategy: RAGStrategy = RAGStrategy.SIMPLE_RETRIEVAL
    stage: RAGStage = RAGStage.MODULAR_RAG
    confidence: float = 0.0
    retrieved: bool = False
    sources: List[Dict] = field(default_factory=list)
    reasoning_chain: List[str] = field(default_factory=list)
    processing_time: float = 0.0
    metadata: Dict = field(default_factory=dict)
    
    # 兼容字段
    reasoning: str = ""
    matched_question: str = ""
    retrieval_type: str = "hybrid"
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "answer": self.answer,
            "strategy": self.strategy.value,
            "stage": self.stage.value,
            "confidence": self.confidence,
            "retrieved": self.retrieved,
            "sources": self.sources,
            "reasoning_chain": self.reasoning_chain,
            "processing_time": self.processing_time,
            "metadata": self.metadata,
            "reasoning": self.reasoning,
            "matched_question": self.matched_question,
            "retrieval_type": self.retrieval_type
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'RAGResult':
        """从字典创建"""
        return cls(
            answer=data.get("answer", ""),
            strategy=RAGStrategy(data.get("strategy", "simple_retrieval")),
            stage=RAGStage(data.get("stage", "modular_rag")),
            confidence=data.get("confidence", 0.0),
            retrieved=data.get("retrieved", False),
            sources=data.get("sources", []),
            reasoning_chain=data.get("reasoning_chain", []),
            processing_time=data.get("processing_time", 0.0),
            metadata=data.get("metadata", {}),
            reasoning=data.get("reasoning", ""),
            matched_question=data.get("matched_question", ""),
            retrieval_type=data.get("retrieval_type", "hybrid")
        )


@dataclass
class RetrievalResult:
    """
    统一检索结果数据模型
    
    整合了 rag_service.py 和 rag_retriever.py 中的定义
    """
    chunk_id: str = ""
    document_id: str = ""
    document_name: str = ""
    content: str = ""
    score: float = 0.0
    source: str = ""
    metadata: Dict = field(default_factory=dict)
    retrieval_type: str = "hybrid"
    
    # 兼容字段 (rag_retriever.py)
    doc_id: str = ""
    
    def __post_init__(self):
        """初始化后处理"""
        if not self.doc_id:
            self.doc_id = self.chunk_id or self.document_id
        if not self.chunk_id:
            self.chunk_id = self.doc_id
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "content": self.content,
            "score": self.score,
            "source": self.source,
            "metadata": self.metadata,
            "retrieval_type": self.retrieval_type,
            "doc_id": self.doc_id
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'RetrievalResult':
        """从字典创建"""
        return cls(
            chunk_id=data.get("chunk_id", data.get("doc_id", "")),
            document_id=data.get("document_id", ""),
            document_name=data.get("document_name", ""),
            content=data.get("content", ""),
            score=data.get("score", 0.0),
            source=data.get("source", ""),
            metadata=data.get("metadata", {}),
            retrieval_type=data.get("retrieval_type", "hybrid"),
            doc_id=data.get("doc_id", data.get("chunk_id", ""))
        )


@dataclass
class QueryAnalysisResult:
    """查询分析结果"""
    query: str
    category: str = "general"
    complexity: float = 0.5
    intent: str = ""
    keywords: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    need_retrieval: bool = True
    suggested_strategy: RAGStrategy = RAGStrategy.SIMPLE_RETRIEVAL
    confidence: float = 0.0
    reasoning: str = ""


@dataclass
class RAGContext:
    """RAG处理上下文"""
    query: str
    enterprise_id: str = "default"
    conversation_history: List[Dict] = field(default_factory=list)
    customer_data: Dict = field(default_factory=dict)
    intent_result: Optional[QueryAnalysisResult] = None
    retrieval_results: List[RetrievalResult] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)


class QueryCategory(Enum):
    """查询类别"""
    GREETING = "greeting"
    THANKS = "thanks"
    GOODBYE = "goodbye"
    CHITCHAT = "chitchat"
    PRODUCT = "product"
    PRICE = "price"
    USAGE = "usage"
    BUSINESS = "business"
    SUPPORT = "support"
    COOPERATION = "cooperation"
    COMPLAINT = "complaint"
    GENERAL = "general"


class StrategyType(Enum):
    """策略类型"""
    SIMPLE = "simple"
    HYBRID = "hybrid"
    MULTI_HOP = "multi_hop"
    LLM_GENERATE = "llm_generate"
    DIRECT_REPLY = "direct_reply"


class AgentAction(Enum):
    """Agent动作类型"""
    RETRIEVE = "retrieve"
    KG_QUERY = "kg_query"
    GENERATE = "generate"
    REFINE = "refine"
    VERIFY = "verify"
    ANSWER = "answer"
    ASK_CLARIFY = "ask_clarify"


@dataclass
class RetrievalDecisionResult:
    """检索决策结果"""
    decision: RetrievalDecision
    reason: str
    confidence: float
    suggested_strategy: StrategyType
    matched_pattern: str = ""


@dataclass
class AgentThought:
    """Agent思考步骤"""
    step: int
    thought: str
    action: AgentAction
    action_input: Dict[str, Any] = field(default_factory=dict)
    observation: str = ""
    is_final: bool = False


@dataclass
class AgentResult:
    """Agent处理结果"""
    answer: str
    strategy: str = "agentic_rag"
    retrieved: bool = False
    sources: List[Dict] = field(default_factory=list)
    confidence: float = 0.0
    processing_time: float = 0.0
    query_analysis: Optional[QueryAnalysisResult] = None
    retrieval_decision: Optional[RetrievalDecisionResult] = None
    reasoning_chain: List[AgentThought] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    needs_human: bool = False
    self_correction_count: int = 0
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "answer": self.answer,
            "strategy": self.strategy,
            "retrieved": self.retrieved,
            "sources": self.sources,
            "confidence": self.confidence,
            "processing_time": self.processing_time,
            "reasoning_chain": [
                {
                    "step": t.step,
                    "thought": t.thought,
                    "action": t.action.value,
                    "observation": t.observation
                }
                for t in self.reasoning_chain
            ],
            "tools_used": self.tools_used,
            "needs_human": self.needs_human,
            "self_correction_count": self.self_correction_count,
            "metadata": self.metadata
        }


@dataclass
class TokenAnalysisResult:
    """分词分析结果"""
    original_text: str
    tokens: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    stopwords_removed: List[str] = field(default_factory=list)
    pos_tags: List[Tuple[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    expanded_tokens: List[str] = field(default_factory=list)
