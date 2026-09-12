"""
统一意图类型定义

整合了 intent_recognizer.py, llm_service.py, context_understanding.py 中的意图类型
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Optional


class IntentType(Enum):
    """
    统一意图类型枚举
    
    包含所有业务场景的意图分类
    """
    # 基础交互
    GREETING = "greeting"
    FAREWELL = "farewell"
    THANKS = "thanks"
    CONFIRMATION = "confirmation"
    CASUAL_CHAT = "casual_chat"
    STATUS_INQUIRY = "status_inquiry"  # 状态询问（如"可以回复了吗"）
    READY_CHECK = "ready_check"        # 准备就绪检查
    
    # 产品相关
    PRODUCT_INQUIRY = "product_inquiry"
    PRICE_INQUIRY = "price_inquiry"
    FEATURE_INQUIRY = "feature_inquiry"
    CASE_INQUIRY = "case_inquiry"
    SERVICE_INQUIRY = "service_inquiry"
    CONSULTATION = "consultation"
    COMPARISON = "comparison"
    REJECTION = "rejection"
    
    # 商业意向
    COOPERATION_INTENT = "cooperation_intent"
    COOPERATION = "cooperation"
    PURCHASE_INTENT = "purchase_intent"
    JOIN_INTENT = "join_intent"
    DISCOUNT_REQUEST = "discount_request"
    PAYMENT_METHOD = "payment_method"
    REFUND_REQUEST = "refund_request"
    RETURN_REQUEST = "return_request"
    
    # 服务相关
    COMPLAINT = "complaint"
    FEEDBACK = "feedback"
    SUPPORT_REQUEST = "support_request"
    AFTER_SALES = "after_sales"
    SHIPPING_INQUIRY = "shipping_inquiry"
    WARRANTY_INQUIRY = "warranty_inquiry"
    COMPENSATION = "compensation"
    
    # 联系与对接
    CONTACT_INQUIRY = "contact_inquiry"
    
    # 通用
    COMMON = "common"
    COMMON_QUESTION = "common_question"
    UNKNOWN = "unknown"
    CACHED = "cached"  # 缓存命中标识
    
    @classmethod
    def get_lightweight_interaction_intents(cls):
        """获取轻量交互意图集合。"""
        return {
            cls.GREETING,
            cls.FAREWELL,
            cls.THANKS,
            cls.CONFIRMATION,
            cls.STATUS_INQUIRY,
            cls.READY_CHECK,
        }
    
    @classmethod
    def get_high_priority_intents(cls):
        """获取高优先级意图集合"""
        return {
            cls.COMPLAINT,
            cls.COOPERATION_INTENT,
            cls.PURCHASE_INTENT,
            cls.CONTACT_INQUIRY
        }
    
    @classmethod
    def get_retrieval_intents(cls):
        """获取需要检索的意图集合"""
        return {
            cls.PRODUCT_INQUIRY,
            cls.PRICE_INQUIRY,
            cls.FEATURE_INQUIRY,
            cls.CASE_INQUIRY,
            cls.COMMON_QUESTION,
            cls.SUPPORT_REQUEST,
            cls.AFTER_SALES,
            cls.COMPLAINT,
            cls.COOPERATION_INTENT,
            cls.PURCHASE_INTENT,
            cls.DISCOUNT_REQUEST,
            cls.PAYMENT_METHOD,
            cls.SHIPPING_INQUIRY,
            cls.WARRANTY_INQUIRY,
            cls.REFUND_REQUEST,
            cls.RETURN_REQUEST,
            cls.CONTACT_INQUIRY
        }


_INDUSTRY_INTENTS: Dict[str, str] = {}


def register_industry_intents(intents: Dict[str, str]):
    _INDUSTRY_INTENTS.update(intents)


def get_intent_value(intent_name: str) -> str:
    try:
        return IntentType(intent_name).value
    except ValueError:
        return _INDUSTRY_INTENTS.get(intent_name, IntentType.UNKNOWN.value)


def is_industry_intent(intent_name: str) -> bool:
    return intent_name in _INDUSTRY_INTENTS


def resolve_intent_type(name: str) -> IntentType:
    try:
        return IntentType(name)
    except ValueError:
        if name in _INDUSTRY_INTENTS:
            return IntentType.UNKNOWN
        return IntentType.UNKNOWN


class SentimentType(Enum):
    """情感类型枚举"""
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"


class UrgencyLevel(Enum):
    """紧急程度枚举"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class EmotionType(Enum):
    """情绪类型枚举"""
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    ANXIOUS = "anxious"
    EXCITED = "excited"
    FRUSTRATED = "frustrated"
    SATISFIED = "satisfied"


@dataclass(init=False)
class IntentResult:
    """统一意图识别结果"""
    primaryIntent: IntentType = IntentType.UNKNOWN
    secondaryIntents: List[IntentType] = field(default_factory=list)
    confidence: float = 0.0
    keywords: List[str] = field(default_factory=list)
    entities: Dict[str, List[str]] = field(default_factory=dict)
    sentiment: SentimentType = SentimentType.NEUTRAL
    urgency: UrgencyLevel = UrgencyLevel.MEDIUM
    needHuman: bool = False
    reasoning: str = ""
    need_retrieval: bool = True
    _complexity_override: Optional[float] = field(default=None, repr=False)

    def __init__(
        self,
        primaryIntent: IntentType = IntentType.UNKNOWN,
        secondaryIntents: Optional[List[IntentType]] = None,
        confidence: float = 0.0,
        keywords: Optional[List[str]] = None,
        entities: Optional[Dict[str, List[str]]] = None,
        sentiment: SentimentType = SentimentType.NEUTRAL,
        urgency: UrgencyLevel = UrgencyLevel.MEDIUM,
        needHuman: bool = False,
        reasoning: str = "",
        need_retrieval: bool = True,
        complexity: Optional[float] = None,
    ):
        self.primaryIntent = primaryIntent
        self.secondaryIntents = list(secondaryIntents or [])
        self.confidence = float(confidence or 0.0)
        self.keywords = list(keywords or [])
        self.entities = dict(entities or {})
        self.sentiment = sentiment
        self.urgency = urgency
        self.needHuman = bool(needHuman)
        self.reasoning = reasoning or ""
        self.need_retrieval = bool(need_retrieval)
        self._complexity_override = float(complexity) if complexity is not None else None

    @property
    def intent(self) -> IntentType:
        return self.primaryIntent

    @property
    def primary_intent(self) -> str:
        return self.primaryIntent.value if self.primaryIntent else ""

    @property
    def complexity(self) -> float:
        if self._complexity_override is not None:
            return max(0.0, min(float(self._complexity_override), 1.0))
        if self.secondaryIntents:
            return min(0.5 + len(self.secondaryIntents) * 0.15, 1.0)
        return 0.3


_LEGACY_INDUSTRY_INTENTS = {
    "tour_package_inquiry": "tour_package_inquiry",
    "tour_inclusion_inquiry": "tour_inclusion_inquiry",
}

register_industry_intents(_LEGACY_INDUSTRY_INTENTS)
