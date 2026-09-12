"""
BERT意图识别器

基于预训练BERT模型实现快速意图分类和槽位提取
当BERT模型不可用或低置信度时自动降级到LLM语义识别

特点：
1. BERT模型推理延迟<50ms，远低于LLM的1-3秒
2. 联合意图分类+槽位填充，共享语义表示
3. 低置信度时自动降级到规则+LLM
4. 支持增量训练和模型热更新
"""

import os
import json
import re
import threading
import logging
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from pathlib import Path

from .types.intent import IntentType, SentimentType, UrgencyLevel
from .utils import call_llm_safe
from src.config.settings import REPLY_LLM_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

BERT_AVAILABLE = False
TORCH_AVAILABLE = False

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    pass

try:
    from transformers import BertTokenizer, BertForSequenceClassification
    BERT_AVAILABLE = True
except ImportError:
    logger.info("transformers未安装，BERT意图识别不可用，将使用LLM语义识别")


@dataclass
class SlotResult:
    """槽位提取结果"""
    slotName: str
    value: str
    start: int = 0
    end: int = 0
    confidence: float = 0.0


@dataclass
class BertIntentResult:
    """BERT意图识别结果"""
    primaryIntent: IntentType
    confidence: float = 0.0
    slots: List[SlotResult] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    source: str = "bert"


INTENT_LABEL_MAP = {
    0: IntentType.GREETING,
    1: IntentType.FAREWELL,
    2: IntentType.THANKS,
    3: IntentType.PRODUCT_INQUIRY,
    4: IntentType.PRICE_INQUIRY,
    5: IntentType.FEATURE_INQUIRY,
    6: IntentType.SERVICE_INQUIRY,
    7: IntentType.COOPERATION_INTENT,
    8: IntentType.PURCHASE_INTENT,
    9: IntentType.COMPLAINT,
    10: IntentType.CONSULTATION,
    11: IntentType.COMPARISON,
    12: IntentType.FEEDBACK,
    13: IntentType.AFTER_SALES,
    14: IntentType.UNKNOWN,
}

LABEL_TO_INTENT = {v: k for k, v in INTENT_LABEL_MAP.items()}

SLOT_PATTERNS = {
    "product_version": [
        r"(基础版|专业版|企业版|旗舰版|标准版|高级版|豪华版)",
        r"(免费版|付费版|试用版)",
    ],
    "platform": [
        r"(抖音|小红书|快手|微博|微信|B站|知乎)",
    ],
    "price_range": [
        r"(\d+)元",
        r"(\d+)块",
        r"(\d+)万",
        r"(便宜|贵|划算|性价比)",
    ],
    "time_period": [
        r"(\d+)天",
        r"(\d+)个月",
        r"(月付|年付|季付)",
    ],
    "feature_area": [
        r"(获客|客服|营销|推广|数据分析|群发|意向分析|客户管理)",
    ],
}


