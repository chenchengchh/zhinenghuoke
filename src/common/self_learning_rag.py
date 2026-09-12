"""
自动学习RAG系统 (SelfLearningRAGSystem)
能够自动检测未知问题、抽取知识、验证并入库

架构：
1. UnknownQuestionDetector - 未知问题检测
2. KnowledgeExtractor - 知识抽取
3. ConversationLearner - 对话学习器
4. KnowledgeValidator - 知识验证
5. AutoEnricher - 自动丰富器

增强模块：
6. LLMKnowledgeExtractor - LLM增强知识抽取
7. KnowledgeQualityScorer - 知识质量评分
8. KnowledgeDecayManager - 知识衰减管理
9. ActiveLearner - 主动学习机制
"""

import os
import re
import json
import uuid
import time
import threading
import logging
import tempfile
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from pathlib import Path

from .knowledge_base import KnowledgeBaseManager
from .types.knowledge import KnowledgeItem, ExtractedKnowledge
from src.infrastructure.runtime_paths import get_chroma_dir

logger = logging.getLogger(__name__)

_LEARNING_CONTACT_POLLUTION_MARKERS = (
    "留个接收资料的联系方式",
    "接收资料的联系方式",
    "方便接收资料",
    "我继续为您处理",
    "我接着帮您整理",
    "继续帮您跟进后续安排",
    "完整行程、报价明细",
    "留个联系方式",
    "锁定名额",
)

# 知识库注入攻击关键词：用于拦截自学习过程中的恶意输入
_INJECTION_BLOCK_KEYWORDS = (
    "联系方式", "电话", "微信", "QQ", "邮箱",
    "加我", "私聊", "转账", "汇款", "打款",
    "赌博", "博彩", "色情", "暴力", "毒品",
    "100元", "500元", "1000元",  # 通用价格注入
)


def _is_potential_injection(text: str) -> bool:
    """
    检测可能的知识库注入攻击。

    Args:
        text: 待检测的文本内容

    Returns:
        bool: 如果包含注入攻击特征则返回 True
    """
    if not text:
        return False
    return any(kw in str(text) for kw in _INJECTION_BLOCK_KEYWORDS)


def _looks_like_contact_pollution(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text:
        return False
    return any(marker in text for marker in _LEARNING_CONTACT_POLLUTION_MARKERS)


# 知识库注入攻击黑名单关键词
# 涵盖：联系方式/引流、违规内容（赌博/色情/毒品）、可疑价格等
_INJECTION_BLOCK_KEYWORDS = (
    # 联系方式与引流
    "联系方式", "电话", "微信", "QQ", "邮箱", "手机号",
    "加我", "私聊", "转账", "汇款", "打款", "加好友",
    # 违规内容
    "赌博", "博彩", "色情", "暴力", "毒品", "违禁",
    # 价格注入（防止恶意标价污染知识库）
    "100元", "500元", "1000元", "免费送", "1元",
    # 越权指令（防止 prompt 注入）
    "忽略之前", "ignore previous", "ignore above", "system prompt",
)


def _is_potential_injection(text: str) -> bool:
    """检测可能的知识库注入攻击。

    用于在 `learn_from_conversation` 入口处阻断含联系方式/违规内容/越权指令的输入，
    防止恶意用户通过对话把垃圾或攻击性内容写入知识库。
    """
    if not text:
        return False
    return any(kw in str(text) for kw in _INJECTION_BLOCK_KEYWORDS)


class LearningStage(Enum):
    """学习阶段"""
    DETECT = "detect"           # 检测未知问题
    EXTRACT = "extract"         # 抽取知识
    VALIDATE = "validate"       # 验证知识
    ENRICH = "enrich"          # 丰富知识
    STORE = "store"            # 入库


@dataclass
class UnknownQuestion:
    """未知问题记录"""
    question: str
    conversation_id: str
    customer_id: str
    timestamp: datetime
    context: List[str]
    suggested_answer: Optional[str] = None
    detected_intent: str = ""
    detected_category: str = ""
    confidence: float = 0.0
    stage: LearningStage = LearningStage.DETECT
    attempts: int = 0


@dataclass
class LearningResult:
    """学习结果"""
    new_knowledge_added: int
    knowledge_updated: int
    questions_learned: int
    accuracy_improvement: float
    details: List[str]


class UnknownQuestionDetector:
    """
    未知问题检测器

    检测RAG系统无法回答或回答质量差的问题

    阶段三改造：关键词从 YAML 配置加载，类属性作为默认值兜底。
    """

    # 类属性作为兜底默认值（如果配置文件不可用）
    LOW_CONFIDENCE_KEYWORDS = ["抱歉", "无法", "不知道", "没有找到", "无法处理"]
    QUESTION_PATTERNS = ["怎么", "如何", "什么", "为什么", "哪里", "多少", "是不是", "能不能"]

    def __init__(self, confidence_threshold: float = None):
        # 尝试从配置加载阈值，未配置时使用类属性默认值
        from .learning_config import get_learning_config_loader
        loader = get_learning_config_loader()
        self.confidence_threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else loader.confidence_threshold
        )
        # 从配置加载关键词（如可用）
        try:
            cfg_keywords = loader.low_confidence_keywords
            if cfg_keywords:
                # 合并：类属性 + 配置（配置优先）
                self._low_confidence_keywords = list(dict.fromkeys(
                    cfg_keywords + self.LOW_CONFIDENCE_KEYWORDS
                ))
            else:
                self._low_confidence_keywords = self.LOW_CONFIDENCE_KEYWORDS
            cfg_patterns = loader.question_patterns
            if cfg_patterns:
                self._question_patterns = list(dict.fromkeys(
                    cfg_patterns + self.QUESTION_PATTERNS
                ))
            else:
                self._question_patterns = self.QUESTION_PATTERNS
        except Exception as e:
            logger.warning(f"加载自学习配置失败，使用类属性默认值: {e}")
            self._low_confidence_keywords = self.LOW_CONFIDENCE_KEYWORDS
            self._question_patterns = self.QUESTION_PATTERNS

        self.unknown_questions: List[UnknownQuestion] = []
        self._max_unknown_questions = 200

    def detect(
        self,
        query: str,
        rag_result,
        conversation_history: List[Dict] = None
    ) -> Tuple[bool, Optional[UnknownQuestion]]:
        """
        检测是否为未知问题

        Returns:
            Tuple[是否未知, 未知问题记录]
        """
        is_unknown = False
        unknown_question = None

        if rag_result.confidence < self.confidence_threshold:
            is_unknown = True
            logger.info(f"检测到未知问题（置信度低）: {query[:30]}...")

        if not rag_result.sources or len(rag_result.sources) == 0:
            is_unknown = True
            logger.info(f"检测到未知问题（无匹配源）: {query[:30]}...")

        if any(keyword in rag_result.answer for keyword in self._low_confidence_keywords):
            if rag_result.confidence < 0.7:
                is_unknown = True
                logger.info(f"检测到未知问题（包含未知关键词）: {query[:30]}...")

        if is_unknown:
            context = []
            if conversation_history:
                context = [msg.get("content", "") for msg in conversation_history[-3:]]

            unknown_question = UnknownQuestion(
                question=query,
                conversation_id="",
                customer_id="",
                timestamp=datetime.now(),
                context=context,
                detected_intent=self._detect_intent(query),
                detected_category=self._detect_category(query),
                confidence=rag_result.confidence
            )
            self.unknown_questions.append(unknown_question)
            if len(self.unknown_questions) > self._max_unknown_questions:
                self.unknown_questions = self.unknown_questions[-self._max_unknown_questions:]

        return is_unknown, unknown_question

    def _detect_intent(self, query: str) -> str:
        """检测意图"""
        query_lower = query.lower()

        intent_patterns = {
            "price_inquiry": ["价格", "多少钱", "费用", "收费", "门票"],
            "attractions_inquiry": ["景点", "好玩", "去哪", "推荐"],
            "itinerary_inquiry": ["线路", "行程", "攻略", "几天"],
            "food_inquiry": ["美食", "吃什么", "火锅", "小面"],
            "accommodation_inquiry": ["住宿", "酒店", "住哪"],
            "service": ["售后", "服务", "支持"]
        }

        for intent, keywords in intent_patterns.items():
            if any(kw in query_lower for kw in keywords):
                return intent

        return "general_inquiry"

    def _detect_category(self, query: str) -> str:
        """检测分类"""
        query_lower = query.lower()

        category_keywords = {
            "price": ["价格", "费用", "门票", "多少钱"],
            "attractions": ["景点", "好玩", "去哪", "推荐", "打卡"],
            "itinerary": ["线路", "行程", "攻略", "几天", "规划"],
            "food": ["美食", "吃什么", "火锅", "小面", "小吃"],
            "accommodation": ["住宿", "酒店", "住哪", "民宿"],
            "transport": ["交通", "怎么去", "地铁", "高铁"],
            "tips": ["注意", "建议", "避坑", "最佳"],
            "service": ["咨询", "预订", "定制"]
        }

        for category, keywords in category_keywords.items():
            if any(kw in query_lower for kw in keywords):
                return category

        return "other"

    def get_pending_questions(self) -> List[UnknownQuestion]:
        """获取待处理的未知问题"""
        return [q for q in self.unknown_questions if q.stage == LearningStage.DETECT]

    def mark_as_learning(self, question: str):
        """标记问题正在学习中"""
        for q in self.unknown_questions:
            if q.question == question:
                q.stage = LearningStage.EXTRACT
                q.attempts += 1
                break


