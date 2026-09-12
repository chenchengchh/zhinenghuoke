"""
统一类型定义模块
"""
from .intent import IntentType, SentimentType, UrgencyLevel
from .conversion import (
    ConversionStage, NextBestAction, CtaMode, ConversionSignals, ReplyObjective,
)
from .rag import (
    RAGStage, RAGStrategy, RetrievalDecision, RetrievalType,
    RAGResult, RetrievalResult, QueryAnalysisResult, RAGContext,
    QueryCategory, StrategyType, AgentAction,
    RetrievalDecisionResult, AgentThought, AgentResult, TokenAnalysisResult,
    RoutingDecision,
)
from .knowledge import KnowledgeCategory, KnowledgeItem
from .knowledge_profile import DomainProfile, ProductProfile, PolicyRule, FAQSignal
from .validation import ValidationStatus, ValidationResult
from .reply_eligibility import (
    EligibilityAction,
    EvidenceLevel,
    HandoffLevel,
    ReplyEligibilityDecision,
    ReplyEligibilityInput,
    ReplyEligibilityPolicy,
)

__all__ = [
    'IntentType', 'SentimentType', 'UrgencyLevel',
    'ConversionStage', 'NextBestAction', 'CtaMode', 'ConversionSignals', 'ReplyObjective',
    'RAGStage', 'RAGStrategy', 'RetrievalDecision', 'RetrievalType',
    'RAGResult', 'RetrievalResult', 'QueryAnalysisResult', 'RAGContext',
    'QueryCategory', 'StrategyType', 'AgentAction',
    'RetrievalDecisionResult', 'AgentThought', 'AgentResult', 'TokenAnalysisResult',
    'RoutingDecision',
    'KnowledgeCategory', 'KnowledgeItem',
    'DomainProfile', 'ProductProfile', 'PolicyRule', 'FAQSignal',
    'ValidationStatus', 'ValidationResult',
    'EligibilityAction', 'EvidenceLevel', 'HandoffLevel',
    'ReplyEligibilityDecision', 'ReplyEligibilityInput', 'ReplyEligibilityPolicy',
]
