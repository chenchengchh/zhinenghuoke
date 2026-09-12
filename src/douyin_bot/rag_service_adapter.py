"""
RAG服务适配器

将RPA引擎与RAG服务解耦，提供统一的接口
增强上下文理解能力，提供更精准的回复
"""

import time
import re
from typing import Dict, List, Optional, Any, Tuple
from loguru import logger
from dataclasses import dataclass
from src.common.conversation_id import build_conversation_id


@dataclass
class RAGRequest:
    """RAG请求"""
    customer_name: str
    message: str
    conversation_history: List[Dict]
    customer_data: Dict
    context_summary: str = ""      # 上下文摘要
    reply_intent: str = ""         # 回复意图
    priority_level: str = "P2"     # 优先级


@dataclass
class RAGResponse:
    """RAG响应"""
    reply: str
    intent_level: str
    priority_level: str
    need_human: bool
    risk_level: str
    sentiment: str
    confidence: float
    matched_knowledge: Optional[str]
    suggested_action: str
    context_used: bool = False      # 是否使用了上下文


class RAGServiceAdapter:
    """
    RAG服务适配器

    增强功能：
    1. 消息格式转换
    2. RAG服务调用
    3. 响应解析
    4. 错误处理
    5. 上下文增强（新增）
    """

    GREETING_PATTERNS = [
        r'你好', r'您好', r'在吗', r'hi', r'hello', r'嗨',
        r'早上好', r'下午好', r'晚上好', r'打扰'
    ]

    THANKS_PATTERNS = [
        r'谢谢', r'感谢', r'多谢', r'谢', r'非常感谢'
    ]

    PRICE_PATTERNS = [
        r'价格', r'多少钱', r'费用', r'收费', r'报价',
        r'套餐', r'优惠', r'折扣', r'活动', r'便宜', r'贵'
    ]

    PRODUCT_PATTERNS = [
        r'产品', r'功能', r'介绍', r'能做什么', r'有什么用',
        r'特点', r'特性', r'优势', r'如何', r'使用'
    ]

    def __init__(self, rpa_engine=None):
        """初始化适配器"""
        self._rpa_engine = rpa_engine
        self._rag_service = None
        self._init_rag_service()

    def _init_rag_service(self):
        """初始化RAG服务"""
        try:
            from src.common.enhanced_customer_service import get_enhanced_customer_service
            self._rag_service = get_enhanced_customer_service()
            logger.info("RAG服务初始化成功")
        except Exception as e:
            logger.warning(f"RAG服务初始化失败: {e}")

    def set_rpa_engine(self, rpa_engine):
        """设置RPA引擎实例"""
        self._rpa_engine = rpa_engine

    def _analyze_intent(self, message: str) -> Tuple[str, str]:
        """
        分析消息意图

        Returns:
            (intent_type, priority_level)
        """
        message_lower = message.lower()

        if any(re.search(p, message_lower) for p in self.GREETING_PATTERNS):
            return ("greeting", "P3")

        if any(re.search(p, message_lower) for p in self.THANKS_PATTERNS):
            return ("thanks", "P3")

        if any(re.search(p, message_lower) for p in self.PRICE_PATTERNS):
            return ("price_inquiry", "P2")

        if any(re.search(p, message_lower) for p in self.PRODUCT_PATTERNS):
            return ("product_inquiry", "P2")

        if '合作' in message or '代理' in message or '加盟' in message:
            return ("cooperation", "P1")

        if '购买' in message or '买' in message or '下单' in message:
            return ("purchase", "P1")

        if '投诉' in message or '不满' in message or '退款' in message:
            return ("complaint", "P0")

        return ("common", "P2")

    def _enhance_context(self, message: str, conversation_history: List[Dict] = None) -> str:
        """
        增强上下文信息

        Args:
            message: 当前消息
            conversation_history: 对话历史

        Returns:
            增强的上下文描述
        """
        context_parts = []

        if conversation_history and len(conversation_history) > 0:
            history_summary = self._summarize_history(conversation_history)
            if history_summary:
                context_parts.append(f"对话历史: {history_summary}")

        intent_type, priority = self._analyze_intent(message)
        context_parts.append(f"当前意图: {intent_type}")
        context_parts.append(f"消息内容: {message}")

        return " | ".join(context_parts)

    def _summarize_history(self, history: List[Dict]) -> str:
        """
        总结对话历史

        Args:
            history: 对话历史列表

        Returns:
            总结文本
        """
        if not history:
            return ""

        summaries = []
        for msg in history[-5:]:
            direction = "我" if msg.get('direction') == 'outbound' else "客户"
            content = msg.get('content', '')[:30]
            summaries.append(f"{direction}: {content}")

        return " || ".join(summaries)

    def process(self, message, conversation_history: List[Dict] = None) -> Optional[RAGResponse]:
        """
        处理消息并生成回复

        增强版本：包含意图分析、上下文增强、优先级判断
        """
        if not self._rag_service:
            logger.error("RAG服务未初始化")
            return self._default_response()

        try:
            if hasattr(message, 'customer_name'):
                customer_name = message.customer_name
                msg_content = message.content
                conversation_id = message.conversation_id
            else:
                customer_name = message.get('customer_name', 'unknown')
                msg_content = message.get('content', '')
                conversation_id = message.get('conversation_id', '')

            if not conversation_id:
                conversation_id = build_conversation_id(customer_name, "douyin")

            customer_data = {
                'sec_uid': conversation_id.replace('douyin_', '') if conversation_id.startswith('douyin_') else conversation_id,
                'nickname': customer_name,
                'platform': 'douyin',
                'status': 'active'
            }

            request = RAGRequest(
                customer_name=customer_name,
                message=msg_content,
                conversation_history=conversation_history or [],
                customer_data=customer_data
            )

            intent_type, priority_level = self._analyze_intent(msg_content)
            request.reply_intent = intent_type
            request.priority_level = priority_level

            context_summary = self._enhance_context(msg_content, conversation_history)
            request.context_summary = context_summary

            start_time = time.time()

            result = self._rag_service.process_message(
                message=request.message,
                customer_name=request.customer_name,
                conversation_history=request.conversation_history,
                customer_data=request.customer_data,
                use_enhanced=True,
                session_id=request.customer_data.get('sec_uid', request.customer_name)
            )

            if not result or not isinstance(result, dict):
                logger.warning(f"RAG服务返回无效结果: {type(result)}, 使用默认响应")
                return self._default_response()

            duration = time.time() - start_time
            logger.debug(f"RAG处理耗时: {duration:.2f}秒")

            return RAGResponse(
                reply=str(result.get('reply', '') or ''),
                intent_level=result.get('intent_level', 'E') or 'E',
                priority_level=result.get('priority_level', priority_level),
                need_human=result.get('need_human', False),
                risk_level=(result.get('risk_assessment', {}) or {}).get('overall_level', 'low'),
                sentiment=result.get('sentiment', 'neutral'),
                confidence=result.get('confidence', 0.5),
                matched_knowledge=result.get('matched_knowledge'),
                suggested_action=result.get('suggested_action', '正常跟进'),
                context_used=len(conversation_history or []) > 0
            )

        except Exception as e:
            logger.error(f"RAG处理失败: {e}")
            return self._default_response()

    def _default_response(self) -> RAGResponse:
        """默认响应"""
        return RAGResponse(
            reply='',
            intent_level='E',
            priority_level='P2',
            need_human=True,
            risk_level='low',
            sentiment='neutral',
            confidence=0.0,
            matched_knowledge=None,
            suggested_action='等待主链处理',
            context_used=False
        )

    def batch_process(self, messages: List, conversation_histories: Dict[str, List[Dict]] = None) -> Dict[str, RAGResponse]:
        """
        批量处理消息

        Args:
            messages: 消息列表
            conversation_histories: 对话历史字典 {customer_name: history}

        Returns:
            Dict[str, RAGResponse] 客户名称到响应的映射
        """
        results = {}

        for msg in messages:
            if hasattr(msg, 'customer_name'):
                customer_name = msg.customer_name
            else:
                customer_name = msg.get('customer_name', 'unknown')

            history = []
            if conversation_histories and customer_name in conversation_histories:
                history = conversation_histories[customer_name]

            response = self.process(msg, history)
            if response:
                results[customer_name] = response

        return results

    def get_rag_service(self):
        """获取RAG服务实例"""
        return self._rag_service
