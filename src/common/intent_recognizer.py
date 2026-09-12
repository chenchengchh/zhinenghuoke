"""
增强版意图识别服务
支持多意图识别、情感分析、上下文理解
"""
import json
import re
import threading
import jieba
import jieba.analyse
import logging
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .types.intent import IntentType, SentimentType, UrgencyLevel, IntentResult, is_industry_intent, get_intent_value
from .industry_schema_service import get_industry_schema_service, get_active_schema_with_compat
from .intent_classification_service import IntentClassificationService
from .utils import call_llm_safe
from src.config.settings import REPLY_LLM_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """对话上下文"""
    sessionId: str
    messages: List[Dict] = field(default_factory=list)
    intents: List[IntentResult] = field(default_factory=list)
    entities: Dict[str, List[str]] = field(default_factory=dict)
    topicFlow: List[str] = field(default_factory=list)
    createdAt: datetime = field(default_factory=datetime.now)
    updatedAt: datetime = field(default_factory=datetime.now)


class EnhancedIntentRecognizer:
    """
    增强版意图识别器
    支持多意图识别、情感分析、上下文理解、意图继承
    """

    DOMAIN_CONTEXT_TERMS: List[str] = []
    TOURISM_ONLY_INTENTS: List[Any] = []
    
    SENTIMENT_KEYWORDS = {
        SentimentType.POSITIVE: [
            "好", "棒", "赞", "满意", "喜欢", "不错", "优秀",
            "完美", "厉害", "强大", "方便", "实用", "推荐",
            "感谢", "谢谢", "太好了", "很棒", "超值"
        ],
        SentimentType.NEGATIVE: [
            "差", "烂", "垃圾", "坑", "骗", "不好", "不满",
            "失望", "后悔", "难用", "问题", "故障", "bug",
            "投诉", "退款", "赔偿", "垃圾", "骗人"
        ]
    }
    
    URGENCY_KEYWORDS = {
        UrgencyLevel.HIGH: [
            "急", "紧急", "马上", "立即", "尽快", "今天",
            "现在", "立刻", "等着", "催", "加急"
        ],
        UrgencyLevel.LOW: [
            "不急", "慢慢", "不着急", "有空", "方便时",
            "以后", "再说", "考虑"
        ]
    }

    INTENT_INHERITANCE = {
        "product_inquiry": ["price_inquiry", "purchase_intent"],
        "price_inquiry": ["product_inquiry", "purchase_intent", "discount_request"],
        "service_inquiry": ["product_inquiry", "price_inquiry", "contact_inquiry"],
        "purchase_intent": ["price_inquiry", "service_inquiry", "payment_method"],
        "cooperation_intent": ["price_inquiry", "service_inquiry", "contact_inquiry"],
        "complaint": ["refund_request", "return_request", "compensation", "contact_inquiry"],
        "discount_request": ["price_inquiry", "purchase_intent"],
        "payment_method": ["purchase_intent", "price_inquiry"],
        "shipping_inquiry": ["purchase_intent", "service_inquiry"],
        "warranty_inquiry": ["product_inquiry", "service_inquiry"],
        "contact_inquiry": ["service_inquiry", "cooperation_intent"]
    }

    DOMAIN_INTENT_INHERITANCE: Dict[str, List[str]] = {}

    ENTITY_PATTERNS = {
        "price": [
            r"(\d+)元", r"(\d+)块", r"(\d+)万",
            r"套餐"
        ],
        "time": [
            r"(\d+)天", r"(\d+)月", r"(\d+)年",
            r"今天", r"明天", r"下周", r"月底"
        ],
        "contact": [
            r"微信[：:]\s*(\w+)", r"电话[：:]\s*([\d-]+)",
            r"手机[：:]\s*([\d-]+)", r"(\d{11})"
        ]
    }

    DOMAIN_ENTITY_PATTERNS: Dict[str, List[str]] = {}

    INTENT_ENTITY_MAP = {
        IntentType.PRICE_INQUIRY: {"price"},
        IntentType.SHIPPING_INQUIRY: {"time", "contact"},
        IntentType.PAYMENT_METHOD: {"price", "contact"},
    }

    DOMAIN_INTENT_ENTITY_MAP: Dict[IntentType, set] = {}

    INTENT_DESCRIPTION_MAP = {
        IntentType.GREETING: "问候打招呼",
        IntentType.FAREWELL: "告别离开",
        IntentType.THANKS: "感谢致谢",
        IntentType.CONFIRMATION: "确认同意",
        IntentType.CASUAL_CHAT: "闲聊寒暄",
        IntentType.STATUS_INQUIRY: "状态询问（如'可以回复了吗'、'在吗'）",
        IntentType.READY_CHECK: "准备就绪检查",
        IntentType.PRODUCT_INQUIRY: "产品功能咨询（了解产品/服务内容、特性、用途）",
        IntentType.PRICE_INQUIRY: "价格费用咨询（询问价格、套餐、优惠）",
        IntentType.FEATURE_INQUIRY: "功能特性咨询（询问具体功能细节）",
        IntentType.CASE_INQUIRY: "案例参考咨询（要求看案例、客户案例）",
        IntentType.SERVICE_INQUIRY: "服务支持咨询（售后、培训、技术支持）",
        IntentType.CONSULTATION: "一般咨询（泛化的咨询需求）",
        IntentType.COMPARISON: "对比选择（比较不同产品/方案）",
        IntentType.REJECTION: "拒绝否定（明确表示不需要）",
        IntentType.COOPERATION_INTENT: "合作代理意向（表达合作、代理、加盟意愿）",
        IntentType.COOPERATION: "合作洽谈",
        IntentType.PURCHASE_INTENT: "购买下单意向（明确想买、准备付款）",
        IntentType.JOIN_INTENT: "加入注册意向",
        IntentType.COMPLAINT: "投诉不满（对产品/服务表达强烈不满）",
        IntentType.FEEDBACK: "反馈建议（提供使用感受或改进建议）",
        IntentType.SUPPORT_REQUEST: "技术支持请求",
        IntentType.AFTER_SALES: "售后服务请求",
        IntentType.DISCOUNT_REQUEST: "优惠折扣请求（要求打折、优惠、让利）",
        IntentType.PAYMENT_METHOD: "付款方式咨询（询问如何付款、开发票）",
        IntentType.REFUND_REQUEST: "退款请求（要求退钱、取消订单）",
        IntentType.RETURN_REQUEST: "退货换货请求",
        IntentType.SHIPPING_INQUIRY: "物流配送咨询（询问发货、快递、运费）",
        IntentType.WARRANTY_INQUIRY: "保修质保咨询",
        IntentType.COMPENSATION: "索赔赔偿请求",
        IntentType.CONTACT_INQUIRY: "联系方式咨询（询问怎么联系、微信、电话、客服）",
        IntentType.COMMON_QUESTION: "常见问题",
        IntentType.UNKNOWN: "无法识别的意图",
    }

    DOMAIN_INTENT_DESCRIPTION_MAP: Dict[IntentType, str] = {}

    INTENT_REASONING_NAME_MAP = {
        IntentType.PRODUCT_INQUIRY: "产品咨询",
        IntentType.PRICE_INQUIRY: "价格咨询",
        IntentType.SERVICE_INQUIRY: "服务咨询",
        IntentType.COOPERATION_INTENT: "合作意向",
        IntentType.PURCHASE_INTENT: "购买意向",
        IntentType.COMPLAINT: "投诉",
        IntentType.CONSULTATION: "咨询",
        IntentType.COMPARISON: "对比咨询",
        IntentType.FEEDBACK: "反馈",
        IntentType.GREETING: "问候",
        IntentType.FAREWELL: "告别",
        IntentType.THANKS: "感谢",
        IntentType.CONFIRMATION: "确认",
        IntentType.REJECTION: "拒绝",
        IntentType.DISCOUNT_REQUEST: "优惠请求",
        IntentType.PAYMENT_METHOD: "付款咨询",
        IntentType.REFUND_REQUEST: "退款请求",
        IntentType.RETURN_REQUEST: "退货请求",
        IntentType.SHIPPING_INQUIRY: "物流咨询",
        IntentType.WARRANTY_INQUIRY: "保修咨询",
        IntentType.COMPENSATION: "索赔请求",
        IntentType.CONTACT_INQUIRY: "联系方式咨询",
        IntentType.UNKNOWN: "未知意图",
    }

    DOMAIN_INTENT_REASONING_NAME_MAP: Dict[IntentType, str] = {}

    def __init__(self, llm_service=None, intent_classification_service: IntentClassificationService = None):
        """
        初始化意图识别器
        
        Args:
            llm_service: LLM服务实例，用于语义理解意图识别
        """
        self.llm_service = llm_service
        self.intent_classification_service = intent_classification_service or IntentClassificationService()
        self.contexts: Dict[str, ConversationContext] = {}
        self._context_lock = threading.Lock()
    
    @staticmethod
    def _get_active_schema(enterpriseId: str = None) -> Dict[str, Any]:
        try:
            return get_active_schema_with_compat(
                get_industry_schema_service(),
                enterprise_id=enterpriseId or "",
            )
        except Exception:
            return {}

    @staticmethod
    def _intent_from_name(name: str):
        normalized = str(name or "").strip().lower()
        for intent in IntentType:
            if intent.value == normalized:
                return intent
        return None

    @staticmethod
    def _merge_unique_strings(*collections: Any) -> List[str]:
        merged: List[str] = []
        seen = set()
        for collection in collections:
            for item in list(collection or []):
                value = str(item or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                merged.append(value)
        return merged

    def _get_intent_recognition_config(self, enterpriseId: str = None) -> Dict[str, Any]:
        active_schema = self._get_active_schema(enterpriseId)
        metadata = active_schema.get("metadata") or {}
        config = metadata.get("intent_recognition") or {}
        return config if isinstance(config, dict) else {}

    def _normalize_schema_intent_entity_map(self, raw_map: Any) -> Dict[IntentType, set[str]]:
        normalized_map: Dict[IntentType, set[str]] = {}
        if not isinstance(raw_map, dict):
            return normalized_map
        for intent_name, values in raw_map.items():
            intent = self._intent_from_name(intent_name)
            if not intent:
                continue
            normalized_values = {
                str(value or "").strip()
                for value in list(values or [])
                if str(value or "").strip()
            }
            if normalized_values:
                normalized_map[intent] = normalized_values
        return normalized_map

    def _normalize_schema_entity_patterns(self, raw_patterns: Any) -> Dict[str, List[str]]:
        normalized_patterns: Dict[str, List[str]] = {}
        if not isinstance(raw_patterns, dict):
            return normalized_patterns
        for entity_type, patterns in raw_patterns.items():
            normalized_list = self._merge_unique_strings(patterns)
            if normalized_list:
                normalized_patterns[str(entity_type or "").strip()] = normalized_list
        return normalized_patterns

    @staticmethod
    def _get_compat_config_value(config: Dict[str, Any], primary_key: str, legacy_key: str):
        primary_value = config.get(primary_key)
        if primary_value not in (None, "", [], {}, ()):
            return primary_value
        return config.get(legacy_key)

    def _get_schema_domain_overrides(self, enterpriseId: str = None) -> Dict[str, Any]:
        active_schema = self._get_active_schema(enterpriseId)
        config = self._get_intent_recognition_config(enterpriseId)
        if not config and bool(active_schema.get("is_domain_specific", False)):
            warned = getattr(self, "_warned_missing_intent_recognition", None)
            if warned is None:
                warned = set()
                self._warned_missing_intent_recognition = warned
            schema_key = str(active_schema.get("schema_id") or active_schema.get("industry_code") or "unknown")
            if schema_key not in warned:
                warned.add(schema_key)
                logger.warning("Schema 未配置行业领域意图识别参数，将使用默认通用配置")
        return {
            "domain_context_terms": self._merge_unique_strings(
                self._get_compat_config_value(config, "domain_context_terms", "tourism_context_terms")
            ),
            "domain_entity_patterns": self._normalize_schema_entity_patterns(
                self._get_compat_config_value(config, "domain_entity_patterns", "tourism_entity_patterns")
            ),
            "domain_intent_inheritance": {
                str(intent_name or "").strip().lower(): self._merge_unique_strings(values)
                for intent_name, values in dict(
                    self._get_compat_config_value(config, "domain_intent_inheritance", "tourism_intent_inheritance") or {}
                ).items()
                if self._merge_unique_strings(values)
            },
            "domain_intent_entity_map": self._normalize_schema_intent_entity_map(
                self._get_compat_config_value(config, "domain_intent_entity_map", "tourism_intent_entity_map")
            ),
            "domain_intent_descriptions": {
                intent: str(value or "").strip()
                for intent, value in (
                    (self._intent_from_name(intent_name), description)
                    for intent_name, description in dict(
                        self._get_compat_config_value(config, "domain_intent_descriptions", "tourism_intent_descriptions") or {}
                    ).items()
                )
                if intent and str(value or "").strip()
            },
            "domain_reasoning_names": {
                intent: str(value or "").strip()
                for intent, value in (
                    (self._intent_from_name(intent_name), description)
                    for intent_name, description in dict(
                        self._get_compat_config_value(config, "domain_reasoning_names", "tourism_reasoning_names") or {}
                    ).items()
                )
                if intent and str(value or "").strip()
            },
        }

    def _is_domain_schema_active(self, enterpriseId: str = None) -> bool:
        active_schema = self._get_active_schema(enterpriseId)
        return bool(active_schema.get("is_domain_specific", False))

    def _get_effective_domain_context(
        self,
        message: str,
        context: 'ConversationContext' = None,
        enterpriseId: str = None,
    ) -> bool:
        return self._is_domain_schema_active(enterpriseId) or self._has_domain_context(
            message,
            context,
            enterpriseId=enterpriseId,
        )

    def _should_allow_domain_intent(
        self,
        intentType: IntentType,
        domain_context: bool = False,
        tourism_context: Optional[bool] = None,
    ) -> bool:
        if tourism_context is not None:
            domain_context = bool(tourism_context)
        if not is_industry_intent(intentType.value):
            return True
        return domain_context

    def _get_effective_entity_patterns(
        self,
        domain_context: bool = False,
        enterpriseId: str = None,
        tourism_context: Optional[bool] = None,
    ) -> Dict[str, List[str]]:
        if tourism_context is not None:
            domain_context = bool(tourism_context)
        effective = {
            entity_type: list(patterns)
            for entity_type, patterns in self.ENTITY_PATTERNS.items()
        }
        if domain_context:
            overrides = self._get_schema_domain_overrides(enterpriseId)
            for entity_type, patterns in self.DOMAIN_ENTITY_PATTERNS.items():
                merged = effective.get(entity_type, []) + list(patterns)
                effective[entity_type] = list(dict.fromkeys(merged))
            for entity_type, patterns in overrides["domain_entity_patterns"].items():
                merged = effective.get(entity_type, []) + list(patterns)
                effective[entity_type] = list(dict.fromkeys(merged))
        return effective

    def _get_effective_intent_inheritance(
        self,
        domain_context: bool = False,
        enterpriseId: str = None,
        tourism_context: Optional[bool] = None,
    ) -> Dict[str, List[str]]:
        if tourism_context is not None:
            domain_context = bool(tourism_context)
        effective = {
            intent: list(values)
            for intent, values in self.INTENT_INHERITANCE.items()
        }
        if domain_context:
            overrides = self._get_schema_domain_overrides(enterpriseId)
            for intent, values in self.DOMAIN_INTENT_INHERITANCE.items():
                merged = effective.get(intent, []) + list(values)
                effective[intent] = list(dict.fromkeys(merged))
            for intent_name, values in overrides["domain_intent_inheritance"].items():
                merged = effective.get(intent_name, []) + list(values)
                effective[intent_name] = list(dict.fromkeys(merged))
        return effective

    def _get_effective_intent_entity_map(
        self,
        domain_context: bool,
        enterpriseId: str = None,
    ) -> Dict[IntentType, set[str]]:
        effective = {
            intent: set(values)
            for intent, values in self.INTENT_ENTITY_MAP.items()
        }
        if domain_context:
            overrides = self._get_schema_domain_overrides(enterpriseId)
            for intent, values in self.DOMAIN_INTENT_ENTITY_MAP.items():
                effective[intent] = set(effective.get(intent, set())) | set(values)
            for intent, values in overrides["domain_intent_entity_map"].items():
                effective[intent] = set(effective.get(intent, set())) | set(values)
        return effective

    def _get_effective_intent_descriptions(
        self,
        domain_context: bool = False,
        enterpriseId: str = None,
        tourism_context: Optional[bool] = None,
    ) -> Dict[IntentType, str]:
        if tourism_context is not None:
            domain_context = bool(tourism_context)
        effective = dict(self.INTENT_DESCRIPTION_MAP)
        if domain_context:
            effective.update(self.DOMAIN_INTENT_DESCRIPTION_MAP)
            effective.update(self._get_schema_domain_overrides(enterpriseId)["domain_intent_descriptions"])
        return effective

    def _get_effective_reasoning_names(
        self,
        domain_context: bool,
        enterpriseId: str = None,
    ) -> Dict[IntentType, str]:
        effective = dict(self.INTENT_REASONING_NAME_MAP)
        if domain_context:
            effective.update(self.DOMAIN_INTENT_REASONING_NAME_MAP)
            effective.update(self._get_schema_domain_overrides(enterpriseId)["domain_reasoning_names"])
        return effective

    def _get_domain_context_terms(self, enterpriseId: str = None) -> set[str]:
        terms: set[str] = {
            term
            for term in self.DOMAIN_CONTEXT_TERMS
            if len(str(term or "").strip()) >= 2
        }
        overrides = self._get_schema_domain_overrides(enterpriseId)
        entity_patterns = dict(self.DOMAIN_ENTITY_PATTERNS)
        entity_patterns.update(overrides["domain_entity_patterns"])
        for patterns in entity_patterns.values():
            for pattern in patterns:
                normalized = str(pattern or "").strip()
                if not normalized:
                    continue
                if "|" in normalized and not re.search(r"[\\\[\]\(\)\+\*\?\{\}\.^$]", normalized):
                    for item in normalized.split("|"):
                        item = item.strip()
                        if len(item) >= 2:
                            terms.add(item)
                    continue
                if len(normalized) >= 2 and not re.search(r"[\\\[\]\(\)\+\*\?\{\}\.^$|]", normalized):
                    terms.add(normalized)

        for term in overrides["domain_context_terms"]:
            if len(term) >= 2:
                terms.add(term)
        return terms

    def _has_domain_context(
        self,
        message: str,
        context: 'ConversationContext' = None,
        enterpriseId: str = None,
    ) -> bool:
        text = str(message or "")
        domain_terms = self._get_domain_context_terms(enterpriseId)
        if any(term in text for term in domain_terms):
            return True

        if not self._is_domain_schema_active(enterpriseId):
            return False

        if not context:
            return False

        topic = str(getattr(context, "current_topic", "") or "")
        if any(term in topic for term in domain_terms):
            return True

        entities = getattr(context, "entities", {}) or {}
        for value in entities.values():
            if isinstance(value, list):
                values = value
            else:
                values = [value]
            for item in values:
                if any(term in str(item or "") for term in domain_terms):
                    return True
        return False

    def recognize(
        self,
        message: str,
        sessionId: str = None,
        context: 'ConversationContext' = None,
        enterpriseId: str = None,
    ) -> IntentResult:
        """
        识别消息意图（通用模式：LLM + 知识画像）

        Args:
            message: 用户消息
            sessionId: 会话ID
            context: 对话上下文

        Returns:
            意图识别结果
        """
        result: Optional[IntentResult] = None

        if self.llm_service:
            try:
                result = self._llm_recognize(message, context, enterpriseId=enterpriseId)
                if result and result.primaryIntent != IntentType.UNKNOWN:
                    logger.info(
                        f"LLM意图识别: {result.primaryIntent.value}, 置信度: {result.confidence:.2f}"
                    )
            except Exception as e:
                logger.warning(f"LLM意图识别失败，尝试知识画像回退: {e}")

        if result is None or result.primaryIntent == IntentType.UNKNOWN:
            result = self._profile_based_recognize(message, enterpriseId=enterpriseId)

        if result is None:
            result = self._build_unknown_result(message, reason="llm_and_profile_unavailable")

        if 0.3 <= result.confidence < 0.6 and context:
            result = self._disambiguate(result, message, context)

        if sessionId:
            self._updateContext(sessionId, message, result)

        return result

    def _profile_based_recognize(
        self,
        message: str,
        enterpriseId: str = None,
    ) -> Optional[IntentResult]:
        if not enterpriseId or not self.intent_classification_service:
            return None

        profile_scores = self.intent_classification_service.build_profile_intent_scores(
            message=message,
            enterprise_id=enterpriseId,
        )
        if not profile_scores:
            return None

        sorted_intents = sorted(profile_scores.items(), key=lambda item: item[1], reverse=True)
        primary_intent, primary_score = sorted_intents[0]
        secondary_intents = [
            intent
            for intent, score in sorted_intents[1:4]
            if score >= max(primary_score * 0.55, 0.8)
        ]
        confidence = min(0.35 + primary_score * 0.18, 0.78)
        sentiment = self._analyzeSentiment(message)
        urgency = self._analyzeUrgency(message)
        entities = self._extractEntities(message)
        keywords = self._extract_keywords_from_message(message)
        needHuman = self._checkNeedHuman(primary_intent, sentiment, urgency, confidence)

        return IntentResult(
            primaryIntent=primary_intent,
            secondaryIntents=secondary_intents,
            confidence=confidence,
            keywords=keywords,
            entities=entities,
            sentiment=sentiment,
            urgency=urgency,
            needHuman=needHuman,
            reasoning="知识画像语义回退",
        )

    def _build_unknown_result(self, message: str, reason: str = "") -> IntentResult:
        sentiment = self._analyzeSentiment(message)
        urgency = self._analyzeUrgency(message)
        entities = self._extractEntities(message)
        keywords = self._extract_keywords_from_message(message)
        return IntentResult(
            primaryIntent=IntentType.UNKNOWN,
            confidence=0.0,
            keywords=keywords,
            entities=entities,
            sentiment=sentiment,
            urgency=urgency,
            needHuman=True,
            reasoning=reason or "unknown",
        )
    
    def _disambiguate(
        self,
        result: IntentResult,
        message: str,
        context: 'ConversationContext'
    ) -> IntentResult:
        """
        歧义消解：当 LLM/知识画像结果处于模糊区间时，结合上下文进行消歧
        """
        confidence_boost = 0.0
        adjusted_intent = result.primaryIntent
        adjusted_reasoning = result.reasoning
        domain_context = self._get_effective_domain_context(message, context, enterpriseId=enterpriseId)
        effective_inheritance = self._get_effective_intent_inheritance(domain_context, enterpriseId=enterpriseId)
        
        if hasattr(context, 'intents') and context.intents:
            recent_intents = [i.primaryIntent for i in context.intents[-3:]]
            
            for prev_intent in recent_intents:
                prev_value = prev_intent.value.lower()
                current_value = result.primaryIntent.value.lower()
                
                if current_value in effective_inheritance.get(prev_value, []):
                    confidence_boost += 0.1
                    adjusted_reasoning += f" | 上下文继承: {prev_value}->{current_value}"
                
                if prev_intent in IntentType.get_high_priority_intents():
                    if result.primaryIntent in IntentType.get_high_priority_intents():
                        confidence_boost += 0.05
        
        if hasattr(context, 'entities') and context.entities:
            entity_types = set(context.entities.keys())
            intent_entity_map = self._get_effective_intent_entity_map(domain_context, enterpriseId=enterpriseId)
            expected_entities = intent_entity_map.get(result.primaryIntent, set())
            if expected_entities and entity_types & expected_entities:
                confidence_boost += 0.08
                adjusted_reasoning += f" | 实体关联: {entity_types & expected_entities}"
        
        if result.sentiment == SentimentType.NEGATIVE:
            if result.primaryIntent == IntentType.FEEDBACK:
                adjusted_intent = IntentType.COMPLAINT
                confidence_boost += 0.1
                adjusted_reasoning += " | 情感-意图调整: 负面情感+反馈→投诉"
            elif result.primaryIntent == IntentType.PRICE_INQUIRY:
                adjusted_intent = IntentType.DISCOUNT_REQUEST
                confidence_boost += 0.08
                adjusted_reasoning += " | 情感-意图调整: 负面情感+价格咨询→优惠请求"
        
        if hasattr(context, 'intents') and len(context.intents) >= 2:
            intent_sequence = [i.primaryIntent for i in context.intents[-2:]]
            if intent_sequence[-1] == IntentType.PRODUCT_INQUIRY:
                if result.primaryIntent == IntentType.CONSULTATION:
                    adjusted_intent = IntentType.PRICE_INQUIRY
                    confidence_boost += 0.05
                    adjusted_reasoning += " | 对话阶段: 产品咨询后→价格咨询"
            elif intent_sequence[-1] == IntentType.PRICE_INQUIRY:
                if result.primaryIntent == IntentType.CONSULTATION:
                    adjusted_intent = IntentType.PURCHASE_INTENT
                    confidence_boost += 0.05
                    adjusted_reasoning += " | 对话阶段: 价格咨询后→购买意向"
        
        new_confidence = min(result.confidence + confidence_boost, 0.95)
        
        return IntentResult(
            primaryIntent=adjusted_intent,
            secondaryIntents=result.secondaryIntents,
            confidence=new_confidence,
            keywords=result.keywords,
            entities=result.entities,
            sentiment=result.sentiment,
            urgency=result.urgency,
            needHuman=result.needHuman,
            reasoning=adjusted_reasoning
        )
    
    def _llm_recognize(
        self,
        message: str,
        context: 'ConversationContext' = None,
        enterpriseId: str = None,
    ) -> IntentResult:
        """
        使用LLM进行意图识别（语义理解增强）
        
        优势：
        - 能理解同义词、隐含语义
        - 能处理复杂句式
        - 能结合上下文推理
        """
        domain_context = self._get_effective_domain_context(message, context, enterpriseId=enterpriseId)
        intent_descriptions = self._get_effective_intent_descriptions(domain_context, enterpriseId=enterpriseId)

        intent_list = "\n".join([
            f"- {intent.value}: {intent_descriptions.get(intent, '')}"
            for intent in IntentType
            if intent != IntentType.CACHED and self._should_allow_domain_intent(intent, domain_context)
        ])

        context_info = ""
        if context:
            recent_intents = [
                i.primaryIntent.value
                for i in getattr(context, "intents", [])[-3:]
                if getattr(i, "primaryIntent", None)
            ]
            if recent_intents:
                context_info = f"\n对话历史意图: {recent_intents}"
            recent_messages = [
                str(item.get("content") or "").strip()
                for item in getattr(context, "messages", [])[-4:]
                if str(item.get("content") or "").strip()
            ]
            if recent_messages:
                context_info += f"\n最近对话: {recent_messages}"
        profile_context = self.intent_classification_service.build_prompt_context(enterpriseId)
        if profile_context:
            context_info += f"\n{profile_context}"

        prompt = f"""分析用户消息的意图类型。

用户消息: {message}{context_info}

可选意图类型:
{intent_list}

分析要求:
1. 根据用户消息的语义，选择最匹配的意图类型
2. 如果消息可能属于多个意图，选择最核心的作为primary_intent，其他作为secondary_intents
3. 注意区分相似意图：price_inquiry(问价格) vs discount_request(要优惠) vs purchase_intent(要购买)
4. 注意区分：complaint(强烈不满) vs feedback(温和建议) vs refund_request(要求退钱)
5. 如果消息简短模糊，结合上下文推理
6. 只能从给定意图中选择；如果无法判断，返回 unknown
7. 不要使用关键词匹配思路，要按语义和上下文判断

请以JSON格式输出分析结果（只输出JSON，不要其他内容）:
{{
    "primary_intent": "意图类型",
    "secondary_intents": ["次要意图1", "次要意图2"],
    "confidence": 0.0-1.0,
    "sentiment": "positive/neutral/negative/mixed",
    "urgency": "high/medium/low",
    "reasoning": "分析理由"
}}"""

        try:
            response = call_llm_safe(self.llm_service, prompt, timeout=REPLY_LLM_TIMEOUT_SECONDS)
            if not response:
                return None

            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())

                intent_str = data.get('primary_intent', 'unknown').lower().strip()
                primaryIntent = IntentType.UNKNOWN
                for intent in IntentType:
                    if intent.value == intent_str:
                        primaryIntent = intent
                        break

                sentiment_str = data.get('sentiment', 'neutral').lower()
                sentiment_map = {
                    'positive': SentimentType.POSITIVE,
                    'negative': SentimentType.NEGATIVE,
                    'neutral': SentimentType.NEUTRAL,
                    'mixed': SentimentType.MIXED
                }
                sentiment = sentiment_map.get(sentiment_str, SentimentType.NEUTRAL)

                secondary_intents = []
                for si in data.get('secondary_intents', []):
                    si_str = si.lower().strip() if isinstance(si, str) else ''
                    for intent in IntentType:
                        if intent.value == si_str and intent != primaryIntent:
                            secondary_intents.append(intent)
                            break

                urgency_str = data.get('urgency', 'medium').lower()
                urgency_map = {
                    'high': UrgencyLevel.HIGH,
                    'medium': UrgencyLevel.MEDIUM,
                    'low': UrgencyLevel.LOW
                }
                urgency = urgency_map.get(urgency_str, UrgencyLevel.MEDIUM)

                confidence = max(0.0, min(float(data.get('confidence', 0.5)), 0.98))
                keywords = self._extract_keywords_from_message(message)
                entities = self._extractEntities(message, enterpriseId=enterpriseId)
                needHuman = self._checkNeedHuman(primaryIntent, sentiment, urgency, confidence)
                reasoning = data.get('reasoning', f'LLM语义分析: {message[:30]}...')

                return IntentResult(
                    primaryIntent=primaryIntent,
                    secondaryIntents=secondary_intents,
                    confidence=confidence,
                    keywords=keywords,
                    entities=entities,
                    sentiment=sentiment,
                    urgency=urgency,
                    needHuman=needHuman,
                    reasoning=reasoning
                )
        except Exception as e:
            logger.error(f"LLM意图识别异常: {e}")

        return None
    
    def _extract_keywords_from_message(self, message: str) -> List[str]:
        """从消息中提取关键词"""
        try:
            import jieba
            words = jieba.cut(message)
            return [w for w in words if len(w) >= 2]
        except Exception:
            return [w for w in message.split() if len(w) >= 2]
    
    def _analyzeSentiment(self, message: str) -> SentimentType:
        """分析情感"""
        positiveCount = sum(1 for kw in self.SENTIMENT_KEYWORDS[SentimentType.POSITIVE] if kw in message)
        negativeCount = sum(1 for kw in self.SENTIMENT_KEYWORDS[SentimentType.NEGATIVE] if kw in message)
        
        if positiveCount > negativeCount:
            return SentimentType.POSITIVE
        elif negativeCount > positiveCount:
            return SentimentType.NEGATIVE
        elif positiveCount > 0 and negativeCount > 0:
            return SentimentType.MIXED
        return SentimentType.NEUTRAL
    
    def _analyzeUrgency(self, message: str) -> UrgencyLevel:
        """分析紧急程度"""
        for kw in self.URGENCY_KEYWORDS[UrgencyLevel.HIGH]:
            if kw in message:
                return UrgencyLevel.HIGH
        
        for kw in self.URGENCY_KEYWORDS[UrgencyLevel.LOW]:
            if kw in message:
                return UrgencyLevel.LOW
        
        return UrgencyLevel.MEDIUM
    
    def _extractEntities(self, message: str, enterpriseId: str = None) -> Dict[str, List[str]]:
        """提取实体"""
        entities = {}
        domain_context = self._get_effective_domain_context(message, enterpriseId=enterpriseId)
        effective_entity_patterns = self._get_effective_entity_patterns(domain_context, enterpriseId=enterpriseId)
        
        for entityType, patterns in effective_entity_patterns.items():
            found = []
            for pattern in patterns:
                matches = re.findall(pattern, message, re.IGNORECASE)
                if matches:
                    if isinstance(matches[0], str):
                        found.extend(matches)
                    elif isinstance(matches[0], tuple) and len(matches[0]) > 0:
                        found.extend([m[0] for m in matches if m])
            if found:
                entities[entityType] = list(set(found))
        
        try:
            keywords = jieba.analyse.extract_tags(message, topK=5)
            if keywords:
                entities["keywords"] = keywords
        except Exception as e:
            logger.debug(f"关键词提取失败: {e}")
        
        return entities
    
    def _checkNeedHuman(
        self,
        intent: IntentType,
        sentiment: SentimentType,
        urgency: UrgencyLevel,
        confidence: float
    ) -> bool:
        """判断是否需要人工介入"""
        if intent == IntentType.COMPLAINT:
            return True
        if intent == IntentType.COOPERATION_INTENT:
            return True
        if intent == IntentType.PURCHASE_INTENT and confidence > 0.7:
            return True
        if sentiment == SentimentType.NEGATIVE and urgency == UrgencyLevel.HIGH:
            return True
        if confidence < 0.3:
            return True
        return False
    
    def _generateReasoning(
        self,
        intent: IntentType,
        keywords: List[str],
        sentiment: SentimentType,
        urgency: UrgencyLevel,
        domain_context: bool = False,
    ) -> str:
        """生成推理说明"""
        intentNames = self._get_effective_reasoning_names(domain_context)
        
        sentimentNames = {
            SentimentType.POSITIVE: "积极",
            SentimentType.NEGATIVE: "消极",
            SentimentType.NEUTRAL: "中性",
            SentimentType.MIXED: "混合"
        }
        
        urgencyNames = {
            UrgencyLevel.HIGH: "紧急",
            UrgencyLevel.MEDIUM: "一般",
            UrgencyLevel.LOW: "不急"
        }
        
        parts = [
            f"主要意图: {intentNames.get(intent, '未知')}",
            f"关键词: {', '.join(keywords[:5]) if keywords else '无'}",
            f"情感: {sentimentNames.get(sentiment, '未知')}",
            f"紧急程度: {urgencyNames.get(urgency, '未知')}"
        ]
        
        return " | ".join(parts)
    
    def _updateContext(
        self,
        sessionId: str,
        message: str,
        result: IntentResult
    ):
        """更新对话上下文（带LRU淘汰，防止内存无限增长，线程安全，锁覆盖完整）"""
        with self._context_lock:
            if sessionId not in self.contexts:
                max_contexts = 500
                if len(self.contexts) >= max_contexts:
                    try:
                        oldest_key = min(self.contexts.keys(),
                                         key=lambda k: self.contexts[k].updatedAt
                                         if hasattr(self.contexts[k], 'updatedAt') and self.contexts[k].updatedAt
                                         else datetime.min)
                        self.contexts.pop(oldest_key, None)
                    except (ValueError, KeyError):
                        pass
                self.contexts[sessionId] = ConversationContext(sessionId=sessionId)
            
            ctx = self.contexts[sessionId]
            ctx.messages.append({
                "content": message,
                "timestamp": datetime.now().isoformat(),
                "intent": result.primaryIntent.value
            })
            ctx.intents.append(result)
            
            for entityType, entities in result.entities.items():
                if entityType not in ctx.entities:
                    ctx.entities[entityType] = []
                ctx.entities[entityType].extend(entities)
                ctx.entities[entityType] = list(set(ctx.entities[entityType]))
            
            ctx.topicFlow.append(result.primaryIntent.value)
            ctx.updatedAt = datetime.now()
    
    def getContext(self, sessionId: str) -> Optional[ConversationContext]:
        """获取对话上下文"""
        return self.contexts.get(sessionId)
    
    def summarizeConversation(self, sessionId: str) -> Dict:
        """总结对话"""
        ctx = self.getContext(sessionId)
        if not ctx:
            return {"message": "无对话记录"}
        
        intentCounts = {}
        for intent in ctx.intents:
            intentName = intent.primaryIntent.value
            intentCounts[intentName] = intentCounts.get(intentName, 0) + 1
        
        mainIntent = max(intentCounts.items(), key=lambda x: x[1])[0] if intentCounts else "unknown"
        
        return {
            "messageCount": len(ctx.messages),
            "mainIntent": mainIntent,
            "intentDistribution": intentCounts,
            "entities": ctx.entities,
            "topicFlow": ctx.topicFlow,
            "duration": (ctx.updatedAt - ctx.createdAt).total_seconds()
        }
    
    def getIntentScore(self, message: str, enterpriseId: str = None) -> Dict[str, float]:
        """
        获取所有意图的得分
        
        Args:
            message: 用户消息
        
        Returns:
            意图得分字典
        """
        if not enterpriseId or not self.intent_classification_service:
            return {}
        profile_scores = self.intent_classification_service.build_profile_intent_scores(
            message=message,
            enterprise_id=enterpriseId,
        )
        return {
            intent.value: score
            for intent, score in profile_scores.items()
            if score > 0
        }

    def predict_next_intent(
        self,
        sessionId: str,
        context: 'ConversationContext' = None
    ) -> List[Dict[str, Any]]:
        """
        预测下一个可能的意图

        Args:
            sessionId: 会话ID
            context: 对话上下文

        Returns:
            预测的意图列表，按概率排序
        """
        predictions = []
        currentIntent = ""
        if context and hasattr(context, 'current_intent') and context.current_intent:
            currentIntent = context.current_intent.lower()
        domain_context = self._is_domain_schema_active() or (currentIntent and is_industry_intent(currentIntent))
        effective_inheritance = self._get_effective_intent_inheritance(domain_context)

        if currentIntent:
            if currentIntent in effective_inheritance:
                relatedIntents = effective_inheritance.get(currentIntent, [])
                for intent in relatedIntents:
                    try:
                        intentEnum = IntentType(intent)
                        if not self._should_allow_domain_intent(intentEnum, domain_context):
                            continue
                        predictions.append({
                            "intent": intent,
                            "intent_name": intentEnum.value,
                            "probability": 0.7,
                            "reason": f"从{currentIntent}意图继承"
                        })
                    except ValueError:
                        pass

        if context and hasattr(context, 'entities') and context.entities:
            intent_entity_map = self._get_effective_intent_entity_map(domain_context)
            for entityType, entities in context.entities.items():
                for intentType, expected_entities in intent_entity_map.items():
                    if not self._should_allow_domain_intent(intentType, domain_context):
                        continue
                    if entityType not in expected_entities:
                        continue
                    existing = next((p for p in predictions if p["intent"] == intentType.value), None)
                    if existing:
                        continue
                    sample_entity = entities[0] if entities else entityType
                    predictions.append({
                        "intent": intentType.value,
                        "intent_name": intentType.value,
                        "probability": 0.5,
                        "reason": f"基于实体{sample_entity}"
                    })

        predictions.sort(key=lambda x: x["probability"], reverse=True)
        return predictions[:5]

    def analyze_intent_transition(
        self,
        sessionId: str,
        context: 'ConversationContext' = None
    ) -> Dict[str, Any]:
        """
        分析意图转换模式

        Args:
            sessionId: 会话ID
            context: 对话上下文

        Returns:
            意图转换分析结果
        """
        if not context or not hasattr(context, 'topic_flow'):
            return {"pattern": "insufficient_data"}

        topicFlow = context.topic_flow if hasattr(context, 'topic_flow') else []
        if len(topicFlow) < 2:
            return {"pattern": "too_short", "transitions": []}

        transitions = {}
        for i in range(len(topicFlow) - 1):
            fromIntent = topicFlow[i]
            toIntent = topicFlow[i + 1]
            key = f"{fromIntent} -> {toIntent}"
            transitions[key] = transitions.get(key, 0) + 1

        sortedTransitions = sorted(transitions.items(), key=lambda x: x[1], reverse=True)
        mostCommon = sortedTransitions[0] if sortedTransitions else (None, 0)

        return {
            "pattern": mostCommon[0] if mostCommon[0] else "unknown",
            "count": mostCommon[1],
            "all_transitions": dict(sortedTransitions[:10]),
            "total_transitions": len(topicFlow) - 1
        }

    def get_conversation_insights(
        self,
        sessionId: str,
        context: 'ConversationContext' = None
    ) -> Dict[str, Any]:
        """
        获取对话洞察

        Args:
            sessionId: 会话ID
            context: 对话上下文

        Returns:
            对话洞察分析
        """
        insights = {
            "sessionId": sessionId,
            "current_stage": "unknown",
            "customer_sentiment_trend": "stable",
            "buying_signals": [],
            "risk_signals": [],
            "recommendations": []
        }

        if not context:
            return insights

        if hasattr(context, 'sentiment_history') and context.sentiment_history:
            sentiments = context.sentiment_history
            if len(sentiments) >= 2:
                if sentiments[-1] != sentiments[0]:
                    insights["customer_sentiment_trend"] = "changing"

        if hasattr(context, 'intents') and context.intents:
            intentNames = [i.primaryIntent.value for i in context.intents[-5:]]

            purchaseKeywords = ["purchase_intent", "price_inquiry"]
            if any(kw in intentNames for kw in purchaseKeywords):
                insights["buying_signals"].append("active_inquiry")

            complaintKeywords = ["complaint", "refund_request"]
            if any(kw in intentNames for kw in complaintKeywords):
                insights["risk_signals"].append("complaint_detected")

        if hasattr(context, 'urgency_history') and context.urgency_history:
            if any(u == "high" for u in context.urgency_history[-3:]):
                insights["risk_signals"].append("high_urgency")

        return insights