class KnowledgeExtractor:
    """
    知识抽取器

    从对话历史中抽取知识
    """

    def __init__(self):
        self.extracted_knowledge: List[ExtractedKnowledge] = []
        self._max_extracted = 200

    def extract_from_conversation(
        self,
        conversation_history: List[Dict],
        current_query: str
    ) -> List[ExtractedKnowledge]:
        """
        从对话中抽取知识

        分析对话历史，提取问答对
        """
        knowledge_list = []

        if not conversation_history:
            return knowledge_list

        messages = conversation_history[-6:]

        qa_pairs = self._find_qa_pairs(messages)

        for question, answer in qa_pairs:
            extracted = ExtractedKnowledge(
                question=question,
                answer=answer,
                source="conversation",
                confidence=0.7,
                keywords=self._extract_keywords(question),
                category=self._detect_category(question),
                tags=self._extract_tags(question, answer),
                related_items=[],
                id=str(uuid.uuid4()),
                created_at=datetime.now(),
                suggested_intent=""
            )
            knowledge_list.append(extracted)
            self.extracted_knowledge.append(extracted)
            if len(self.extracted_knowledge) > self._max_extracted:
                self.extracted_knowledge = self.extracted_knowledge[-self._max_extracted:]

        return knowledge_list

    def _find_qa_pairs(self, messages: List[Dict]) -> List[Tuple[str, str]]:
        """查找问答对"""
        qa_pairs = []

        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            direction = msg.get("direction", "")

            if direction == "inbound" and self._is_question(content):
                answer = self._find_best_answer(messages, i)
                if answer:
                    qa_pairs.append((content, answer))

        return qa_pairs

    def _is_question(self, content: str) -> bool:
        """判断是否为问题"""
        question_patterns = ["怎么", "如何", "什么", "为什么", "哪里", "多少", "是不是", "能不能", "有没有", "可以吗"]
        return any(pattern in content for pattern in question_patterns)

    def _find_best_answer(self, messages: List[Dict], question_index: int) -> Optional[str]:
        """找到最佳答案"""
        if question_index + 1 < len(messages):
            next_msg = messages[question_index + 1]
            if next_msg.get("direction") == "outbound":
                answer = next_msg.get("content", "")
                if len(answer) > 5 and not self._is_question(answer):
                    return answer

        for j in range(question_index + 1, len(messages)):
            msg = messages[j]
            if msg.get("direction") == "outbound":
                answer = msg.get("content", "")
                if len(answer) > 5:
                    return answer

        return None

    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词"""
        keywords = []

        important_patterns = [
            r"价格|费用|套餐",
            r"功能|产品|系统",
            r"合作|代理|加盟",
            r"售后|服务|支持",
            r"使用|操作|设置"
        ]

        text_lower = text.lower()
        for pattern in important_patterns:
            match = re.search(pattern, text_lower)
            if match:
                keywords.append(match.group())

        return list(set(keywords))[:5]

    def _detect_category(self, text: str) -> str:
        """检测分类"""
        return "faq"

    def _extract_tags(self, question: str, answer: str) -> List[str]:
        """提取标签"""
        tags = []

        combined = question + " " + answer

        if any(kw in combined for kw in ["价格", "费用", "套餐"]):
            tags.append("价格")
        if any(kw in combined for kw in ["功能", "产品", "系统"]):
            tags.append("产品")
        if any(kw in combined for kw in ["合作", "代理"]):
            tags.append("合作")
        if any(kw in combined for kw in ["售后", "服务"]):
            tags.append("服务")

        if not tags:
            tags.append("常见问题")

        return list(set(tags))[:3]

    def get_pending_extraction(self) -> List[ExtractedKnowledge]:
        """获取待验证的抽取知识"""
        return [k for k in self.extracted_knowledge if k.validation_status == "pending"]


class ConversationLearner:
    """
    对话学习器

    分析对话模式，自动学习常见的问答模式
    """

    def __init__(self):
        self.pattern_cache: Dict[str, List[str]] = {}

    def learn_from_successful_reply(
        self,
        query: str,
        reply: str,
        matched_knowledge: KnowledgeItem = None
    ):
        """
        从成功的回复中学习。

        在学习前执行注入检测，拦截可疑的恶意输入，
        防止知识库被攻击者污染。
        """
        # 注入防护：检测可疑输入
        if _is_potential_injection(query) or _is_potential_injection(reply):
            logger.warning(
                f"[self_learning] 拦截可疑注入: "
                f"q={str(query)[:50]}... a={str(reply)[:50]}..."
            )
            return

        if matched_knowledge:
            existing_keywords = getattr(matched_knowledge, 'keywords', []) or []
            query_variants = self._generate_variants(query, existing_keywords)
            
            if hasattr(matched_knowledge, 'keywords') and matched_knowledge.keywords is not None:
                for variant in query_variants:
                    if variant not in matched_knowledge.keywords:
                        matched_knowledge.keywords.append(variant)
            elif hasattr(matched_knowledge, 'keywords'):
                matched_knowledge.keywords = query_variants

            logger.info(f"学习了 {len(query_variants)} 个新的查询变体")

    def _generate_variants(self, query: str, existing_keywords: List[str]) -> List[str]:
        """生成查询变体"""
        variants = []

        synonyms = {
            "价格": ["多少钱", "费用", "收费", "报价", "票价", "门票", "花销", "开销"],
            "功能": ["有什么用", "能做什么", "能力", "特色", "特点"],
            "购买": ["买", "下单", "获取", "预订", "预约"],
            "景点": ["景区", "旅游地", "打卡地", "名胜", "游览地", "观光地"],
            "美食": ["吃的", "小吃", "餐饮", "餐厅", "饭馆", "特色菜", "推荐菜"],
            "住宿": ["酒店", "民宿", "住的地方", "宾馆", "旅馆", "客栈"],
            "交通": ["怎么去", "出行", "路线", "坐车", "地铁", "公交", "打车"],
            "行程": ["路线", "安排", "计划", "攻略", "怎么玩", "游玩顺序"],
            "推荐": ["建议", "有什么好", "必去", "值得", "热门", "网红"],
            "重庆": ["山城", "雾都", "8D城市"],
            "火锅": ["老火锅", "麻辣火锅", "重庆火锅", "锅底"],
            "夜景": ["晚上", "夜游", "灯光", "夜色", "看夜景"],
            "免费": ["不收费", "不要钱", "0元", "免门票"],
            "营业时间": ["开放时间", "几点开门", "几点关门", "几点到几点"],
            "停车": ["停车场", "车位", "停车费", "自驾"],
            "亲子": ["带孩子", "小孩", "家庭游", "小朋友", "儿童"],
            "拍照": ["打卡", "拍照点", "拍照圣地", "出片"],
            "特产": ["伴手礼", "纪念品", "带什么回去", "当地特色"],
            "天气": ["气候", "什么时候去", "最佳季节", "几月去"],
            "避坑": ["注意什么", "防坑", "别踩雷", "避雷", "注意事项"]
        }

        for keyword, syns in synonyms.items():
            if keyword in query:
                for syn in syns:
                    variant = query.replace(keyword, syn)
                    if variant != query:
                        variants.append(variant)

        return variants[:5]

    def analyze_conversation_pattern(
        self,
        conversation_history: List[Dict]
    ) -> Dict[str, Any]:
        """分析对话模式"""
        if not conversation_history:
            return {"pattern": "unknown", "suggestions": []}

        inbound_count = sum(1 for m in conversation_history if m.get("direction") == "inbound")
        outbound_count = sum(1 for m in conversation_history if m.get("direction") == "outbound")

        pattern = "balanced"
        if inbound_count > outbound_count * 2:
            pattern = "inquiry_heavy"
        elif outbound_count > inbound_count:
            pattern = "response_heavy"

        return {
            "pattern": pattern,
            "inbound_count": inbound_count,
            "outbound_count": outbound_count,
            "suggestions": self._generate_pattern_suggestions(pattern)
        }

    def _generate_pattern_suggestions(self, pattern: str) -> List[str]:
        """生成模式建议"""
        suggestions = {
            "inquiry_heavy": ["客户可能有多个问题", "建议主动询问其他需求"],
            "response_heavy": ["回复内容可能过长", "建议精简回复"],
            "balanced": ["对话节奏良好"]
        }
        return suggestions.get(pattern, [])


class KnowledgeValidator:
    """
    知识验证器

    验证抽取的知识是否准确
    """

    def __init__(self, auto_approve_threshold: float = 0.9, knowledge_base=None):
        self.auto_approve_threshold = auto_approve_threshold
        self.knowledge_base = knowledge_base
        self.pending_validation: List[ExtractedKnowledge] = []
        self.approved_knowledge: List[ExtractedKnowledge] = []
        self.rejected_knowledge: List[ExtractedKnowledge] = []
        self._max_approved = 200
        self._max_rejected = 200

    def validate(self, knowledge: ExtractedKnowledge) -> Tuple[bool, str]:
        """
        验证知识

        Returns:
            Tuple[是否通过, 原因]
        """
        issues = []

        if len(knowledge.answer) < 10:
            issues.append("答案过短")

        if len(knowledge.question) < 5:
            issues.append("问题过短")

        if not knowledge.answer or not knowledge.question:
            issues.append("问答不能为空")

        if self._has_conflicting_info(knowledge):
            issues.append("与其他知识冲突")

        if issues:
            return False, "; ".join(issues)

        if knowledge.confidence >= self.auto_approve_threshold:
            knowledge.validation_status = "approved"
            knowledge.validated_by = "auto"
            knowledge.validated_at = datetime.now()
            self.approved_knowledge.append(knowledge)
            if len(self.approved_knowledge) > self._max_approved:
                self.approved_knowledge = self.approved_knowledge[-self._max_approved:]
            return True, "自动审核通过"

        knowledge.validation_status = "pending"
        self.pending_validation.append(knowledge)
        return True, "待人工审核"

    def _has_conflicting_info(self, knowledge: ExtractedKnowledge) -> bool:
        """检查是否有冲突信息 - 与已有知识进行相似度和矛盾检查"""
        try:
            if not self.knowledge_base:
                return False
            metadata = dict(getattr(knowledge, "metadata", {}) or {})
            enterprise_id = str(metadata.get("enterprise_id") or "").strip()
            if not enterprise_id:
                logger.warning("跳过无租户上下文的学习冲突检测")
                return False
            try:
                existing_items = self.knowledge_base.search(
                    knowledge.question,
                    top_k=5,
                    enterprise_id=enterprise_id,
                )
            except TypeError:
                existing_items = self.knowledge_base.search(knowledge.question, top_k=5)
            if not existing_items:
                return False
            
            for item_tuple in existing_items:
                if isinstance(item_tuple, tuple) and len(item_tuple) >= 2:
                    kb_item, _score = item_tuple[0], item_tuple[1]
                    question_text = kb_item.question if hasattr(kb_item, 'question') else str(kb_item)
                    answer_text = kb_item.answer if hasattr(kb_item, 'answer') else ''
                else:
                    kb_item = item_tuple
                    question_text = kb_item.get('question', '') if isinstance(kb_item, dict) else str(kb_item)
                    answer_text = kb_item.get('answer', '') if isinstance(kb_item, dict) else ''
                question_sim = self._calculate_similarity(knowledge.question, question_text)
                if question_sim > 0.85:
                    answer_sim = self._calculate_similarity(knowledge.answer, answer_text)
                    if answer_sim < 0.3 and question_sim > 0.9:
                        logger.warning(f"检测到冲突知识: 新问题与已有问题高度相似但答案差异大")
                        return True
            
            if self._has_number_conflict(knowledge.question, knowledge.answer, existing_items):
                return True
                
            return False
        except Exception as e:
            logger.warning(f"冲突检测失败: {e}")
            return False
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算文本相似度（基于字符重叠）"""
        if not text1 or not text2:
            return 0.0
        set1 = set(text1)
        set2 = set(text2)
        intersection = set1 & set2
        union = set1 | set2
        return len(intersection) / len(union) if union else 0.0
    
    def _has_number_conflict(self, question: str, answer: str, existing_items: list) -> bool:
        """检查数字矛盾（如价格冲突）"""
        import re
        new_numbers = set(re.findall(r'\d+\.?\d*', answer))
        if not new_numbers:
            return False
        
        for item_tuple in existing_items:
            if isinstance(item_tuple, tuple) and len(item_tuple) >= 2:
                kb_item, _score = item_tuple[0], item_tuple[1]
                question_text = kb_item.question if hasattr(kb_item, 'question') else str(kb_item)
                answer_text = kb_item.answer if hasattr(kb_item, 'answer') else ''
            else:
                kb_item = item_tuple
                question_text = kb_item.get('question', '') if isinstance(kb_item, dict) else str(kb_item)
                answer_text = kb_item.get('answer', '') if isinstance(kb_item, dict) else ''
            if self._calculate_similarity(question, question_text) > 0.8:
                existing_numbers = set(re.findall(r'\d+\.?\d*', answer_text))
                common_numbers = new_numbers & existing_numbers
                if existing_numbers and not common_numbers and len(new_numbers) > 0 and len(existing_numbers) > 0:
                    return True
        return False

    def approve(self, knowledge: ExtractedKnowledge, approver: str = "human"):
        """批准知识"""
        knowledge.validation_status = "approved"
        knowledge.validated_by = approver
        knowledge.validated_at = datetime.now()
        if knowledge not in self.approved_knowledge:
            self.approved_knowledge.append(knowledge)
        if knowledge in self.pending_validation:
            self.pending_validation.remove(knowledge)
        logger.info(f"知识已批准: {knowledge.question[:30]}...")

    def reject(self, knowledge: ExtractedKnowledge, reason: str = ""):
        """拒绝知识"""
        knowledge.validation_status = "rejected"
        if knowledge not in self.rejected_knowledge:
            self.rejected_knowledge.append(knowledge)
            if len(self.rejected_knowledge) > self._max_rejected:
                self.rejected_knowledge = self.rejected_knowledge[-self._max_rejected:]
        if knowledge in self.pending_validation:
            self.pending_validation.remove(knowledge)
        logger.info(f"知识已拒绝: {knowledge.question[:30]}... 原因: {reason}")

    def get_pending_validation(self) -> List[ExtractedKnowledge]:
        """获取待验证知识"""
        return self.pending_validation.copy()

    def get_approved_knowledge(self) -> List[ExtractedKnowledge]:
        """获取已批准知识"""
        return self.approved_knowledge.copy()


