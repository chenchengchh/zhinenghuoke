"""
统一意图识别服务 (UnifiedIntentService)

整合意图识别的统一入口:
- EnhancedIntentRecognizer: 规则+上下文意图识别 (主链路)
- BertIntentRecognizer: BERT模型意图分类 (底层引擎)

设计原则:
1. BERT优先: 有BERT模型时使用BERT，否则降级到规则
2. 缓存优化: 相同查询缓存结果
3. 向后兼容: 保留原有接口
"""

import re
import json
import threading
import logging
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .types.intent import (
    IntentType,
    SentimentType,
    UrgencyLevel,
    is_industry_intent,
    get_intent_value,
)
from .utils import call_llm_safe
from src.config.settings import REPLY_LLM_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


# ============== 统一结果数据类 ==============

@dataclass
class UnifiedIntentResult:
    """统一意图识别结果"""
    primary_intent: IntentType
    confidence: float = 0.0
    sentiment: SentimentType = SentimentType.NEUTRAL
    urgency: UrgencyLevel = UrgencyLevel.MEDIUM
    slots: Dict[str, str] = field(default_factory=dict)
    source: str = "unified"  # bert, rule, llm, cached
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============== 情感分析工具 ==============

class SentimentAnalyzer:
    """情感分析工具 (从原EnhancedIntentRecognizer迁移)"""

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
        ],
    }

    @classmethod
    def analyze(cls, text: str) -> Tuple[SentimentType, float]:
        """分析文本情感"""
        text_lower = text.lower()
        pos_score = sum(1 for kw in cls.SENTIMENT_KEYWORDS[SentimentType.POSITIVE] if kw in text_lower)
        neg_score = sum(1 for kw in cls.SENTIMENT_KEYWORDS[SentimentType.NEGATIVE] if kw in text_lower)

        total = pos_score + neg_score
        if total == 0:
            return SentimentType.NEUTRAL, 0.5

        confidence = max(pos_score, neg_score) / total
        if pos_score > neg_score:
            return SentimentType.POSITIVE, confidence
        elif neg_score > pos_score:
            return SentimentType.NEGATIVE, confidence
        else:
            return SentimentType.NEUTRAL, 0.5


# ============== 紧急程度分析 ==============

class UrgencyAnalyzer:
    """紧急程度分析工具 (从原EnhancedIntentRecognizer迁移)"""

    URGENCY_KEYWORDS = {
        UrgencyLevel.HIGH: [
            "急", "紧急", "马上", "立即", "尽快", "今天",
            "现在", "立刻", "等着", "催", "加急"
        ],
        UrgencyLevel.LOW: [
            "不急", "慢慢", "不着急", "有空", "方便时",
            "以后", "再说", "考虑"
        ],
    }

    @classmethod
    def analyze(cls, text: str) -> UrgencyLevel:
        """分析文本紧急程度"""
        high_count = sum(1 for kw in cls.URGENCY_KEYWORDS[UrgencyLevel.HIGH] if kw in text)
        low_count = sum(1 for kw in cls.URGENCY_KEYWORDS[UrgencyLevel.LOW] if kw in text)

        if high_count > low_count and high_count > 0:
            return UrgencyLevel.HIGH
        elif low_count > 0:
            return UrgencyLevel.LOW
        elif high_count == low_count and high_count > 0:
            return UrgencyLevel.MEDIUM
        else:
            return UrgencyLevel.MEDIUM


# ============== 规则匹配器 ==============

