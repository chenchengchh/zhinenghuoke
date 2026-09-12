"""
答案重组优化模块

基于检索到的知识，深度加工生成自然、准确、个性化的回复
拒绝直接返回索引内容，实现真正的智能答案生成
"""

import logging
import re
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from .types.intent import IntentType, SentimentType, UrgencyLevel
from .knowledge_base import KnowledgeItem
from src.common.utils import call_llm_safe

logger = logging.getLogger(__name__)


@dataclass
class AnswerComponent:
    """答案组件"""
    content: str
    component_type: str  # direct_answer, explanation, example, suggestion, follow_up
    confidence: float
    source: Optional[KnowledgeItem] = None
    priority: int = 1  # 优先级，数字越小优先级越高


@dataclass
class ReorganizedAnswer:
    """重组后的答案"""
    final_answer: str
    components: List[AnswerComponent]
    confidence: float
    sources: List[KnowledgeItem]
    answer_type: str  # direct, explanatory, suggestive, exploratory
    completeness: float  # 答案完整性
    personalization: Dict[str, Any] = field(default_factory=dict)


class AnswerReorganizer:
    """
    答案重组器
    
    核心能力：
    1. 多知识源融合 - 合并多个相关知识
    2. 答案结构化 - 按逻辑组织答案
    3. 个性化定制 - 根据用户上下文调整
    4. 自然语言生成 - 生成流畅的回复
    5. 质量验证 - 确保答案准确性
    """

    # 答案模板
    ANSWER_TEMPLATES = {
        "direct": {
            "opening": ["", "您好，", "根据您的提问，"],
            "core": ["{content}", "答案是：{content}", "具体来说是：{content}"],
            "closing": ["", "如有其他问题，随时问我。", "希望这能帮到您！"]
        },
        "explanatory": {
            "opening": ["您好，关于这个问题，", "让我为您详细解释一下，", "这个问题涉及到，"],
            "core": ["{content}", "详细说明：{content}", "具体来说：{content}"],
            "closing": ["简单来说，就是这样。", "如有不明白的地方，可以继续问我。", "希望这个解释对您有帮助！"]
        },
        "suggestive": {
            "opening": ["建议您，", "您可以考虑，", "我的建议是，"],
            "core": ["{content}", "具体方案：{content}", "推荐做法：{content}"],
            "closing": ["您觉得这个方案如何？", "如果需要更详细的方案，我可以继续帮您规划。", "有任何问题都可以随时问我。"]
        },
        "exploratory": {
            "opening": ["这个问题很有意思，", "让我想想，", "关于这个问题，"],
            "core": ["{content}", "可以先这样理解：{content}", "能确认的部分是：{content}"],
            "closing": ["如果您需要更详细的信息，建议咨询专业人士。", "我还在不断学习中，如有不准确的地方请见谅。", "还有其他想了解的吗？"]
        }
    }

    # 连接词库
    CONNECTORS = {
        "addition": ["而且", "此外", "另外", "同时", "还有"],
        "contrast": ["但是", "然而", "不过", "相反", "另一方面"],
        "cause": ["因为", "由于", "原因是", "之所以"],
        "effect": ["所以", "因此", "于是", "结果是", "导致"],
        "example": ["比如", "例如", "举例来说", "举个例子"],
        "summary": ["总之", "总的来说", "综上所述", "简而言之"]
    }

    def __init__(self, llm_provider=None):
        """
        初始化答案重组器
        
        Args:
            llm_provider: LLM 提供者，用于生成自然语言
        """
        self.llm_provider = llm_provider

    def reorganize(
        self,
        retrieved_knowledge: List[Tuple[KnowledgeItem, float]],
        intent_result: Any,
        conversation_history: List[Dict] = None,
        customer_data: Dict = None,
        original_question: str = None
    ) -> ReorganizedAnswer:
        """
        重组答案
        
        Args:
            retrieved_knowledge: 检索到的知识及其匹配度
            intent_result: 意图识别结果
            conversation_history: 对话历史
            customer_data: 客户数据
            original_question: 原始问题
            
        Returns:
            ReorganizedAnswer: 重组后的答案
        """
        if not retrieved_knowledge or len(retrieved_knowledge) == 0:
            return self._create_fallback_answer(original_question)
        
        # 1. 分析和筛选知识
        filtered_knowledge = self._filter_and_rank_knowledge(retrieved_knowledge, intent_result)
        
        # 2. 提取答案组件
        components = self._extract_answer_components(filtered_knowledge, intent_result)
        
        # 3. 确定答案类型
        answer_type = self._determine_answer_type(intent_result, components)
        
        # 4. 结构化组织答案
        structured_answer = self._structure_answer(components, answer_type)
        
        # 5. 个性化调整
        personalized_answer = self._personalize_answer(
            structured_answer, customer_data, conversation_history
        )
        
        # 6. 生成自然语言回复
        final_answer = self._generate_natural_language(
            personalized_answer, answer_type, intent_result
        )
        
        # 7. 质量验证
        quality_score = self._validate_answer_quality(
            final_answer, original_question, intent_result
        )
        
        return ReorganizedAnswer(
            final_answer=final_answer,
            components=components,
            confidence=self._calculate_overall_confidence(components, quality_score),
            sources=[item for item, _ in filtered_knowledge[:5]],
            answer_type=answer_type,
            completeness=self._calculate_completeness(components),
            personalization={
                "has_customer_data": customer_data is not None,
                "has_history": conversation_history is not None and len(conversation_history) > 0
            }
        )

    def _filter_and_rank_knowledge(
        self,
        retrieved_knowledge: List[Tuple[KnowledgeItem, float]],
        intent_result: Any
    ) -> List[Tuple[KnowledgeItem, float]]:
        """
        筛选和排序知识
        
        考虑因素：
        - 匹配分数
        - 知识启用状态
        - 意图相关性
        - 知识时效性
        """
        filtered = []
        
        for item, score in retrieved_knowledge:
            if not item.enabled:
                continue
            
            # 意图相关性加权
            intent_boost = self._calculate_intent_relevance_boost(item, intent_result)
            adjusted_score = score * intent_boost
            
            # 时效性加权
            recency_boost = self._calculate_recency_boost(item)
            adjusted_score *= recency_boost
            
            filtered.append((item, adjusted_score))
        
        # 按调整后的分数排序
        filtered.sort(key=lambda x: x[1], reverse=True)
        
        return filtered[:10]  # 返回 top 10

    def _calculate_intent_relevance_boost(
        self,
        item: KnowledgeItem,
        intent_result: Any
    ) -> float:
        """计算意图相关性加权"""
        boost = 1.0
        
        if not intent_result:
            return boost
        
        # 检查分类匹配
        intent_type_map = {
            "product_inquiry": ["产品相关", "功能介绍"],
            "price_inquiry": ["价格相关", "促销活动"],
            "service_inquiry": ["服务相关", "售后服务"],
            "cooperation_intent": ["合作加盟"]
        }
        
        intent_value = intent_result.primaryIntent.value if hasattr(intent_result, 'primaryIntent') else str(intent_result)
        
        for intent_key, categories in intent_type_map.items():
            if intent_key in intent_value and item.category in categories:
                boost *= 1.5
                break
        
        return boost

    def _calculate_recency_boost(self, item: KnowledgeItem) -> float:
        """计算时效性加权"""
        if not hasattr(item, 'updated_at') or not item.updated_at:
            return 1.0
        
        try:
            updated = datetime.fromisoformat(item.updated_at)
            days_old = (datetime.now() - updated).days
            
            if days_old < 7:
                return 1.3
            elif days_old < 30:
                return 1.1
            elif days_old < 90:
                return 1.0
            else:
                return 0.9
        except Exception:
            return 1.0

    def _extract_answer_components(
        self,
        filtered_knowledge: List[Tuple[KnowledgeItem, float]],
        intent_result: Any
    ) -> List[AnswerComponent]:
        """提取答案组件"""
        components = []
        
        for i, (item, score) in enumerate(filtered_knowledge):
            # 直接答案
            if i == 0:
                components.append(AnswerComponent(
                    content=item.answer,
                    component_type="direct_answer",
                    confidence=score,
                    source=item,
                    priority=1
                ))
            
            # 补充说明
            if hasattr(item, 'related_questions') and item.related_questions:
                components.append(AnswerComponent(
                    content="相关问题：" + "，".join(item.related_questions[:3]),
                    component_type="explanation",
                    confidence=score * 0.8,
                    source=item,
                    priority=2
                ))
            
            # 示例
            if hasattr(item, 'examples') and item.examples:
                components.append(AnswerComponent(
                    content=item.examples[0] if isinstance(item.examples, list) else item.examples,
                    component_type="example",
                    confidence=score * 0.7,
                    source=item,
                    priority=3
                ))
        
        return components

    def _determine_answer_type(
        self,
        intent_result: Any,
        components: List[AnswerComponent]
    ) -> str:
        """确定答案类型"""
        if not intent_result:
            return "explanatory"
        
        intent_value = intent_result.primaryIntent.value if hasattr(intent_result, 'primaryIntent') else str(intent_result)
        
        # 简单事实性问题 → direct
        if intent_value in ["greeting", "thanks", "confirmation"]:
            return "direct"
        
        # 需要解释的问题 → explanatory
        if intent_value in ["product_inquiry", "service_inquiry", "price_inquiry"]:
            return "explanatory"
        
        # 建议性问题 → suggestive
        if intent_value in ["cooperation_intent", "comparison"]:
            return "suggestive"
        
        # 复杂或开放性问题 → exploratory
        return "exploratory"

    def _structure_answer(
        self,
        components: List[AnswerComponent],
        answer_type: str
    ) -> Dict[str, Any]:
        """结构化组织答案"""
        # 按优先级排序
        components.sort(key=lambda x: x.priority)
        
        structured = {
            "opening": "",
            "core_content": [],
            "supplementary": [],
            "closing": ""
        }
        
        # 提取核心内容
        for comp in components:
            if comp.component_type == "direct_answer":
                structured["core_content"].append(comp.content)
            elif comp.component_type in ["explanation", "example"]:
                structured["supplementary"].append(comp.content)
        
        # 添加开场白和结束语
        template = self.ANSWER_TEMPLATES.get(answer_type, self.ANSWER_TEMPLATES["explanatory"])
        structured["opening"] = template["opening"][0]
        structured["closing"] = template["closing"][0]
        
        return structured

    def _personalize_answer(
        self,
        structured_answer: Dict[str, Any],
        customer_data: Dict = None,
        conversation_history: List[Dict] = None
    ) -> Dict[str, Any]:
        """个性化调整答案"""
        if not customer_data and not conversation_history:
            return structured_answer
        
        personalized = structured_answer.copy()
        
        # 根据客户数据调整
        if customer_data:
            customer_name = customer_data.get("nickname") or customer_data.get("name")
            if customer_name:
                personalized["opening"] = f"嘿 {customer_name}~ "
        
        # 根据对话历史调整
        if conversation_history and len(conversation_history) > 0:
            # 检查是否是连续对话
            last_msg = conversation_history[-1] if conversation_history else None
            if last_msg and last_msg.get("role") == "assistant":
                # 连续对话，使用更自然的连接
                personalized["opening"] = ""
        
        return personalized

    def _generate_natural_language(
        self,
        personalized_answer: Dict[str, Any],
        answer_type: str,
        intent_result: Any
    ) -> str:
        """生成自然语言回复"""
        parts = []
        
        # 开场白
        if personalized_answer.get("opening"):
            parts.append(personalized_answer["opening"])
        
        # 核心内容
        core_content = personalized_answer.get("core_content", [])
        if core_content:
            for i, content in enumerate(core_content):
                if i == 0:
                    parts.append(content)
                else:
                    connector = self.CONNECTORS["addition"][0]
                    parts.append(f"{connector}，{content}")
        
        # 补充内容
        supplementary = personalized_answer.get("supplementary", [])
        if supplementary:
            parts.append(self.CONNECTORS["example"][0] + "：")
            parts.extend(supplementary)
        
        # 结束语
        if personalized_answer.get("closing"):
            parts.append(personalized_answer["closing"])
        
        # 拼接最终答案
        final = "".join(parts)
        
        # 如果有 LLM，可以用 LLM 润色
        if self.llm_provider and len(final) > 50:
            try:
                prompt = f"""请润色以下客服回复，使其更自然、友好、专业：

原始回复：{final}

要求：
1. 保持原意不变
2. 语言自然流畅
3. 语气友好专业
4. 适合在线客服场景

润色后的回复："""
                
                from src.config.settings import REPLY_LLM_TIMEOUT_SECONDS
                polished = call_llm_safe(self.llm_provider, prompt, timeout=REPLY_LLM_TIMEOUT_SECONDS)
                if polished and len(polished.strip()) > 0:
                    return polished.strip()
            except Exception as e:
                logger.debug(f"LLM 润色失败：{e}")
        
        return final

    def _validate_answer_quality(
        self,
        final_answer: str,
        original_question: str,
        intent_result: Any
    ) -> float:
        """验证答案质量"""
        score = 0.0
        
        # 长度合理性
        if 20 <= len(final_answer) <= 500:
            score += 0.3
        
        # 包含关键词
        if original_question:
            question_words = set(original_question)
            answer_words = set(final_answer)
            overlap = len(question_words & answer_words)
            if overlap > 0:
                score += 0.3
        
        # 语气友好
        friendly_words = ["您", "请", "欢迎", "随时", "帮助"]
        if any(word in final_answer for word in friendly_words):
            score += 0.2
        
        # 有结束语
        if final_answer.endswith(("。", "!", "！", "~")):
            score += 0.2
        
        return min(score, 1.0)

    def _calculate_overall_confidence(
        self,
        components: List[AnswerComponent],
        quality_score: float
    ) -> float:
        """计算整体置信度"""
        if not components:
            return 0.0
        
        avg_confidence = sum(comp.confidence for comp in components) / len(components)
        return (avg_confidence + quality_score) / 2

    def _calculate_completeness(
        self,
        components: List[AnswerComponent]
    ) -> float:
        """计算答案完整性"""
        if not components:
            return 0.0
        
        # 有直接答案
        has_direct = any(c.component_type == "direct_answer" for c in components)
        
        # 有补充说明
        has_supplementary = any(c.component_type in ["explanation", "example"] for c in components)
        
        # 有多个组件
        has_multiple = len(components) > 1
        
        score = 0.0
        if has_direct:
            score += 0.5
        if has_supplementary:
            score += 0.3
        if has_multiple:
            score += 0.2
        
        return min(score, 1.0)

    def _create_fallback_answer(self, original_question: str) -> ReorganizedAnswer:
        """创建兜底答案"""
        fallback_answer = "您好，我还在不断学习中，这个问题我暂时无法给出准确答案。建议您咨询人工客服获取更专业的帮助~"
        
        return ReorganizedAnswer(
            final_answer=fallback_answer,
            components=[],
            confidence=0.3,
            sources=[],
            answer_type="exploratory",
            completeness=0.3
        )


def get_answer_reorganizer(llm_provider=None) -> AnswerReorganizer:
    """获取答案重组器实例"""
    return AnswerReorganizer(llm_provider)
