"""
意图识别统一编排器

整合 BERT 意图识别、增强意图识别、意图增强器三套引擎，
通过置信度分层策略选择最优结果。
"""
import logging
from typing import Dict, Optional, Any, List

from .types.intent import IntentResult, IntentType

logger = logging.getLogger(__name__)


class IntentOrchestrator:
    """
    意图识别统一编排器

    策略：
    - 高置信度（>=0.7）：BERT 快速路径直接返回
    - 中置信度（0.3-0.6）：增强意图识别 + IntentEnhancer
    - 低置信度（<0.3）：关键词回退
    """

    BERT_HIGH_CONFIDENCE = 0.7
    ENHANCER_LOW_THRESHOLD = 0.3
    ENHANCER_HIGH_THRESHOLD = 0.6

    def __init__(self):
        self._bert_recognizer = None
        self._enhanced_recognizer = None
        self._enhancer = None
        self._keyword_rules = self._init_keyword_rules()

    def _init_keyword_rules(self) -> Dict[str, List[str]]:
        return {
            "greeting": ["你好", "您好", "hi", "hello", "在吗"],
            "farewell": ["再见", "拜拜", "bye"],
            "thanks": ["谢谢", "感谢", "多谢"],
            "complaint": ["投诉", "不满", "差评", "太差"],
            "refund_request": ["退款", "退钱", "退货"],
        }

    # ------------------------------------------------------------------
    # P2-8: 多意图检测（参考 ECLM 意图链）
    # ------------------------------------------------------------------

    # 多意图关键词规则（跨行业通用），用于检测消息中的次要意图
    MULTI_INTENT_KEYWORD_RULES: Dict[str, List[str]] = {
        "price_inquiry": ["多少钱", "价格", "价位", "报价", "费用", "收费", "票价", "怎么收费"],
        "refund_request": ["退款", "退钱", "退货", "退订"],
        "return_request": ["退货", "换货", "退换"],
        "complaint": ["投诉", "不满", "差评", "太差", "坑"],
        "discount_request": ["折扣", "优惠", "便宜", "减免", "优惠券", "促销"],
        "payment_method": ["怎么付款", "支付方式", "分期", "转账"],
        "shipping_inquiry": ["发货", "快递", "物流", "配送", "多久到"],
        "warranty_inquiry": ["保修", "质保", "售后", "维护"],
        "contact_inquiry": ["联系方式", "电话", "微信", "地址", "怎么联系"],
        "comparison": ["对比", "比较", "区别", "哪个好", "vs"],
        "feature_inquiry": ["功能", "特点", "能做什么", "支持什么"],
        "service_inquiry": ["服务", "售后", "客服", "支持"],
    }

    def _detect_secondary_intents(
        self,
        message: str,
        primary_intent: IntentType,
    ) -> List[IntentType]:
        """检测消息中的次要意图（多意图识别）。

        Args:
            message: 用户消息
            primary_intent: 已识别的主意图

        Returns:
            List[IntentType]: 次要意图列表（不含主意图，最多 3 个）
        """
        message_lower = str(message or "").lower().strip()
        if not message_lower:
            return []

        detected: List[IntentType] = []
        primary_value = primary_intent.value if primary_intent else ""

        for intent_name, keywords in self.MULTI_INTENT_KEYWORD_RULES.items():
            # 跳过主意图
            if intent_name == primary_value:
                continue
            # 检查是否命中关键词
            for kw in keywords:
                if kw in message_lower:
                    try:
                        intent_type = IntentType(intent_name)
                        if intent_type not in detected:
                            detected.append(intent_type)
                    except ValueError:
                        continue
                    break  # 命中一个关键词即可，避免重复

        # 限制最多 3 个次要意图
        return detected[:3]

    def _get_bert_recognizer(self):
        if self._bert_recognizer is None:
            try:
                from .unified_intent_service import get_bert_intent_recognizer
                self._bert_recognizer = get_bert_intent_recognizer()
            except Exception as e:
                logger.warning(f"BERT 意图识别器加载失败: {e}")
        return self._bert_recognizer

    def _get_enhanced_recognizer(self):
        if self._enhanced_recognizer is None:
            try:
                from .unified_intent_service import get_enhanced_intent_recognizer
                self._enhanced_recognizer = get_enhanced_intent_recognizer()
            except Exception as e:
                logger.warning(f"增强意图识别器加载失败: {e}")
        return self._enhanced_recognizer

    def _get_enhancer(self):
        if self._enhancer is None:
            try:
                from .intent_enhancement import IntentEnhancer
                self._enhancer = IntentEnhancer()
            except Exception as e:
                logger.warning(f"意图增强器加载失败: {e}")
        return self._enhancer

    def recognize(self, message: str, context: Optional[Dict] = None) -> IntentResult:
        context = context or {}

        bert_result = self._try_bert_recognize(message)
        if bert_result and bert_result.confidence >= self.BERT_HIGH_CONFIDENCE:
            # P2-8: 填充次要意图（兼容无 secondaryIntents 属性的结果对象）
            secondary = self._detect_secondary_intents(
                message, getattr(bert_result, "primaryIntent", IntentType.UNKNOWN)
            )
            if hasattr(bert_result, "secondaryIntents"):
                if not bert_result.secondaryIntents:
                    bert_result.secondaryIntents = secondary
            return bert_result

        result = self._try_enhanced_recognize(message, context)
        if result is None:
            result = self._keyword_fallback(message)

        if result and self.ENHANCER_LOW_THRESHOLD <= result.confidence < self.ENHANCER_HIGH_THRESHOLD:
            enhanced = self._try_enhance(message, result, context)
            if enhanced is not None:
                result = enhanced

        if result is None:
            result = IntentResult(
                primaryIntent=IntentType.UNKNOWN,
                confidence=0.0,
                reasoning="all_engines_failed",
            )

        # P2-8: 填充次要意图（所有路径统一处理，兼容无 secondaryIntents 属性的结果对象）
        secondary = self._detect_secondary_intents(
            message, getattr(result, "primaryIntent", None) or getattr(result, "primary_intent", IntentType.UNKNOWN)
        )
        if hasattr(result, "secondaryIntents"):
            if not result.secondaryIntents:
                result.secondaryIntents = secondary
        elif hasattr(result, "metadata") and isinstance(result.metadata, dict):
            # UnifiedIntentResult 等无 secondaryIntents 属性的对象，存入 metadata
            if not result.metadata.get("secondary_intents"):
                result.metadata["secondary_intents"] = [s.value for s in secondary]

        return result

    def _try_bert_recognize(self, message: str) -> Optional[IntentResult]:
        recognizer = self._get_bert_recognizer()
        if recognizer is None:
            return None
        try:
            return recognizer.predict_with_fallback(message)
        except Exception as e:
            logger.warning(f"BERT 意图识别异常: {e}")
            return None

    def _try_enhanced_recognize(self, message: str, context: Dict) -> Optional[IntentResult]:
        recognizer = self._get_enhanced_recognizer()
        if recognizer is None:
            return None
        try:
            return recognizer.recognize(message, context)
        except Exception as e:
            logger.warning(f"增强意图识别异常: {e}")
            return None

    def _try_enhance(self, message: str, result: IntentResult, context: Dict) -> Optional[IntentResult]:
        enhancer = self._get_enhancer()
        if enhancer is None:
            return None
        try:
            return enhancer.enhance_understanding(message, result, context)
        except Exception as e:
            logger.warning(f"意图增强异常: {e}")
            return None

    def _keyword_fallback(self, message: str) -> IntentResult:
        message_lower = message.lower()
        for intent_name, keywords in self._keyword_rules.items():
            for kw in keywords:
                if kw in message_lower:
                    try:
                        intent_type = IntentType(intent_name)
                    except ValueError:
                        continue
                    return IntentResult(
                        primaryIntent=intent_type,
                        confidence=0.4,
                        keywords=[kw],
                        reasoning="keyword_fallback",
                    )
        return IntentResult(
            primaryIntent=IntentType.UNKNOWN,
            confidence=0.1,
            reasoning="keyword_fallback_no_match",
        )


from functools import lru_cache

@lru_cache(maxsize=1)
def get_intent_orchestrator() -> IntentOrchestrator:
    return IntentOrchestrator()