class RuleBasedMatcher:
    """基于规则的意图匹配器 (从原EnhancedIntentRecognizer迁移核心逻辑)"""

    # 意图关键词映射
    INTENT_KEYWORDS = {
        IntentType.GREETING: ["你好", "嗨", "hi", "hello", "在吗", "在不在"],
        IntentType.FAREWELL: ["再见", "拜拜", "bye", "下次聊"],
        IntentType.THANKS: ["谢谢", "感谢", "多谢", "thx", "thanks"],
        IntentType.PRODUCT_INQUIRY: ["产品", "功能", "能做什么", "有什么用", "系统", "平台"],
        IntentType.PRICE_INQUIRY: ["多少钱", "价格", "费用", "收费", "贵", "便宜", "成本", "付费", "免费"],
        IntentType.FEATURE_INQUIRY: ["特性", "特点", "优势", "好处", "功能"],
        IntentType.PURCHASE_INTENT: ["买", "购买", "订购", "下单", "要一个", "想要"],
        IntentType.COMPLAINT: ["投诉", "退款", "问题", "故障", "无法", "错误", "失败", "不工作"],
        IntentType.COOPERATION_INTENT: ["合作", "加盟", "代理", "伙伴", "商务", "投资"],
        IntentType.AFTER_SALES: ["售后", "维修", "保养", "退换货"],
        IntentType.CONTACT_INQUIRY: ["联系", "电话", "微信", "地址", "在哪里"],
    }

    # 实体提取模式
    ENTITY_PATTERNS = {
        "price": [r"(\d+)元", r"(\d+)块", r"(\d+)万", r"套餐"],
        "time": [r"(\d+)天", r"(\d+)月", r"(\d+)年", r"今天", r"明天", r"下周"],
        "contact": [r"微信[：:]\s*(\w+)", r"电话[：:]\s*([\d-]+)", r"手机[：:]\s*([\d-]+)", r"(\d{11})"],
    }

    @classmethod
    def match_intent(cls, query: str) -> Tuple[Optional[IntentType], float]:
        """基于关键词匹配意图"""
        query_lower = query.lower()

        best_intent = None
        best_score = 0.0

        for intent, keywords in cls.INTENT_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in query_lower)
            if score > best_score:
                best_score = score / len(keywords)
                best_intent = intent

        if best_intent and best_score >= 0.5:
            return best_intent, min(best_score * 100, 95.0)

        return None, 0.0

    @classmethod
    def extract_entities(cls, text: str) -> Dict[str, List[str]]:
        """提取实体信息"""
        entities = {}
        for entity_type, patterns in cls.ENTITY_PATTERNS.items():
            matches = []
            for pattern in patterns:
                found = re.findall(pattern, text)
                matches.extend(found)
            if matches:
                entities[entity_type] = list(set(matches))
        return entities


# ============== 核心统一服务 ==============

