"""
增强指代消解模块

在上下文理解模块基础上，提供更强大的指代消解能力
支持实体栈追踪、跨轮次消解、LLM辅助消解

原理：
1. 维护对话中的实体栈（最近提到的实体）
2. 代词出现时，从实体栈中查找最近匹配的实体
3. 低置信度时使用LLM进行语义消解
"""

import re
from loguru import logger
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from collections import deque



@dataclass
class EntityMention:
    """实体提及记录"""
    entity_text: str
    entity_type: str
    turn_index: int
    confidence: float = 1.0
    source_message: str = ""


@dataclass
class CoreferenceResult:
    """指代消解结果"""
    original_text: str
    resolved_text: str
    resolved_entities: Dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    method: str = "rule"


class EnhancedCoreferenceResolver:
    """
    增强指代消解器

    特点：
    1. 实体栈追踪：维护最近提到的实体列表
    2. 类型匹配：代词类型与实体类型匹配
    3. LLM辅助：低置信度时使用LLM消解
    """

    PRONOUN_ENTITY_TYPE_MAP = {
        "他": "person",
        "她": "person",
        "它": "object",
        "这个": "demonstrative",
        "那个": "demonstrative",
        "这些": "demonstrative_plural",
        "那些": "demonstrative_plural",
        "此": "demonstrative",
        "其": "demonstrative",
        "该": "demonstrative",
    }

    ENTITY_TYPE_KEYWORDS = {
        "object": ["产品", "系统", "软件", "平台", "工具", "功能", "服务", "方案", "版本", "套餐"],
        "person": ["经理", "客服", "同事", "老板", "客户", "朋友", "伙伴"],
        "price": ["价格", "费用", "收费", "报价", "定价", "套餐", "优惠", "折扣"],
        "service": ["售后", "支持", "培训", "教程", "维护", "升级"],
        "company": ["公司", "企业", "厂商", "品牌", "团队"],
        "feature": ["获客", "营销", "推广", "数据分析", "群发", "意向分析", "客户管理"],
    }

    ENTITY_PATTERNS = [
        (r'(基础版|专业版|企业版|旗舰版|标准版|高级版)', "version"),
        (r'(抖音|小红书|快手|微博|微信|B站|知乎)', "platform"),
        (r'(\d+元|\d+块|\d+万)', "price_value"),
        (r'(获客|营销|推广|数据分析|群发|意向分析|客户管理)', "feature"),
    ]
    GENERIC_ENTITY_TERMS = {
        "产品", "系统", "软件", "平台", "工具", "功能", "服务", "方案", "版本", "套餐",
        "价格", "费用", "收费", "报价", "定价", "优惠", "折扣",
        "售后", "支持", "培训", "教程", "维护", "升级",
        "公司", "企业", "厂商", "品牌", "团队",
    }

    def __init__(self, llm_service=None, max_entity_stack: int = 20):
        """
        初始化指代消解器

        Args:
            llm_service: LLM服务实例
            max_entity_stack: 实体栈最大容量
        """
        self.llm_service = llm_service
        self.max_entity_stack = max_entity_stack
        self.entity_stacks: Dict[str, deque] = {}
        self._max_sessions = 500

    def _call_llm(self, prompt: str) -> str:
        """兼容不同LLM服务接口的统一调用方法（委托给call_llm_safe）"""
        from src.common.utils import call_llm_safe
        from src.config.settings import REPLY_LLM_TIMEOUT_SECONDS
        return call_llm_safe(self.llm_service, prompt, timeout=REPLY_LLM_TIMEOUT_SECONDS)

    def _cleanup_old_sessions(self):
        """清理过多的会话实体栈，防止内存泄漏"""
        if len(self.entity_stacks) > self._max_sessions:
            keys_to_remove = list(self.entity_stacks.keys())[:len(self.entity_stacks) - self._max_sessions + 100]
            for key in keys_to_remove:
                del self.entity_stacks[key]

    def resolve(
        self,
        text: str,
        session_id: str,
        conversation_history: Optional[List[Dict]] = None,
        allow_llm: bool = True,
        grounded_entities: Optional[List[str]] = None,
    ) -> CoreferenceResult:
        """
        消解文本中的指代

        Args:
            text: 当前文本
            session_id: 会话ID
            conversation_history: 对话历史

        Returns:
            消解结果
        """
        if session_id not in self.entity_stacks:
            self.entity_stacks[session_id] = deque(maxlen=self.max_entity_stack)
            self._cleanup_old_sessions()

        self._seed_grounded_entities(session_id, grounded_entities or [])

        if conversation_history:
            self._update_entity_stack(session_id, conversation_history)

        has_pronoun = any(p in text for p in self.PRONOUN_ENTITY_TYPE_MAP)
        if not has_pronoun:
            self._extract_and_add_entities(session_id, text, 0)
            return CoreferenceResult(
                original_text=text,
                resolved_text=text,
                confidence=1.0,
                method="no_pronoun"
            )

        rule_result = self._rule_resolve(text, session_id)

        if rule_result.confidence >= 0.7:
            self._extract_and_add_entities(session_id, rule_result.resolved_text, 0)
            return rule_result

        if allow_llm and self.llm_service:
            llm_result = self._llm_resolve(text, session_id, conversation_history)
            if llm_result and llm_result.confidence > rule_result.confidence:
                self._extract_and_add_entities(session_id, llm_result.resolved_text, 0)
                return llm_result

        self._extract_and_add_entities(session_id, text, 0)
        return rule_result

    def _seed_grounded_entities(self, session_id: str, grounded_entities: List[str]):
        """把最近知识结论中的线路实体注入当前会话栈，增强多轮代词承接。"""
        if session_id not in self.entity_stacks:
            self.entity_stacks[session_id] = deque(maxlen=self.max_entity_stack)
        entity_stack = self.entity_stacks[session_id]
        existing = {
            (entity.entity_text, entity.entity_type)
            for entity in entity_stack
        }
        for entity_text in grounded_entities or []:
            entity_text = str(entity_text or "").strip()
            if not entity_text:
                continue
            pair = (entity_text, "object")
            if pair in existing:
                continue
            entity_stack.append(
                EntityMention(
                    entity_text=entity_text,
                    entity_type="object",
                    turn_index=-1,
                    confidence=0.98,
                    source_message="grounded_context",
                )
            )
            existing.add(pair)

    def _rule_resolve(self, text: str, session_id: str) -> CoreferenceResult:
        """
        基于规则的指代消解

        Args:
            text: 当前文本
            session_id: 会话ID

        Returns:
            消解结果
        """
        resolved_text = text
        resolved_entities = {}
        total_confidence = 0.0
        pronoun_count = 0

        entity_stack = self.entity_stacks.get(session_id, deque())

        for pronoun, expected_type in self.PRONOUN_ENTITY_TYPE_MAP.items():
            if pronoun not in text:
                continue

            pronoun_count += 1
            best_entity = None
            best_confidence = 0.0

            for reverse_idx, entity in enumerate(reversed(entity_stack)):
                type_match = self._check_type_match(expected_type, entity.entity_type)
                confidence = entity.confidence * (0.8 if type_match else 0.3)
                # 反向枚举保证越新的实体权重越高，避免 index() 命中首次位置造成最近性失真。
                recency_boost = max(1.0 - reverse_idx * 0.08, 0.45)
                confidence *= recency_boost
                if self._is_generic_entity_text(entity.entity_text):
                    confidence *= 0.55

                if confidence > best_confidence:
                    best_confidence = confidence
                    best_entity = entity

            if best_entity and best_confidence >= 0.35:
                resolved_text = resolved_text.replace(pronoun, best_entity.entity_text, 1)
                resolved_entities[pronoun] = best_entity.entity_text
                total_confidence += best_confidence

        avg_confidence = total_confidence / pronoun_count if pronoun_count > 0 else 0.0

        return CoreferenceResult(
            original_text=text,
            resolved_text=resolved_text,
            resolved_entities=resolved_entities,
            confidence=avg_confidence,
            method="rule"
        )

    def _llm_resolve(self, text: str, session_id: str, history: Optional[List[Dict]] = None) -> Optional[CoreferenceResult]:
        """
        使用LLM进行指代消解

        Args:
            text: 当前文本
            session_id: 会话ID
            history: 对话历史

        Returns:
            消解结果
        """
        if not history:
            return None

        recent_history = history[-6:]
        history_text = "\n".join([
            f"{'用户' if msg.get('direction') == 'inbound' else '助手'}: {msg.get('content', '')}"
            for msg in recent_history
        ])

        prompt = f"""在以下对话中，将最新消息中的代词（它、这个、那个等）替换为具体指代的内容。
只输出替换后的结果，不要解释。

对话历史：
{history_text}

最新消息：{text}

替换后的消息："""

        try:
            response = self._call_llm(prompt)
            if response:
                resolved = response.strip().strip('"\'""''')
                if resolved and resolved != text:
                    return CoreferenceResult(
                        original_text=text,
                        resolved_text=resolved,
                        confidence=0.85,
                        method="llm"
                    )
        except Exception as e:
            logger.warning(f"LLM指代消解失败: {e}")

        return None

    def _update_entity_stack(self, session_id: str, history: List[Dict]):
        """从对话历史更新实体栈"""
        if session_id not in self.entity_stacks:
            self.entity_stacks[session_id] = deque(maxlen=self.max_entity_stack)

        entity_stack = self.entity_stacks[session_id]

        for i, msg in enumerate(history):
            if msg.get('direction') != 'inbound':
                continue
            content = msg.get('content', '')
            self._extract_and_add_entities(session_id, content, i)

    def _extract_and_add_entities(self, session_id: str, text: str, turn_index: int):
        """从文本中提取实体并添加到栈"""
        entity_stack = self.entity_stacks.get(session_id)
        if not entity_stack:
            return

        for entity_type, keywords in self.ENTITY_TYPE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    entity_stack.append(EntityMention(
                        entity_text=keyword,
                        entity_type=entity_type,
                        turn_index=turn_index,
                        confidence=0.9,
                        source_message=text[:50]
                    ))

        for pattern, entity_type in self.ENTITY_PATTERNS:
            matches = re.findall(pattern, text)
            for match in matches:
                entity_stack.append(EntityMention(
                    entity_text=match,
                    entity_type=entity_type,
                    turn_index=turn_index,
                    confidence=0.95,
                    source_message=text[:50]
                ))

    def _check_type_match(self, pronoun_type: str, entity_type: str) -> bool:
        """检查代词类型与实体类型是否匹配"""
        type_compatibility = {
            "object": ["object", "feature", "version", "platform"],
            "demonstrative": ["object", "price", "service", "feature", "version", "platform", "company"],
            "demonstrative_plural": ["feature", "object"],
            "person": ["person", "company"],
        }

        compatible = type_compatibility.get(pronoun_type, [])
        return entity_type in compatible

    def _is_generic_entity_text(self, entity_text: str) -> bool:
        text = str(entity_text or "").strip()
        return text in self.GENERIC_ENTITY_TERMS

    def clear_session(self, session_id: str):
        """清除会话实体栈"""
        if session_id in self.entity_stacks:
            del self.entity_stacks[session_id]


_coreference_resolver: Optional[EnhancedCoreferenceResolver] = None


def get_coreference_resolver(llm_service=None) -> EnhancedCoreferenceResolver:
    """获取指代消解器单例"""
    global _coreference_resolver
    if _coreference_resolver is None:
        _coreference_resolver = EnhancedCoreferenceResolver(llm_service)
    return _coreference_resolver
