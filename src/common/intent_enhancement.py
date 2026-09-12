"""
意图识别增强模块

提供深度语义理解、上下文推理、意图消歧能力
解决传统关键词匹配的局限性
"""

import logging
import re
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime

from .types.intent import IntentType, SentimentType, UrgencyLevel, IntentResult
from .unified_intent_service import UnifiedIntentService, get_enhanced_intent_recognizer
# 向后兼容别名
EnhancedIntentRecognizer = UnifiedIntentService

logger = logging.getLogger(__name__)


@dataclass
class SemanticUnderstanding:
    """语义理解结果"""
    core_intent: str
    sub_intent: Optional[str]
    entities: Dict[str, List[str]]
    modifiers: List[str]  # 修饰词，如"最"、"非常"
    question_type: str  # what/how/why/whether
    completeness: float  # 问题完整性评分
    ambiguity_score: float  # 歧义度评分


class IntentEnhancer:
    """
    意图增强器
    
    核心能力：
    1. 语义深度理解 - 超越关键词匹配
    2. 上下文推理 - 基于历史对话理解省略句
    3. 意图消歧 - 处理多意图冲突
    4. 问题补全 - 补全省略的问题成分
    5. 答案重组指导 - 为答案重组提供语义标签
    """

    # 问题类型模式
    QUESTION_PATTERNS = {
        "what": ["什么", "哪些", "哪个", "what", "which"],
        "how": ["怎么", "如何", "怎样", "how", "way"],
        "why": ["为什么", "为何", "为啥", "why", "reason"],
        "whether": ["吗", "么", "是否", "能不能", "可不可以", "whether", "if"],
        "where": ["哪里", "何处", "where"],
        "when": ["什么时候", "何时", "when"],
        "who": ["谁", "哪位", "who"],
        "how_much": ["多少", "多少钱", "价格", "how much", "price"]
    }

    # 修饰词库
    MODIFIERS = {
        "degree": ["最", "非常", "特别", "极其", "十分", "很", "太"],
        "comparison": ["更", "更加", "比较", "相比", "对比"],
        "negation": ["不", "没", "无", "非", "未"],
        "emphasis": ["到底", "究竟", "真的", "确实", "绝对"]
    }

    # 上下文继承规则
    CONTEXT_INHERITANCE_RULES = {
        "product_inquiry": ["price_inquiry", "service_inquiry", "comparison"],
        "price_inquiry": ["purchase_intent", "cooperation_intent"],
        "service_inquiry": ["technical_support", "training_request"],
        "greeting": ["product_inquiry", "service_inquiry"]
    }

    def __init__(self, base_recognizer: EnhancedIntentRecognizer = None):
        """
        初始化意图增强器
        
        Args:
            base_recognizer: 基础意图识别器实例
        """
        self.base_recognizer = base_recognizer or EnhancedIntentRecognizer()

    def enhance_understanding(
        self,
        message: str,
        conversation_history: List[Dict] = None,
        context_entities: Dict = None
    ) -> Tuple[IntentResult, SemanticUnderstanding]:
        """
        增强意图理解
        
        Args:
            message: 用户消息
            conversation_history: 对话历史
            context_entities: 上下文实体
            
        Returns:
            (IntentResult, SemanticUnderstanding): 增强后的意图识别结果和语义理解
        """
        # 1. 基础意图识别
        base_result = self.base_recognizer.recognize(message)  # 移除 conversation_history 参数
        
        # 2. 语义深度分析
        semantic = self._analyze_semantics(message, conversation_history)
        
        # 3. 上下文推理（处理省略句）
        if conversation_history and len(conversation_history) > 0:
            base_result = self._infer_from_context(base_result, message, conversation_history)
        
        # 4. 实体抽取增强
        if context_entities:
            base_result.entities = self._merge_entities(base_result.entities, context_entities)
        
        # 5. 意图消歧（处理多意图冲突）
        base_result = self._disambiguate(base_result, semantic)
        
        # 6. 调整置信度
        base_result.confidence = self._calculate_enhanced_confidence(
            base_result, semantic, conversation_history
        )
        
        # 7. 添加语义标签用于答案重组
        base_result.metadata = {
            "semantic": {
                "question_type": semantic.question_type,
                "completeness": semantic.completeness,
                "ambiguity": semantic.ambiguity_score,
                "core_intent": semantic.core_intent,
                "sub_intent": semantic.sub_intent
            },
            "entities_detailed": semantic.entities,
            "modifiers": semantic.modifiers
        }
        
        return base_result, semantic

    def _analyze_semantics(
        self,
        message: str,
        conversation_history: List[Dict] = None
    ) -> SemanticUnderstanding:
        """
        深度语义分析
        
        分析问题的：
        - 核心意图和子意图
        - 实体和修饰词
        - 问题类型（what/how/why）
        - 问题完整性
        - 歧义程度
        """
        message_lower = message.lower()
        
        # 1. 识别问题类型
        question_type = "unknown"
        for qtype, patterns in self.QUESTION_PATTERNS.items():
            if any(p in message_lower for p in patterns):
                question_type = qtype
                break
        
        # 2. 提取修饰词
        modifiers = []
        for mod_type, words in self.MODIFIERS.items():
            for word in words:
                if word in message:
                    modifiers.append(f"{mod_type}:{word}")
        
        # 3. 提取实体（简化版，实际应该用 NER）
        entities = self._extract_entities(message)
        
        # 4. 识别核心意图和子意图
        core_intent, sub_intent = self._identify_intents(message, entities)
        
        # 5. 计算问题完整性
        completeness = self._calculate_completeness(message, conversation_history)
        
        # 6. 计算歧义度
        ambiguity = self._calculate_ambiguity(message, core_intent, sub_intent)
        
        return SemanticUnderstanding(
            core_intent=core_intent,
            sub_intent=sub_intent,
            entities=entities,
            modifiers=modifiers,
            question_type=question_type,
            completeness=completeness,
            ambiguity_score=ambiguity
        )

    def _extract_entities(self, message: str) -> Dict[str, List[str]]:
        """提取实体"""
        entities = {
            "product": [],
            "feature": [],
            "price": [],
            "action": [],
            "object": []
        }
        
        # 产品相关
        product_keywords = ["系统", "软件", "工具", "平台", "产品", "功能"]
        for kw in product_keywords:
            if kw in message:
                entities["product"].append(kw)
        
        # 价格相关
        price_keywords = ["价格", "多少钱", "费用", "收费", "优惠", "折扣"]
        for kw in price_keywords:
            if kw in message:
                entities["price"].append(kw)
        
        # 动作相关
        action_keywords = ["购买", "使用", "操作", "学习", "了解", "咨询"]
        for kw in action_keywords:
            if kw in message:
                entities["action"].append(kw)
        
        return entities

    def _identify_intents(
        self,
        message: str,
        entities: Dict
    ) -> Tuple[str, Optional[str]]:
        """识别核心意图和子意图"""
        message_lower = message.lower()
        
        # 核心意图识别
        if any(kw in message_lower for kw in ["什么", "干嘛", "做啥"]):
            core_intent = "product_inquiry"
        elif any(kw in message_lower for kw in ["价格", "多少钱", "费用", "收费"]):
            core_intent = "price_inquiry"
        elif any(kw in message_lower for kw in ["怎么", "如何", "使用", "操作"]):
            core_intent = "service_inquiry"
        elif any(kw in message_lower for kw in ["购买", "买", "订购", "下单"]):
            core_intent = "purchase_intent"
        elif any(kw in message_lower for kw in ["你好", "您好", "嗨", "hello"]):
            core_intent = "greeting"
        else:
            core_intent = "general_inquiry"
        
        # 子意图识别
        sub_intent = None
        if "对比" in message_lower or "比较" in message_lower:
            sub_intent = "comparison"
        elif "为什么" in message_lower or "为何" in message_lower:
            sub_intent = "reason_inquiry"
        elif "哪里" in message_lower or "何处" in message_lower:
            sub_intent = "location_inquiry"
        
        return core_intent, sub_intent

    def _calculate_completeness(
        self,
        message: str,
        conversation_history: List[Dict] = None
    ) -> float:
        """
        计算问题完整性评分 (0-1)
        
        完整性评估标准：
        - 有明确的主语/宾语：+0.3
        - 有明确的动作/意图：+0.3
        - 有具体的对象/实体：+0.2
        - 语法完整：+0.2
        """
        score = 0.0
        message_lower = message.lower()
        
        # 检查主语/宾语
        if any(kw in message_lower for kw in ["产品", "系统", "功能", "价格", "服务"]):
            score += 0.3
        
        # 检查动作/意图
        if any(kw in message_lower for kw in ["是什么", "怎么用", "多少钱", "好不好"]):
            score += 0.3
        
        # 检查具体对象
        if len(message) > 5:
            score += 0.2
        
        # 语法完整性
        if message.endswith(("?", "？", "吗", "么")):
            score += 0.2
        
        # 如果是省略句但有上下文，提高评分
        if score < 0.5 and conversation_history and len(conversation_history) > 0:
            score = max(score, 0.6)  # 有上下文支持的省略句
        
        return min(score, 1.0)

    def _calculate_ambiguity(
        self,
        message: str,
        core_intent: str,
        sub_intent: Optional[str]
    ) -> float:
        """
        计算歧义度评分 (0-1, 越高越歧义)
        
        歧义来源：
        - 多意图冲突
        - 缺少关键信息
        - 指代不明
        """
        ambiguity = 0.0
        
        # 消息过短
        if len(message) < 5:
            ambiguity += 0.3
        
        # 只有疑问词没有具体内容
        if message in ["什么？", "怎么了", "是吗", "对吧"]:
            ambiguity += 0.5
        
        # 多个意图冲突
        if core_intent == "general_inquiry":
            ambiguity += 0.3
        
        return min(ambiguity, 1.0)

    def _infer_from_context(
        self,
        intent_result: IntentResult,
        message: str,
        conversation_history: List[Dict]
    ) -> IntentResult:
        """
        基于上下文推理意图
        
        处理场景：
        1. 省略句："等于多少" → 需要从上文找到数学表达式
        2. 指代："这个多少钱" → 需要从上文找到指代的产品
        3. 追问："那怎么使用呢" → 延续上文话题
        """
        if not conversation_history:
            return intent_result
        
        # 获取最近的 bot 回复和用户消息
        last_bot_msg = None
        last_user_msg = None
        for msg in reversed(conversation_history[-5:]):
            if msg.get("role") == "assistant" and not last_bot_msg:
                last_bot_msg = msg.get("content", "")
            elif msg.get("role") == "user" and not last_user_msg:
                last_user_msg = msg.get("content", "")
        
        # 处理省略句
        if self._is_elliptical_sentence(message):
            intent_result = self._handle_elliptical_sentence(
                intent_result, message, last_user_msg, last_bot_msg
            )
        
        # 处理指代
        if self._has_pronoun_reference(message):
            intent_result = self._handle_pronoun_reference(
                intent_result, message, last_bot_msg
            )
        
        return intent_result

    def _is_elliptical_sentence(self, message: str) -> bool:
        """判断是否为省略句"""
        message_stripped = message.strip()
        
        # 只有疑问词
        if message_stripped in ["等于多少", "多少钱", "怎么用", "是什么", "为什么", "多少", "如何"]:
            return True
        
        # 以"那"、"呢"、"吗"开头的追问
        if message_stripped.startswith(("那", "呢", "那这", "那那")):
            return True
        
        # 缺少主语的疑问句
        if re.match(r"^(怎么|如何|多少|什么|为什么).{0,5}(吗|呢|？|\?)$", message_stripped):
            return True
        
        return False

    def _has_pronoun_reference(self, message: str) -> bool:
        """检查是否有代词指代"""
        pronouns = ["这个", "那个", "这些", "那些", "它", "他们", "她们", "它们", "此", "该"]
        return any(pronoun in message for pronoun in pronouns)

    def _handle_elliptical_sentence(
        self,
        intent_result: IntentResult,
        message: str,
        last_user_msg: str,
        last_bot_msg: str
    ) -> IntentResult:
        """处理省略句"""
        # 从上文补充缺失的信息
        if last_user_msg:
            # 合并上下文信息用于意图理解
            combined = f"{last_user_msg} {message}"
            
            # 重新识别意图
            new_result = self.base_recognizer.recognize(combined)
            
            # 保留原始消息，但使用增强后的意图
            if new_result.confidence > intent_result.confidence:
                intent_result.primaryIntent = new_result.primaryIntent
                intent_result.confidence = new_result.confidence
                intent_result.keywords.extend(new_result.keywords)
        
        return intent_result

    def _handle_pronoun_reference(
        self,
        intent_result: IntentResult,
        message: str,
        last_bot_msg: str
    ) -> IntentResult:
        """处理代词指代"""
        if not last_bot_msg:
            return intent_result
        
        # 从最近的 bot 回复中提取可能的指代对象
        # 这里简化处理，实际应该用核心ference resolution
        
        return intent_result

    def _merge_entities(
        self,
        entities1: Dict,
        entities2: Dict
    ) -> Dict:
        """合并实体"""
        merged = {}
        all_keys = set(entities1.keys()) | set(entities2.keys())
        
        for key in all_keys:
            merged[key] = list(set(
                entities1.get(key, []) + entities2.get(key, [])
            ))
        
        return merged

    def _disambiguate(
        self,
        intent_result: IntentResult,
        semantic: SemanticUnderstanding
    ) -> IntentResult:
        """意图消歧"""
        # 如果有多个可能的意图，选择置信度最高的
        # 如果置信度接近，考虑问题类型和上下文
        
        if semantic.ambiguity_score > 0.5:
            # 高歧义度，需要额外处理
            logger.debug(f"检测到高歧义消息：{semantic.ambiguity_score}")
        
        return intent_result

    def _calculate_enhanced_confidence(
        self,
        intent_result: IntentResult,
        semantic: SemanticUnderstanding,
        conversation_history: List[Dict] = None
    ) -> float:
        """
        计算增强后的置信度
        
        考虑因素：
        - 基础置信度
        - 问题完整性
        - 歧义度
        - 上下文支持
        """
        base_conf = intent_result.confidence
        
        # 完整性奖励
        completeness_bonus = semantic.completeness * 0.1
        
        # 歧义度惩罚
        ambiguity_penalty = semantic.ambiguity_score * 0.15
        
        # 上下文支持奖励
        context_bonus = 0.0
        if conversation_history and len(conversation_history) > 0:
            context_bonus = 0.1
        
        enhanced_conf = base_conf + completeness_bonus - ambiguity_penalty + context_bonus
        return min(max(enhanced_conf, 0.0), 1.0)


def get_intent_enhancer() -> IntentEnhancer:
    """获取意图增强器实例"""
    return IntentEnhancer()
