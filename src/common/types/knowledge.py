"""
统一知识类型定义

整合所有知识相关的数据模型，避免重复定义
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime
from enum import Enum


class KnowledgeCategory(Enum):
    """知识分类（行业扩展分类标记为行业专属，其他行业可通过 register_industry_categories 注册）"""
    PRICE = "price"
    SERVICE = "service"
    PRODUCT = "product"
    PROMOTION = "promotion"
    AFTER_SALE = "after_sale"
    COOPERATION = "cooperation"
    FAQ = "faq"
    POLICY = "policy"
    OTHER = "other"


_INDUSTRY_CATEGORIES: Dict[str, str] = {}


def register_industry_categories(categories: Dict[str, str]):
    _INDUSTRY_CATEGORIES.update(categories)


def get_category_value(category_name: str) -> str:
    try:
        return KnowledgeCategory(category_name).value
    except ValueError:
        return _INDUSTRY_CATEGORIES.get(category_name, KnowledgeCategory.OTHER.value)


def is_industry_category(category_name: str) -> bool:
    return category_name in _INDUSTRY_CATEGORIES


class KnowledgeStatus(Enum):
    """知识状态"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING = "pending"
    ARCHIVED = "archived"


@dataclass
class KnowledgeItem:
    """
    统一知识条目数据模型
    
    整合了 knowledge_base.py, llm_service.py, unified_knowledge_service.py 中的定义
    """
    id: str = ""
    question: str = ""
    answer: str = ""
    title: str = ""
    content: str = ""
    category: str = KnowledgeCategory.OTHER.value
    tags: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    reply_templates: List[str] = field(default_factory=list)
    priority: int = 0
    enabled: bool = True
    use_count: int = 0
    last_used_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    score: float = 0.0
    source: str = "main"
    domain: str = ""
    topic: str = ""
    status: str = KnowledgeStatus.ACTIVE.value
    effectiveness_score: float = 0.0
    enterprise_id: str = ""
    schema_id: str = ""
    version: int = 1
    parent_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "id": self.id,
            "question": self.question,
            "answer": self.answer,
            "title": self.title,
            "content": self.content,
            "category": self.category,
            "tags": self.tags,
            "keywords": self.keywords,
            "aliases": self.aliases,
            "reply_templates": self.reply_templates,
            "priority": self.priority,
            "enabled": self.enabled,
            "use_count": self.use_count,
            "last_used_at": self.last_used_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "score": self.score,
            "source": self.source,
            "domain": self.domain,
            "topic": self.topic,
            "status": self.status,
            "effectiveness_score": self.effectiveness_score,
            "enterprise_id": self.enterprise_id,
            "schema_id": self.schema_id,
            "version": self.version,
            "parent_id": self.parent_id,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'KnowledgeItem':
        """从字典创建"""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered_data)


@dataclass
class ExtractedKnowledge:
    """
    提取的知识结构
    
    整合了 self_learning_rag.py 和 llm_knowledge_extractor.py 中的定义
    """
    question: str
    answer: str
    source: str = "extraction"
    confidence: float = 0.8
    keywords: List[str] = field(default_factory=list)
    category: str = "general"
    tags: List[str] = field(default_factory=list)
    related_items: List[str] = field(default_factory=list)
    validation_status: str = "pending"
    validated_by: Optional[str] = None
    validated_at: Optional[datetime] = None
    id: str = ""
    created_at: Optional[datetime] = None
    suggested_intent: str = ""
    
    # 质量评分
    quality_score: float = 0.0
    completeness_score: float = 0.0
    accuracy_score: float = 0.0
    relevance_score: float = 0.0
    
    # 元数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    extraction_time: datetime = field(default_factory=datetime.now)
    reasoning: str = ""
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "question": self.question,
            "answer": self.answer,
            "source": self.source,
            "confidence": self.confidence,
            "keywords": self.keywords,
            "category": self.category,
            "tags": self.tags,
            "related_items": self.related_items,
            "validation_status": self.validation_status,
            "validated_by": self.validated_by,
            "validated_at": self.validated_at.isoformat() if self.validated_at else None,
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "suggested_intent": self.suggested_intent,
            "quality_score": self.quality_score,
            "completeness_score": self.completeness_score,
            "accuracy_score": self.accuracy_score,
            "relevance_score": self.relevance_score,
            "metadata": self.metadata,
            "reasoning": self.reasoning
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'ExtractedKnowledge':
        """从字典创建"""
        return cls(
            question=data.get("question", ""),
            answer=data.get("answer", ""),
            source=data.get("source", "extraction"),
            confidence=data.get("confidence", 0.8),
            keywords=data.get("keywords", []),
            category=data.get("category", "general"),
            tags=data.get("tags", []),
            related_items=data.get("related_items", []),
            validation_status=data.get("validation_status", "pending"),
            validated_by=data.get("validated_by"),
            validated_at=data.get("validated_at"),
            id=data.get("id", ""),
            created_at=data.get("created_at"),
            suggested_intent=data.get("suggested_intent", ""),
            quality_score=data.get("quality_score", 0.0),
            completeness_score=data.get("completeness_score", 0.0),
            accuracy_score=data.get("accuracy_score", 0.0),
            relevance_score=data.get("relevance_score", 0.0),
            metadata=data.get("metadata", {}),
            reasoning=data.get("reasoning", "")
        )


@dataclass
class ValidationResult:
    """知识验证结果"""
    knowledge_id: str
    status: str = "pending"
    score: float = 0.0
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    validated_at: datetime = field(default_factory=datetime.now)


register_industry_categories({
    "attractions": "attractions",
    "food": "food",
    "transport": "transport",
    "accommodation": "accommodation",
    "itinerary": "itinerary",
    "tips": "tips",
})
