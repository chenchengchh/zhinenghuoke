"""
统一置信度计算工具

整合了各模块中的置信度计算逻辑，提供统一的计算方法
"""
from enum import Enum
from typing import List, Dict, Optional, Any
from dataclasses import dataclass


class MessageCategory(str, Enum):
    """
    消息分类枚举

    用于在置信度计算中区分消息类型，给予不同的基础分。
    解决"短消息/无关键词消息置信度永远为 0"的问题。
    """
    GREETING_SOCIAL = "greeting_social"   # 问候/寒暄：你好/在吗/嗨/hi
    FAREWELL_THANKS = "farewell_thanks"   # 道别/致谢：再见/谢谢
    INQUIRY = "inquiry"                   # 询问：多少钱/怎么报名
    PURCHASE = "purchase"                 # 购买意向：怎么付款
    COMPLAINT = "complaint"               # 投诉/质疑
    CHITCHAT = "chitchat"                 # 闲聊
    UNKNOWN = "unknown"                   # 未知


# 社交问候关键词集合（高频、简单、必然要走快速通道）
_SOCIAL_GREETING_KEYWORDS = frozenset({
    "你好", "您好", "在吗", "在么", "嗨", "hi", "hello", "hey",
    "哈喽", "哈罗", "早上好", "下午好", "晚上好", "在不在",
})

# 道别/致谢关键词
_FAREWELL_THANKS_KEYWORDS = frozenset({
    "谢谢", "感谢", "多谢", "辛苦了", "再见", "拜拜", "bye", "thanks", "thank",
})

# 投诉/质疑关键词
_COMPLAINT_KEYWORDS = frozenset({
    "投诉", "退款", "退货", "骗子", "不靠谱", "骗人", "假的", "差评", "投诉你",
})

# 购买意向关键词
_PURCHASE_KEYWORDS = frozenset({
    "购买", "下单", "付款", "怎么付", "多少钱", "怎么买", "我要", "我要买",
})


def classify_message_category(message: str) -> MessageCategory:
    """
    快速将消息分类，返回 MessageCategory 枚举值。
    纯字符串匹配，毫秒级，不依赖任何 LLM。
    """
    if not message:
        return MessageCategory.UNKNOWN
    text = message.strip().lower()
    if not text:
        return MessageCategory.UNKNOWN

    # 短消息（≤6 字符）优先做社交意图识别，避免误伤
    if len(text) <= 6:
        if text in _SOCIAL_GREETING_KEYWORDS or any(kw in text for kw in _SOCIAL_GREETING_KEYWORDS):
            return MessageCategory.GREETING_SOCIAL
        if text in _FAREWELL_THANKS_KEYWORDS or any(kw in text for kw in _FAREWELL_THANKS_KEYWORDS):
            return MessageCategory.FAREWELL_THANKS

    # 投诉/质疑关键词匹配
    for kw in _COMPLAINT_KEYWORDS:
        if kw in text:
            return MessageCategory.COMPLAINT

    # 购买意向关键词匹配
    for kw in _PURCHASE_KEYWORDS:
        if kw in text:
            return MessageCategory.PURCHASE

    # 道别/致谢关键词匹配
    for kw in _FAREWELL_THANKS_KEYWORDS:
        if kw in text:
            return MessageCategory.FAREWELL_THANKS

    # 社交问候关键词匹配
    for kw in _SOCIAL_GREETING_KEYWORDS:
        if kw in text:
            return MessageCategory.GREETING_SOCIAL

    return MessageCategory.INQUIRY if text else MessageCategory.UNKNOWN


# 不同消息类型的基础置信度分（无关键词匹配时的兜底分）
_CATEGORY_BASE_SCORES: Dict[MessageCategory, float] = {
    MessageCategory.GREETING_SOCIAL: 0.65,  # 社交问候天然高置信
    MessageCategory.FAREWELL_THANKS: 0.65,
    MessageCategory.COMPLAINT: 0.7,         # 投诉需要关注
    MessageCategory.PURCHASE: 0.6,           # 购买意向明确
    MessageCategory.CHITCHAT: 0.4,
    MessageCategory.INQUIRY: 0.4,
    MessageCategory.UNKNOWN: 0.0,
}


@dataclass
class ConfidenceContext:
    """置信度计算上下文"""
    base_score: float = 0.0
    match_count: int = 0
    result_count: int = 0
    answer_length: int = 0
    has_llm: bool = False
    has_kg: bool = False
    second_score_gap: float = 0.0


