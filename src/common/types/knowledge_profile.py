"""
通用知识画像类型定义
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List


@dataclass
class ProductProfile:
    name: str = ""
    aliases: List[str] = field(default_factory=list)
    summary: str = ""
    features: List[str] = field(default_factory=list)
    price_notes: List[str] = field(default_factory=list)
    audience: List[str] = field(default_factory=list)
    scenarios: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "summary": self.summary,
            "features": list(self.features),
            "price_notes": list(self.price_notes),
            "audience": list(self.audience),
            "scenarios": list(self.scenarios),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProductProfile":
        return cls(
            name=str(data.get("name", "") or ""),
            aliases=list(data.get("aliases", []) or []),
            summary=str(data.get("summary", "") or ""),
            features=list(data.get("features", []) or []),
            price_notes=list(data.get("price_notes", []) or []),
            audience=list(data.get("audience", []) or []),
            scenarios=list(data.get("scenarios", []) or []),
            metadata=dict(data.get("metadata", {}) or {}),
        )


@dataclass
class PolicyRule:
    rule_type: str = ""
    title: str = ""
    content: str = ""
    priority: int = 0
    conditions: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_type": self.rule_type,
            "title": self.title,
            "content": self.content,
            "priority": self.priority,
            "conditions": list(self.conditions),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PolicyRule":
        return cls(
            rule_type=str(data.get("rule_type", "") or ""),
            title=str(data.get("title", "") or ""),
            content=str(data.get("content", "") or ""),
            priority=int(data.get("priority", 0) or 0),
            conditions=list(data.get("conditions", []) or []),
            metadata=dict(data.get("metadata", {}) or {}),
        )


@dataclass
class FAQSignal:
    question: str = ""
    intent_hint: str = ""
    keywords: List[str] = field(default_factory=list)
    answer_summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "intent_hint": self.intent_hint,
            "keywords": list(self.keywords),
            "answer_summary": self.answer_summary,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FAQSignal":
        return cls(
            question=str(data.get("question", "") or ""),
            intent_hint=str(data.get("intent_hint", "") or ""),
            keywords=list(data.get("keywords", []) or []),
            answer_summary=str(data.get("answer_summary", "") or ""),
        )


@dataclass
class DomainProfile:
    enterprise_id: str = "default"
    industry: str = "general"
    sub_industry: str = ""
    tone: str = "专业顾问"
    summary: str = ""
    products: List[ProductProfile] = field(default_factory=list)
    policies: List[PolicyRule] = field(default_factory=list)
    faq_signals: List[FAQSignal] = field(default_factory=list)
    sales_actions: List[str] = field(default_factory=list)
    forbidden_claims: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enterprise_id": self.enterprise_id,
            "industry": self.industry,
            "sub_industry": self.sub_industry,
            "tone": self.tone,
            "summary": self.summary,
            "products": [item.to_dict() for item in self.products],
            "policies": [item.to_dict() for item in self.policies],
            "faq_signals": [item.to_dict() for item in self.faq_signals],
            "sales_actions": list(self.sales_actions),
            "forbidden_claims": list(self.forbidden_claims),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DomainProfile":
        return cls(
            enterprise_id=str(data.get("enterprise_id", "default") or "default"),
            industry=str(data.get("industry", "general") or "general"),
            sub_industry=str(data.get("sub_industry", "") or ""),
            tone=str(data.get("tone", "专业顾问") or "专业顾问"),
            summary=str(data.get("summary", "") or ""),
            products=[ProductProfile.from_dict(item) for item in list(data.get("products", []) or [])],
            policies=[PolicyRule.from_dict(item) for item in list(data.get("policies", []) or [])],
            faq_signals=[FAQSignal.from_dict(item) for item in list(data.get("faq_signals", []) or [])],
            sales_actions=list(data.get("sales_actions", []) or []),
            forbidden_claims=list(data.get("forbidden_claims", []) or []),
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=str(data.get("created_at", datetime.now().isoformat()) or datetime.now().isoformat()),
            updated_at=str(data.get("updated_at", datetime.now().isoformat()) or datetime.now().isoformat()),
        )