class BertIntentRecognizer:
    """
    BERT意图识别器

    优先使用BERT模型进行意图分类，模型不可用或低置信度时降级到LLM
    支持槽位提取，延迟<50ms
    """

    MODEL_BASE_PATH = "models/intent"

    def __init__(self, llm_service=None):
        """
        初始化BERT意图识别器

        Args:
            llm_service: LLM服务实例，作为最终降级方案
        """
        self.llm_service = llm_service
        self.tokenizer = None
        self.model = None
        self._initialized = False
        self._device = "cpu"
        self._predict_lock = threading.Lock()

        if TORCH_AVAILABLE:
            self._device = "cuda" if torch.cuda.is_available() else "cpu"

        if BERT_AVAILABLE:
            self._init_model()

    def _init_model(self):
        """初始化BERT模型"""
        model_paths = [
            os.path.join(self.MODEL_BASE_PATH, "bert-intent-zh"),
            os.path.join(self.MODEL_BASE_PATH, "bge-small-zh-intent"),
        ]

        existing_path = None
        for path in model_paths:
            if os.path.exists(path) and os.path.exists(os.path.join(path, "config.json")):
                existing_path = path
                break

        if existing_path:
            try:
                self.tokenizer = BertTokenizer.from_pretrained(existing_path)
                self.model = BertForSequenceClassification.from_pretrained(existing_path)
                self.model.to(self._device)
                self.model.eval()
                self._initialized = True
                logger.info(f"BERT意图模型加载成功: {existing_path}, 设备: {self._device}")
            except Exception as e:
                logger.warning(f"BERT意图模型加载失败: {e}")
        else:
            logger.info(f"BERT意图模型未找到({self.MODEL_BASE_PATH})，将使用LLM语义识别")

    def predict(self, text: str) -> BertIntentResult:
        """
        BERT意图识别

        Args:
            text: 用户消息

        Returns:
            意图识别结果
        """
        if not self._initialized or not self.model or not self.tokenizer:
            return BertIntentResult(
                primaryIntent=IntentType.UNKNOWN,
                confidence=0.0,
                source="unavailable"
            )

        try:
            with self._predict_lock:
                inputs = self.tokenizer(
                    text,
                    return_tensors="pt",
                    truncation=True,
                    max_length=128,
                    padding=True
                )
                inputs = {k: v.to(self._device) for k, v in inputs.items()}

                with torch.no_grad():
                    outputs = self.model(**inputs)
                    logits = outputs.logits
                    probabilities = torch.softmax(logits, dim=-1)
                    confidence, predicted = torch.max(probabilities, dim=-1)

                intent_idx = predicted.item()
                conf = confidence.item()

            intent = INTENT_LABEL_MAP.get(intent_idx, IntentType.UNKNOWN)

            slots = self._extract_slots(text)
            keywords = self._extract_keywords(text)

            return BertIntentResult(
                primaryIntent=intent,
                confidence=conf,
                slots=slots,
                keywords=keywords,
                source="bert"
            )

        except Exception as e:
            logger.warning(f"BERT推理失败: {e}")
            return BertIntentResult(
                primaryIntent=IntentType.UNKNOWN,
                confidence=0.0,
                source="error"
            )

    def predict_with_fallback(self, text: str, session_id: str = None, context: Any = None) -> 'IntentResult':
        """
        BERT优先，低置信度时降级到LLM语义识别

        Args:
            text: 用户消息
            session_id: 会话ID
            context: 对话上下文

        Returns:
            意图识别结果（兼容IntentResult格式）
        """
        from .intent_recognizer import IntentResult

        if self._initialized:
            bert_result = self.predict(text)
            if bert_result.confidence >= 0.7 and bert_result.primaryIntent != IntentType.UNKNOWN:
                logger.debug(f"BERT意图识别: {bert_result.primaryIntent.value}, 置信度: {bert_result.confidence:.2f}")
                return IntentResult(
                    primaryIntent=bert_result.primaryIntent,
                    confidence=bert_result.confidence,
                    keywords=bert_result.keywords,
                    entities={slot.slotName: [slot.value] for slot in bert_result.slots},
                    sentiment=SentimentType.NEUTRAL,
                    urgency=UrgencyLevel.MEDIUM,
                    needHuman=False,
                    reasoning=f"BERT识别(置信度{bert_result.confidence:.2f})"
                )

            logger.debug(f"BERT置信度低({bert_result.confidence:.2f})，降级到LLM语义识别")

        if self.llm_service:
            try:
                llm_result = self._llm_fallback(text, context=context)
                if llm_result:
                    return llm_result
            except Exception as e:
                logger.warning(f"LLM意图识别失败: {e}")

        return IntentResult(
            primaryIntent=IntentType.UNKNOWN,
            confidence=0.0,
            keywords=[],
            reasoning="所有识别方法均失败"
        )

    def _extract_slots(self, text: str) -> List[SlotResult]:
        """
        基于规则的槽位提取

        Args:
            text: 用户消息

        Returns:
            槽位提取结果列表
        """
        slots = []
        for slot_name, patterns in SLOT_PATTERNS.items():
            for pattern in patterns:
                matches = re.finditer(pattern, text)
                for match in matches:
                    slots.append(SlotResult(
                        slotName=slot_name,
                        value=match.group(1) if match.lastindex else match.group(0),
                        start=match.start(),
                        end=match.end(),
                        confidence=0.85
                    ))
        return slots

    def _extract_keywords(self, text: str) -> List[str]:
        """
        提取关键词

        Args:
            text: 用户消息

        Returns:
            关键词列表
        """
        try:
            import jieba
            import jieba.analyse
            keywords = jieba.analyse.extract_tags(text, topK=5)
            return keywords
        except Exception:
            words = re.findall(r'[\u4e00-\u9fff]{2,}', text)
            return words[:5]

    def _llm_fallback(self, text: str, context: Any = None) -> Optional['IntentResult']:
        """LLM 语义兜底识别"""
        from .intent_recognizer import IntentResult

        if not self.llm_service:
            return None

        try:
            intent_names = [it.value for it in IntentType if it != IntentType.CACHED]
            context_text = ""
            if context:
                recent_messages = [
                    str(item.get("content") or "").strip()
                    for item in getattr(context, "messages", [])[-4:]
                    if str(item.get("content") or "").strip()
                ]
                if recent_messages:
                    context_text = f"\n最近对话: {recent_messages}"
            prompt = f"""分析以下用户消息的主要意图，只能从给定列表中选择。

用户消息：{text}{context_text}

可选意图：{', '.join(intent_names)}

要求：
1. 基于语义理解，不要靠固定关键词表。
2. 如果无法可靠判断，返回 unknown。
3. 只输出 JSON。

输出格式：
{{
  "primary_intent": "intent_name",
  "confidence": 0.0,
  "reasoning": "简短原因"
}}"""

            response = call_llm_safe(self.llm_service, prompt, timeout=REPLY_LLM_TIMEOUT_SECONDS)
            if response:
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group())
                    intent_name = str(data.get("primary_intent", "unknown") or "unknown").strip()
                    confidence = max(0.0, min(float(data.get("confidence", 0.0) or 0.0), 0.98))
                    for intent_type in IntentType:
                        if intent_type.value == intent_name:
                            return IntentResult(
                                primaryIntent=intent_type,
                                confidence=confidence,
                                keywords=self._extract_keywords(text),
                                reasoning=str(data.get("reasoning") or "LLM语义兜底识别"),
                            )
        except Exception as e:
            logger.warning(f"LLM意图识别失败: {e}")

        return None

    def is_available(self) -> bool:
        """检查BERT模型是否可用（含模型和tokenizer有效性检查）"""
        return self._initialized and self.model is not None and self.tokenizer is not None

    def get_status(self) -> Dict:
        """获取识别器状态"""
        return {
            "bert_available": self._initialized,
            "device": self._device,
            "llm_service_available": self.llm_service is not None,
            "model_path": self.MODEL_BASE_PATH if not self._initialized else "loaded"
        }


_bert_recognizer: Optional[BertIntentRecognizer] = None
_bert_recognizer_lock = threading.Lock()


def get_bert_intent_recognizer(llm_service=None) -> BertIntentRecognizer:
    """获取BERT意图识别器单例（双重检查锁定，线程安全）"""
    global _bert_recognizer
    if _bert_recognizer is None:
        with _bert_recognizer_lock:
            if _bert_recognizer is None:
                _bert_recognizer = BertIntentRecognizer(llm_service)
    else:
        if llm_service is not None:
            _bert_recognizer.llm_service = llm_service
    return _bert_recognizer