class ConfidenceCalculator:
    """
    统一置信度计算器

    提供多种场景的置信度计算方法：
    1. 意图识别置信度
    2. RAG检索置信度
    3. 答案生成置信度
    """

    # 统一阈值常量
    MODULAR_RAG_THRESHOLD = 0.7
    AGENTIC_RAG_THRESHOLD = 0.7
    HIGH_CONFIDENCE_THRESHOLD = 0.8
    LOW_CONFIDENCE_THRESHOLD = 0.5

    @staticmethod
    def calculate_intent_confidence(
        primary_score: float,
        keyword_count: int,
        is_high_confidence_intent: bool = False,
        second_score: float = 0.0,
        message: str = "",
        message_category: Optional[MessageCategory] = None,
    ) -> float:
        """
        计算意图识别置信度

        Args:
            primary_score: 主要意图分数
            keyword_count: 匹配关键词数量
            is_high_confidence_intent: 是否为高置信意图（问候/投诉/购买意向等）
            second_score: 第二意图分数
            message: 原始消息文本（用于自动分类）
            message_category: 消息分类（可选，传入则跳过自动分类）

        Returns:
            float: 置信度 (0.0 - 1.0)
        """
        # 1. 基础分：根据消息分类给出兜底分
        category = message_category
        if category is None and message:
            category = classify_message_category(message)

        # 当没有关键词匹配时（primary_score=0, keyword_count=0），使用消息分类基础分
        # 这样"你好"等短消息不再 confidence=0
        if primary_score <= 0 and keyword_count <= 0 and category is not None:
            confidence = _CATEGORY_BASE_SCORES.get(category, 0.0)
        else:
            # 旧逻辑保留，作为有明确关键词时的增强计算
            confidence = min(primary_score / 5.0, 0.6)
            if keyword_count >= 3:
                confidence += 0.15
            elif keyword_count >= 2:
                confidence += 0.1
            elif keyword_count >= 1:
                confidence += 0.05
            # 消息分类给额外加分（与 keyword_count 互补）
            if category in {MessageCategory.GREETING_SOCIAL, MessageCategory.FAREWELL_THANKS}:
                confidence += 0.1

        if is_high_confidence_intent:
            confidence += 0.1

        if primary_score > 0 and second_score > 0:
            score_gap = primary_score - second_score
            if score_gap > 2.0:
                confidence += 0.15
            elif score_gap > 1.0:
                confidence += 0.1
            elif score_gap > 0.5:
                confidence += 0.05

        return min(confidence, 1.0)
    
    @staticmethod
    def calculate_rag_confidence(
        results: List[Dict],
        decision_confidence: float = 0.5
    ) -> float:
        """
        计算RAG检索置信度
        
        Args:
            results: 检索结果列表
            decision_confidence: 检索决策置信度
            
        Returns:
            float: 置信度 (0.0 - 1.0)
        """
        if not results:
            return 0.3
        
        confidence = decision_confidence * 0.3
        
        best_score = results[0].get("score", 0) if results else 0
        if best_score > 50:
            confidence += 0.35
        elif best_score > 35:
            confidence += 0.25
        elif best_score > 20:
            confidence += 0.15
        elif best_score > 10:
            confidence += 0.05
        
        if len(results) >= 3:
            confidence += 0.1
        elif len(results) >= 2:
            confidence += 0.05
        
        best_answer = results[0].get("answer", "") if results else ""
        if len(best_answer) > 50:
            confidence += 0.1
        elif len(best_answer) > 20:
            confidence += 0.05
        
        if len(results) >= 2:
            scores = [r.get("score", 0) for r in results[:3]]
            if scores[0] > 0:
                score_ratio = scores[-1] / scores[0] if scores[0] > 0 else 0
                if score_ratio > 0.7:
                    confidence += 0.05
        
        return min(confidence, 1.0)
    
    @staticmethod
    def calculate_agentic_confidence(
        retrieved_results: List[Dict],
        kg_info: Optional[Dict] = None,
        has_llm: bool = False,
        reasoning_score: float = 0.0
    ) -> float:
        """
        计算Agentic RAG置信度
        
        Args:
            retrieved_results: 检索结果
            kg_info: 知识图谱信息
            has_llm: 是否使用LLM增强
            reasoning_score: 推理分数
            
        Returns:
            float: 置信度 (0.0 - 1.0)
        """
        confidence = 0.2
        
        if retrieved_results:
            confidence += 0.2
            
            best_score = retrieved_results[0].get("score", 0) if retrieved_results else 0
            if best_score > 50:
                confidence += 0.2
            elif best_score > 35:
                confidence += 0.15
            elif best_score > 20:
                confidence += 0.1
            
            if len(retrieved_results) >= 3:
                confidence += 0.1
            
            best_answer = retrieved_results[0].get("answer", "") if retrieved_results else ""
            if len(best_answer) > 100:
                confidence += 0.1
            elif len(best_answer) > 50:
                confidence += 0.05
        
        if kg_info and kg_info.get("entities"):
            confidence += 0.1
            if len(kg_info.get("entities", [])) >= 3:
                confidence += 0.05
        
        if reasoning_score > 0.7:
            confidence += 0.1
        elif reasoning_score > 0.5:
            confidence += 0.05
        
        if has_llm and retrieved_results:
            confidence += 0.05
        
        return min(confidence, 1.0)
    
    @staticmethod
    def calculate_strategy_confidence(
        results: List[Dict],
        strategy_type: str = "simple"
    ) -> float:
        """
        计算策略置信度
        
        Args:
            results: 策略执行结果
            strategy_type: 策略类型
            
        Returns:
            float: 置信度 (0.0 - 1.0)
        """
        if not results:
            return 0.3
        
        base_confidence = {
            "simple": 0.5,
            "hybrid": 0.6,
            "multi_hop": 0.7,
            "llm_generate": 0.8
        }.get(strategy_type, 0.5)
        
        confidence = base_confidence
        
        if results:
            best_score = results[0].get("score", 0) if results else 0
            if best_score > 40:
                confidence += 0.2
            elif best_score > 25:
                confidence += 0.1
            
            if len(results) >= 2:
                confidence += 0.1
        
        return min(confidence, 1.0)


# 全局实例
confidence_calculator = ConfidenceCalculator()


def get_confidence_calculator() -> ConfidenceCalculator:
    """获取置信度计算器实例"""
    return confidence_calculator