class UnifiedIntentService:
    """
    统一意图识别服务

    整合多种识别策略:
    1. BERT模型优先 (快速、准确)
    2. 规则匹配兜底 (稳定、可控)
    3. LLM语义理解 (复杂场景)

    用法示例:
        service = UnifiedIntentService.get_instance()
        result = service.recognize("产品价格是多少?")
        print(result.primary_intent)  # IntentType.PRICE_INQUIRY
        print(result.confidence)      # 0.85
        print(result.sentiment)       # SentimentType.NEUTRAL
    """

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.bert_recognizer = None
        self.rule_matcher = RuleBasedMatcher()
        self._cache: Dict[str, UnifiedIntentResult] = {}
        self._cache_max_size = 500
        self._init_bert()

    def _init_bert(self):
        """初始化BERT识别器（可选）"""
        try:
            from .bert_intent_recognizer import BertIntentRecognizer, get_bert_intent_recognizer
            self.bert_recognizer = get_bert_intent_recognizer()
            logger.info("BERT意图识别器初始化成功")
        except Exception as e:
            logger.warning(f"BERT意图识别器不可用: {e}，将使用规则匹配")

    @classmethod
    def get_instance(cls, reset: bool = False) -> 'UnifiedIntentService':
        """获取单例实例"""
        if cls._instance is not None and not reset:
            return cls._instance
        with cls._lock:
            if cls._instance is not None and not reset:
                return cls._instance
            cls._instance = cls()
        return cls._instance

    def recognize(
        self,
        query: str,
        context: Optional[List[Dict]] = None,
        use_cache: bool = True,
        prefer_bert: bool = True,
    ) -> UnifiedIntentResult:
        """
        执行统一意图识别

        Args:
            query: 用户输入文本
            context: 对话上下文 (可选)
            use_cache: 是否使用缓存
            prefer_bert: 是否优先使用BERT

        Returns:
            UnifiedIntentResult: 统一识别结果
        """
        # 1. 检查缓存
        cache_key = query.strip().lower()[:200]
        if use_cache and cache_key in self._cache:
            cached_result = self._cache[cache_key]
            cached_result.source = "cached"
            return cached_result

        # 2. 尝试BERT识别
        result = None
        if prefer_bert and self.bert_recognizer:
            result = self._recognize_with_bert(query)

        # 3. BERT失败或置信度低时使用规则匹配
        if result is None or result.confidence < 0.6:
            rule_result = self._recognize_with_rules(query)
            if result is None or rule_result.confidence > result.confidence:
                result = rule_result

        # 4. 补充情感和紧急度
        result.sentiment = SentimentAnalyzer.analyze(query)[0]
        result.urgency = UrgencyAnalyzer.analyze(query)

        # 5. 写入缓存
        if use_cache and len(self._cache) < self._cache_max_size:
            self._cache[cache_key] = result

        return result

    def _recognize_with_bert(self, query: str) -> Optional[UnifiedIntentResult]:
        """使用BERT模型识别意图"""
        if not self.bert_recognizer:
            return None

        try:
            bert_result = self.bert_recognizer.recognize(query)
            if bert_result and hasattr(bert_result, 'primaryIntent'):
                return UnifiedIntentResult(
                    primary_intent=bert_result.primaryIntent,
                    confidence=getattr(bert_result, 'confidence', 0.8),
                    slots={s.slotName: s.value for s in getattr(bert_result, 'slots', [])},
                    source="bert",
                )
        except Exception as e:
            logger.warning(f"BERT识别失败: {e}")

        return None

    def _recognize_with_rules(self, query: str) -> UnifiedIntentResult:
        """使用规则匹配识别意图"""
        intent, confidence = RuleBasedMatcher.match_intent(query)
        entities = RuleBasedMatcher.extract_entities(query)

        if intent is None:
            intent = IntentType.UNKNOWN
            confidence = 0.3

        return UnifiedIntentResult(
            primary_intent=intent,
            confidence=confidence,
            slots=entities,
            source="rule",
        )

    def recognize_batch(
        self,
        queries: List[str],
    ) -> List[UnifiedIntentResult]:
        """批量识别意图"""
        return [self.recognize(q) for q in queries]

    def clear_cache(self):
        """清除缓存"""
        self._cache.clear()


# ============== 工厂函数和便捷方法 ==============

def recognize_intent(
    query: str,
    context: Optional[List[Dict]] = None,
) -> UnifiedIntentResult:
    """
    便捷函数: 执行意图识别

    这是推荐的统一入口，替代直接调用各个识别器。

    Args:
        query: 用户输入
        context: 对话上下文 (可选)

    Returns:
        UnifiedIntentResult: 识别结果

    示例:
        >>> result = recognize_intent("产品多少钱？")
        >>> print(result.primary_intent)
        IntentType.PRICE_INQUIRY
        >>> print(result.confidence)
        0.85
    """
    service = UnifiedIntentService.get_instance()
    return service.recognize(query, context)


def get_intent_service() -> UnifiedIntentService:
    """获取意图识别服务实例"""
    return UnifiedIntentService.get_instance()


# ============== 向后兼容别名 ==============

# 原有类的别名，保持向后兼容
def get_enhanced_intent_recognizer() -> UnifiedIntentService:
    """向后兼容: 获取增强意图识别器"""
    return UnifiedIntentService.get_instance()


def get_bert_intent_recognizer():
    """向后兼容: 获取BERT意图识别器"""
    service = UnifiedIntentService.get_instance()
    return service.bert_recognizer


# 导出
__all__ = [
    'UnifiedIntentService',
    'UnifiedIntentResult',
    'SentimentAnalyzer',
    'UrgencyAnalyzer',
    'RuleBasedMatcher',
    'recognize_intent',
    'get_intent_service',
    # 向后兼容
    'get_enhanced_intent_recognizer',
    'get_bert_intent_recognizer',
]