class AutoEnricher:
    """
    自动丰富器

    丰富现有知识库内容
    """

    def __init__(self, knowledge_base: KnowledgeBaseManager):
        self.knowledge_base = knowledge_base

    def enrich_knowledge(
        self,
        knowledge: ExtractedKnowledge
    ) -> List[Dict[str, Any]]:
        """
        丰富知识

        Returns:
            List[更新内容]
        """
        updates = []

        if len(knowledge.keywords) > 0:
            updates.append({
                "type": "keywords",
                "data": knowledge.keywords
            })

        if len(knowledge.tags) > 0:
            updates.append({
                "type": "tags",
                "data": knowledge.tags
            })

        if knowledge.related_items:
            updates.append({
                "type": "related",
                "data": knowledge.related_items
            })

        return updates

    def suggest_related_questions(
        self,
        question: str,
        existing_items: List[KnowledgeItem]
    ) -> List[str]:
        """建议相关问题"""
        suggestions = []

        question_keywords = set()
        for item in existing_items:
            question_keywords.update(item.keywords)

        for item in existing_items[:5]:
            if any(kw in item.keywords for kw in question_keywords):
                suggestions.append(f"关于{item.question}的相关问题")

        return suggestions[:3]


class SelfLearningRAGSystem:
    """
    自动学习RAG系统

    完整流程：
    1. UnknownQuestionDetector - 检测未知问题
    2. KnowledgeExtractor - 抽取知识
    3. ConversationLearner - 学习对话模式
    4. KnowledgeValidator - 验证知识
    5. AutoEnricher - 丰富知识库

    增强功能：
    6. LLMKnowledgeExtractor - LLM增强知识抽取
    7. KnowledgeQualityScorer - 知识质量评分
    8. KnowledgeDecayManager - 知识衰减管理
    9. ActiveLearner - 主动学习机制
    """

    def __init__(self, knowledge_base: KnowledgeBaseManager):
        self.knowledge_base = knowledge_base

        # 基础组件
        self.detector = UnknownQuestionDetector(confidence_threshold=0.5)
        self.extractor = KnowledgeExtractor()
        self.learner = ConversationLearner()
        self.validator = KnowledgeValidator(auto_approve_threshold=0.85, knowledge_base=self.knowledge_base)
        self.enricher = AutoEnricher(knowledge_base)
        # `pending_validation` 统一以 validator 中的列表为唯一状态源，避免统计和列表漂移。
        self.pending_validation = self.validator.pending_validation

        # 增强组件（延迟加载）
        self._llm_extractor = None
        self._quality_scorer = None
        self._decay_manager = None
        self._active_learner = None

        self.learning_enabled = True
        self.auto_approve_enabled = True
        self.enhanced_learning_enabled = True  # 增强学习开关

        self.total_learned = 0
        self.approved_count = 0
        self.rejected_count = 0
        self.auto_approved_count = 0
        self.unknown_detected_count = 0
        self.accuracy_improvement = 0.0
        self._learning_history: List[Dict] = []
        self._learning_lock = threading.Lock()
        self._last_save_time = 0.0
        self._save_interval = 30.0
        self._save_pending = False

        self._learning_stats = {
            "total_learned": 0,
            "auto_approved": 0,
            "human_approved": 0,
            "rejected": 0,
            "quality_improved": 0,
            "decayed": 0
        }
        
        self._vector_store = None
        self._embedding_service = None
        
        # 持久化存储
        self._data_dir = Path("data")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self._data_dir / "learning_state.json"
        self._load_state()

    @staticmethod
    def _read_tenant_field(payload: Any, *field_names: str) -> str:
        if payload is None:
            return ""
        if isinstance(payload, dict):
            for field_name in field_names:
                value = payload.get(field_name)
                if str(value or "").strip():
                    return str(value).strip()
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                return SelfLearningRAGSystem._read_tenant_field(metadata, *field_names)
            return ""
        for field_name in field_names:
            value = getattr(payload, field_name, "")
            if str(value or "").strip():
                return str(value).strip()
        metadata = getattr(payload, "metadata", None)
        if isinstance(metadata, dict):
            return SelfLearningRAGSystem._read_tenant_field(metadata, *field_names)
        return ""

    def _resolve_learning_tenant_context(
        self,
        *,
        rag_result: Any,
        conversation_history: Optional[List[Dict]],
        matched_knowledge: Optional[KnowledgeItem],
        enterprise_id: str = "",
        schema_id: str = "",
        retrieved_contexts: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, str]:
        resolved_enterprise_id = str(enterprise_id or "").strip()
        resolved_schema_id = str(schema_id or "").strip()

        candidates: List[Any] = []
        if matched_knowledge is not None:
            candidates.append(matched_knowledge)
        candidates.extend(list(retrieved_contexts or []))
        candidates.extend(list(getattr(rag_result, "sources", []) or []))
        if conversation_history:
            candidates.extend(reversed(conversation_history[-3:]))

        for candidate in candidates:
            if not resolved_enterprise_id:
                resolved_enterprise_id = self._read_tenant_field(
                    candidate,
                    "enterprise_id",
                    "enterpriseId",
                )
            if not resolved_schema_id:
                resolved_schema_id = self._read_tenant_field(
                    candidate,
                    "schema_id",
                    "schemaId",
                    "preferred_schema_id",
                    "preferredSchemaId",
                )
            if resolved_enterprise_id and resolved_schema_id:
                break

        return {
            "enterprise_id": resolved_enterprise_id,
            "schema_id": resolved_schema_id,
        }

    @staticmethod
    def _annotate_extracted_knowledge_tenant(
        knowledge: ExtractedKnowledge,
        tenant_context: Dict[str, str],
    ) -> None:
        metadata = dict(getattr(knowledge, "metadata", {}) or {})
        enterprise_id = str(tenant_context.get("enterprise_id") or "").strip()
        schema_id = str(tenant_context.get("schema_id") or "").strip()
        if enterprise_id:
            metadata["enterprise_id"] = enterprise_id
        if schema_id:
            metadata["schema_id"] = schema_id
        knowledge.metadata = metadata

    _LAZY_LOAD_FAILED = object()

    @property
    def llm_extractor(self):
        """延迟加载LLM知识抽取器"""
        if self._llm_extractor is None:
            try:
                from .llm_knowledge_extractor import get_llm_extractor
                self._llm_extractor = get_llm_extractor()
            except Exception as e:
                logger.warning(f"加载LLM知识抽取器失败: {e}")
                self._llm_extractor = self._LAZY_LOAD_FAILED
        return None if self._llm_extractor is self._LAZY_LOAD_FAILED else self._llm_extractor

    @property
    def quality_scorer(self):
        """延迟加载知识质量评分器"""
        if self._quality_scorer is None:
            try:
                from .knowledge_quality_scorer import get_quality_scorer
                self._quality_scorer = get_quality_scorer(self.knowledge_base)
            except Exception as e:
                logger.warning(f"加载知识质量评分器失败: {e}")
                self._quality_scorer = self._LAZY_LOAD_FAILED
        return None if self._quality_scorer is self._LAZY_LOAD_FAILED else self._quality_scorer

    @property
    def decay_manager(self):
        """延迟加载知识衰减管理器"""
        if self._decay_manager is None:
            try:
                from .knowledge_decay_manager import get_decay_manager
                self._decay_manager = get_decay_manager(knowledge_base=self.knowledge_base)
            except Exception as e:
                logger.warning(f"加载知识衰减管理器失败: {e}")
                self._decay_manager = self._LAZY_LOAD_FAILED
        return None if self._decay_manager is self._LAZY_LOAD_FAILED else self._decay_manager

    @property
    def active_learner(self):
        """延迟加载主动学习器"""
        if self._active_learner is None:
            try:
                from .active_learner import get_active_learner
                self._active_learner = get_active_learner(
                    knowledge_base=self.knowledge_base,
                    rag_service=self
                )
            except Exception as e:
                logger.warning(f"加载主动学习器失败: {e}")
                self._active_learner = self._LAZY_LOAD_FAILED
        return None if self._active_learner is self._LAZY_LOAD_FAILED else self._active_learner

    def learn_from_conversation(
        self,
        query: str,
        rag_result,
        conversation_history: List[Dict] = None,
        matched_knowledge: KnowledgeItem = None,
        enterprise_id: str = "",
        schema_id: str = "",
        retrieved_contexts: List[Dict[str, Any]] = None,
    ) -> LearningResult:
        """
        从对话中学习

        Args:
            query: 用户问题
            rag_result: RAG结果
            conversation_history: 对话历史
            matched_knowledge: 匹配的知识项

        Returns:
            LearningResult: 学习结果
        """
        # 注入防护：阻断含联系方式/违规内容/越权指令的输入进入学习流程
        if _is_potential_injection(query) or _is_potential_injection(
            getattr(rag_result, "answer", "")
        ):
            logger.warning(
                f"[self_learning] 拦截可疑注入: "
                f"q={str(query)[:50]}... a={str(getattr(rag_result, 'answer', ''))[:50]}..."
            )
            return LearningResult(
                new_knowledge_added=0,
                knowledge_updated=0,
                questions_learned=0,
                accuracy_improvement=0.0,
                details=["blocked_potential_injection"],
            )

        with self._learning_lock:
            return self._learn_from_conversation_internal(
                query,
                rag_result,
                conversation_history,
                matched_knowledge,
                enterprise_id=enterprise_id,
                schema_id=schema_id,
                retrieved_contexts=retrieved_contexts,
            )
    
    def _learn_from_conversation_internal(
        self,
        query: str,
        rag_result,
        conversation_history: List[Dict] = None,
        matched_knowledge: KnowledgeItem = None,
        enterprise_id: str = "",
        schema_id: str = "",
        retrieved_contexts: List[Dict[str, Any]] = None,
    ) -> LearningResult:
        """从对话中学习（内部实现，已加锁）"""
        if not self.learning_enabled:
            return LearningResult(0, 0, 0, 0.0, ["学习功能已禁用"])

        details = []
        new_knowledge = 0
        updated_knowledge = 0
        tenant_context = self._resolve_learning_tenant_context(
            rag_result=rag_result,
            conversation_history=conversation_history,
            matched_knowledge=matched_knowledge,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
            retrieved_contexts=retrieved_contexts,
        )

        if matched_knowledge:
            self.learner.learn_from_successful_reply(query, rag_result.answer, matched_knowledge)
            details.append("已从成功回复中学习查询变体")

        is_unknown, unknown_question = self.detector.detect(query, rag_result, conversation_history)

        if is_unknown and unknown_question:
            self.detector.mark_as_learning(query)
            details.append(f"检测到未知问题: {query[:30]}...")

            extracted_list = self.extractor.extract_from_conversation(
                conversation_history or [], query
            )

            for extracted in extracted_list:
                self._annotate_extracted_knowledge_tenant(extracted, tenant_context)
                approved, reason = self.validator.validate(extracted)
                if _looks_like_contact_pollution(getattr(extracted, "answer", "")):
                    details.append(f"已跳过留资污染知识: {str(getattr(extracted, 'question', '') or '')[:30]}...")
                    continue

                if approved and extracted.validation_status == "approved":
                    if self.auto_approve_enabled:
                        success = self._add_knowledge_to_db(extracted)
                        if success:
                            new_knowledge += 1
                            self._learning_stats["auto_approved"] += 1
                            self.auto_approved_count += 1
                            self.unknown_detected_count += 1
                            self.total_learned += 1
                            self._add_to_history("auto_approved", extracted)
                            details.append(f"自动添加新知识: {extracted.question[:30]}...")
                    else:
                        self._learning_stats["human_approved"] += 1
                        self.pending_validation.append(extracted)
                        details.append(f"待审核知识: {extracted.question[:30]}...")

        self._learning_stats["total_learned"] += new_knowledge

        self._save_state()

        accuracy_improvement = (self._learning_stats["auto_approved"] /
                              max(self._learning_stats["total_learned"], 1)) * 100

        return LearningResult(
            new_knowledge_added=new_knowledge,
            knowledge_updated=updated_knowledge,
            questions_learned=len(extracted_list) if is_unknown else 0,
            accuracy_improvement=accuracy_improvement,
            details=details
        )

    def _add_knowledge_to_db(self, knowledge: ExtractedKnowledge) -> bool:
        """添加知识到数据库和向量存储"""
        try:
            if _looks_like_contact_pollution(getattr(knowledge, "answer", "")):
                logger.info(f"跳过留资污染知识入库: {str(getattr(knowledge, 'question', '') or '')[:40]}")
                return False
            metadata = dict(getattr(knowledge, "metadata", {}) or {})
            enterprise_id = str(
                metadata.get("enterprise_id")
                or getattr(knowledge, "enterprise_id", "")
                or ""
            ).strip()
            if not enterprise_id:
                logger.warning(
                    f"跳过无租户上下文的自动学习知识入库: {str(getattr(knowledge, 'question', '') or '')[:40]}"
                )
                return False
            schema_id = str(
                metadata.get("schema_id")
                or metadata.get("preferred_schema_id")
                or getattr(knowledge, "schema_id", "")
                or ""
            ).strip()
            import uuid
            item = KnowledgeItem(
                id=str(uuid.uuid4())[:8],
                question=knowledge.question,
                answer=knowledge.answer,
                category=knowledge.category,
                keywords=knowledge.keywords if isinstance(knowledge.keywords, list) else [],
                tags=knowledge.tags if isinstance(knowledge.tags, list) else [],
                reply_templates=[],
                priority=1,
                source=str(getattr(knowledge, "source", "") or "conversation"),
                enterprise_id=enterprise_id,
                schema_id=schema_id,
                metadata=metadata,
            )

            success = self.knowledge_base.add_knowledge(item)

            return success

        except Exception as e:
            logger.error(f"添加知识失败: {e}")
            return False

    def enable_learning(self):
        """启用学习"""
        self.learning_enabled = True
        logger.info("自动学习功能已启用")

    def disable_learning(self):
        """禁用学习"""
        self.learning_enabled = False
        logger.info("自动学习功能已禁用")

    def get_pending_validation(self, top_k: int = 20) -> List[ExtractedKnowledge]:
        """获取待验证的知识列表"""
        return self.pending_validation[:top_k]

    def clear_pending_validation(self) -> int:
        """清空待审核列表，统一操作唯一状态源。"""
        count = len(self.pending_validation)
        self.pending_validation.clear()
        return count

    def get_recent_learning(self, limit: int = 5) -> List[Dict]:
        """获取最近学习记录"""
        return self._learning_history[-limit:][::-1]

    def get_learning_history(self, limit: int = 50) -> List[Dict]:
        """获取学习历史"""
        return self._learning_history[-limit:][::-1]

    def _add_to_history(self, action_type: str, knowledge: ExtractedKnowledge):
        """添加学习历史"""
        self._learning_history.append({
            "action": action_type,
            "question": knowledge.question,
            "answer": knowledge.answer,
            "category": knowledge.category,
            "confidence": knowledge.confidence,
            "timestamp": datetime.now().isoformat()
        })
        if len(self._learning_history) > 200:
            self._learning_history = self._learning_history[-200:]

    def get_learning_stats(self) -> Dict[str, Any]:
        """获取学习统计"""
        return {
            **self._learning_stats,
            "pending_questions": len(self.detector.get_pending_questions()),
            "pending_validation": len(self.pending_validation),
            "total_knowledge": len(self._list_knowledge_items()),
            "learning_enabled": self.learning_enabled
        }

    def _list_knowledge_items(self) -> List[Any]:
        if not self.knowledge_base:
            return []
        if hasattr(self.knowledge_base, "list_knowledge_items"):
            return list(self.knowledge_base.list_knowledge_items() or [])
        if hasattr(self.knowledge_base, "get_knowledge_list"):
            return list(self.knowledge_base.get_knowledge_list() or [])
        if hasattr(self.knowledge_base, "get_all_items"):
            return list(self.knowledge_base.get_all_items() or [])
        return list(getattr(self.knowledge_base, "knowledge_items", []) or [])

    def _load_state(self):
        """从文件加载持久化状态"""
        try:
            if self._state_file.exists():
                with open(self._state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                
                self._learning_stats = state.get('learning_stats', self._learning_stats)
                self.total_learned = state.get('total_learned', 0)
                self.approved_count = state.get('approved_count', 0)
                self.rejected_count = state.get('rejected_count', 0)
                self.auto_approved_count = state.get('auto_approved_count', 0)
                self.unknown_detected_count = state.get('unknown_detected_count', 0)
                self.learning_enabled = state.get('learning_enabled', True)
                self.auto_approve_enabled = state.get('auto_approve_enabled', True)
                
                self.pending_validation.clear()
                saved_pending = state.get('pending_validation', [])
                for item_data in saved_pending:
                    try:
                        knowledge = ExtractedKnowledge(
                            question=item_data.get('question', ''),
                            answer=item_data.get('answer', ''),
                            category=item_data.get('category', 'other'),
                            keywords=item_data.get('keywords', []),
                            tags=item_data.get('tags', []),
                            confidence=item_data.get('confidence', 0.7),
                            source=item_data.get('source', 'unknown'),
                            id=item_data.get('id', str(uuid.uuid4())),
                            metadata=item_data.get('metadata', {}),
                        )
                        self.pending_validation.append(knowledge)
                    except Exception as e:
                        logger.warning(f"加载待审核知识失败: {e}")
                
                self._learning_history = state.get('learning_history', [])[-100:]
                
                logger.info(f"学习状态已加载: {len(self.pending_validation)}条待审核, {self.total_learned}条已学习")
        except Exception as e:
            logger.warning(f"加载学习状态失败: {e}")

    def _save_state(self, force: bool = False):
        """保存状态到文件（带节流，默认最少间隔30秒）"""
        now = time.time()
        if not force and (now - self._last_save_time < self._save_interval):
            self._save_pending = True
            return
        self._last_save_time = now
        self._save_pending = False
        try:
            pending_data = []
            for k in self.pending_validation:
                pending_data.append({
                    'id': k.id,
                    'question': k.question,
                    'answer': k.answer,
                    'category': k.category,
                    'keywords': k.keywords,
                    'tags': k.tags,
                    'confidence': k.confidence,
                    'source': k.source,
                    'metadata': dict(getattr(k, 'metadata', {}) or {}),
                })
            
            state = {
                'learning_stats': self._learning_stats,
                'total_learned': self.total_learned,
                'approved_count': self.approved_count,
                'rejected_count': self.rejected_count,
                'auto_approved_count': self.auto_approved_count,
                'unknown_detected_count': self.unknown_detected_count,
                'learning_enabled': self.learning_enabled,
                'auto_approve_enabled': self.auto_approve_enabled,
                'pending_validation': pending_data,
                'learning_history': self._learning_history[-100:],
                'saved_at': datetime.now().isoformat()
            }

            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w',
                    encoding='utf-8',
                    dir=str(self._state_file.parent),
                    prefix='learning_state_',
                    suffix='.tmp',
                    delete=False,
                ) as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                    tmp_path = Path(f.name)
                os.replace(tmp_path, self._state_file)
            finally:
                if tmp_path and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
        except Exception as e:
            logger.warning(f"保存学习状态失败: {e}")

    def get_pending_validation_questions(self) -> List[Dict]:
        """获取待审核问题"""
        pending = self.pending_validation
        return [
            {
                "question": k.question,
                "answer": k.answer,
                "category": k.category,
                "keywords": k.keywords,
                "confidence": k.confidence
            }
            for k in pending
        ]

    def approve_knowledge(self, item_id: str, approved_answer: str = None, category: str = None) -> bool:
        """批准知识"""
        for knowledge in self.pending_validation:
            if knowledge.id == item_id:
                original_answer = knowledge.answer
                original_category = knowledge.category
                if approved_answer:
                    knowledge.answer = approved_answer
                if category:
                    knowledge.category = category
                success = self._add_knowledge_to_db(knowledge)
                if not success:
                    knowledge.answer = original_answer
                    knowledge.category = original_category
                    return False
                self.validator.approve(knowledge, "human")
                self.approved_count += 1
                self.total_learned += 1
                self._add_to_history("approved", knowledge)
                self._save_state(force=True)
                return True
        return False

    def reject_knowledge(self, item_id: str, reason: str = "") -> bool:
        """拒绝知识"""
        for knowledge in self.pending_validation:
            if knowledge.id == item_id:
                self.validator.reject(knowledge, reason)
                self.rejected_count += 1
                self._add_to_history("rejected", knowledge)
                self._save_state(force=True)
                return True
        return False

    # ==================== 增强学习方法 ====================

    async def learn_from_conversation_enhanced(
        self,
        query: str,
        rag_result,
        conversation_history: List[Dict] = None,
        matched_knowledge: KnowledgeItem = None,
        enterprise_id: str = "",
        schema_id: str = "",
        retrieved_contexts: List[Dict[str, Any]] = None,
    ) -> LearningResult:
        """
        增强的对话学习（使用LLM增强抽取）

        Args:
            query: 用户问题
            rag_result: RAG结果
            conversation_history: 对话历史
            matched_knowledge: 匹配的知识项

        Returns:
            LearningResult: 学习结果
        """
        # 注入防护：与同步入口保持一致，阻断 LLM 抽取路径上的可疑输入
        if _is_potential_injection(query) or _is_potential_injection(
            getattr(rag_result, "answer", "")
        ):
            logger.warning(
                f"[self_learning] 拦截可疑注入(enhanced): "
                f"q={str(query)[:50]}..."
            )
            return LearningResult(
                new_knowledge_added=0,
                knowledge_updated=0,
                questions_learned=0,
                accuracy_improvement=0.0,
                details=["blocked_potential_injection"],
            )

        if not self.learning_enabled or not self.enhanced_learning_enabled:
            return self.learn_from_conversation(
                query,
                rag_result,
                conversation_history,
                matched_knowledge,
                enterprise_id=enterprise_id,
                schema_id=schema_id,
                retrieved_contexts=retrieved_contexts,
            )

        details = []
        new_knowledge = 0
        updated_knowledge = 0
        tenant_context = self._resolve_learning_tenant_context(
            rag_result=rag_result,
            conversation_history=conversation_history,
            matched_knowledge=matched_knowledge,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
            retrieved_contexts=retrieved_contexts,
        )

        # 使用主动学习器分析查询
        if self.active_learner:
            confidence = getattr(rag_result, 'confidence', 0.5)
            gap = self.active_learner.analyze_query(query, {'answer': rag_result.answer}, confidence)
            if gap:
                details.append(f"检测到知识缺口: {gap.topic}")

        # 使用LLM增强抽取
        if self.llm_extractor and conversation_history:
            try:
                extracted_list = await self.llm_extractor.extract_from_conversation(
                    conversation_history
                )
                
                for extracted in extracted_list:
                    self._annotate_extracted_knowledge_tenant(extracted, tenant_context)
                    # 使用质量评分器评估
                    if self.quality_scorer:
                        quality_score = self.quality_scorer.score(extracted)
                        extracted.quality_score = quality_score.overall_score
                        
                        # 只保留高质量知识
                        if quality_score.overall_score < 0.5:
                            details.append(f"跳过低质量知识: {extracted.question[:20]}...")
                            continue
                    
                    # 验证并添加
                    approved, reason = self.validator.validate(extracted)
                    if approved and extracted.validation_status == "approved":
                        if self.auto_approve_enabled:
                            success = self._add_knowledge_to_db(extracted)
                            if success:
                                new_knowledge += 1
                                self._learning_stats["auto_approved"] += 1
                                self._add_to_history("llm_extracted", extracted)
                                details.append(f"LLM抽取新知识: {extracted.question[:30]}...")
            except Exception as e:
                logger.warning(f"LLM增强抽取失败: {e}")
                # 回退到基础方法
                return self.learn_from_conversation(
                    query,
                    rag_result,
                    conversation_history,
                    matched_knowledge,
                    enterprise_id=enterprise_id,
                    schema_id=schema_id,
                    retrieved_contexts=retrieved_contexts,
                )

        # 记录知识使用（用于衰减管理）
        if matched_knowledge and self.decay_manager:
            kid = getattr(matched_knowledge, 'id', str(id(matched_knowledge)))
            self.decay_manager.record_usage(kid)

        self._learning_stats["total_learned"] += new_knowledge

        return LearningResult(
            new_knowledge_added=new_knowledge,
            knowledge_updated=updated_knowledge,
            questions_learned=new_knowledge,
            accuracy_improvement=0.0,
            details=details
        )

    def apply_knowledge_decay(self) -> Dict[str, Any]:
        """
        应用知识衰减

        Returns:
            衰减结果统计
        """
        if not self.decay_manager:
            return {"error": "衰减管理器未初始化"}

        # 获取所有知识ID
        knowledge_ids = [getattr(item, 'id', str(id(item))) for item in self._list_knowledge_items()]

        # 注册未注册的知识
        for kid in knowledge_ids:
            if not self.decay_manager.get_knowledge_state(kid):
                self.decay_manager.register_knowledge(kid)

        # 批量应用衰减
        results = self.decay_manager.batch_decay(knowledge_ids)
        self._learning_stats["decayed"] += results['processed']

        logger.info(f"知识衰减完成: 处理 {results['processed']} 条")
        return results

    def get_learning_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取学习任务

        Args:
            limit: 返回数量限制

        Returns:
            学习任务列表
        """
        if not self.active_learner:
            return []

        if hasattr(self.active_learner, "get_pending_tasks"):
            tasks = self.active_learner.get_pending_tasks(limit)
            return [task.to_dict() if hasattr(task, "to_dict") else task for task in tasks]

        if hasattr(self.active_learner, "get_learning_tasks"):
            tasks = self.active_learner.get_learning_tasks()
            return tasks[:limit]

        return []

    def get_knowledge_gaps(self) -> List[Dict[str, Any]]:
        """
        获取知识缺口列表

        Returns:
            知识缺口列表
        """
        if not self.active_learner:
            return []

        if hasattr(self.active_learner, "export_gaps"):
            return self.active_learner.export_gaps()

        if hasattr(self.active_learner, "detect_knowledge_gaps"):
            return self.active_learner.detect_knowledge_gaps()

        return []

    def get_quality_report(self) -> Dict[str, Any]:
        """
        获取知识质量报告

        Returns:
            质量报告
        """
        if not self.quality_scorer:
            return {"error": "质量评分器未初始化"}

        items = self._list_knowledge_items()
        stats = self.quality_scorer.get_quality_stats(items)

        return {
            "quality_stats": stats,
            "recommendations": self._generate_quality_recommendations(stats)
        }

    def _generate_quality_recommendations(self, stats: Dict) -> List[str]:
        """生成质量改进建议"""
        recommendations = []

        if stats.get('average_overall', 1.0) < 0.6:
            recommendations.append("整体知识质量偏低，建议全面审核知识库")

        if stats.get('low_quality_count', 0) > 5:
            recommendations.append(f"存在 {stats['low_quality_count']} 条低质量知识，建议清理或改进")

        if stats.get('average_completeness', 1.0) < 0.5:
            recommendations.append("知识完整性不足，建议补充问题答案详情")

        if stats.get('average_timeliness', 1.0) < 0.5:
            recommendations.append("知识时效性较低，建议更新过时内容")

        return recommendations

    def get_enhanced_stats(self) -> Dict[str, Any]:
        """
        获取增强学习统计

        Returns:
            统计信息
        """
        stats = {
            "base_stats": self.get_learning_stats(),
            "enhanced_enabled": self.enhanced_learning_enabled
        }

        if self.decay_manager:
            stats["decay_stats"] = self.decay_manager.get_statistics()

        if self.active_learner:
            if hasattr(self.active_learner, "get_statistics"):
                stats["active_learner_stats"] = self.active_learner.get_statistics()
            elif hasattr(self.active_learner, "get_stats"):
                stats["active_learner_stats"] = self.active_learner.get_stats()
            else:
                stats["active_learner_stats"] = {}

        return stats

    def generate_user_question_for_learning(self) -> Optional[str]:
        """
        生成向用户询问的问题（用于主动学习）

        Returns:
            问题文本
        """
        if not self.active_learner:
            return None

        if hasattr(self.active_learner, "generate_user_question"):
            return self.active_learner.generate_user_question()

        if hasattr(self.active_learner, "generate_user_questions"):
            questions = self.active_learner.generate_user_questions()
            if questions:
                first = questions[0]
                if isinstance(first, dict):
                    return first.get("question")
                return str(first)

        return None

    def complete_learning_task(self, task_id: str, result: Dict[str, Any] = None) -> bool:
        """
        完成学习任务

        Args:
            task_id: 任务ID
            result: 任务结果

        Returns:
            是否成功
        """
        if not self.active_learner:
            return False

        if hasattr(self.active_learner, "complete_task"):
            payload = result or {}
            if isinstance(payload, dict):
                answer = payload.get("answer") or payload.get("result") or payload.get("content") or ""
                category = payload.get("category")
                return bool(self.active_learner.complete_task(task_id, answer, category))
            return bool(self.active_learner.complete_task(task_id, str(payload), None))

        return False


from functools import lru_cache

@lru_cache(maxsize=1)
def get_self_learning_rag_system(knowledge_base: KnowledgeBaseManager = None) -> SelfLearningRAGSystem:
    if knowledge_base is None:
        from .knowledge_base_adapter import get_learning_knowledge_base_adapter
        knowledge_base = get_learning_knowledge_base_adapter()
    return SelfLearningRAGSystem(knowledge_base)
