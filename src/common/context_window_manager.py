"""
上下文窗口管理器

负责管理对话的上下文窗口，控制 token 数量和内存使用
"""

from typing import List, Dict, Any, Optional
from collections import deque
from loguru import logger


class ContextWindowManager:
    """
    上下文窗口管理器

    负责管理对话历史的上下文窗口，
    控制 token 数量，防止超出模型限制
    """

    def __init__(self, max_tokens: int = 4000, max_messages: int = 20):
        self.max_tokens = max_tokens
        self.max_messages = max_messages
        self._token_count = 0
        self._messages: deque = deque(maxlen=max_messages)

    def add_message(self, message: Dict[str, Any], session_id: str = None) -> bool:
        """
        添加消息到上下文窗口

        Args:
            message: 消息字典
            session_id: 会话ID（可选，用于兼容）
        Returns:
            是否成功添加
        """
        message_tokens = self._estimate_tokens(message)

        if self._token_count + message_tokens > self.max_tokens:
            self._trim_oldest_messages(message_tokens)

        if message_tokens > self.max_tokens:
            logger.warning(f"单条消息超过最大token限制: {message_tokens}")
            return False

        self._messages.append(message)
        self._token_count += message_tokens
        return True

    def _estimate_tokens(self, message: Dict[str, Any]) -> int:
        """估算消息的token数量"""
        content = message.get("content", "")
        if isinstance(content, str):
            return len(content) // 4 + 100
        return 50

    def _trim_oldest_messages(self, required_tokens: int):
        """删除最旧的消息直到有足够空间"""
        while self._token_count + required_tokens > self.max_tokens and len(self._messages) > 1:
            oldest = self._messages.popleft()
            self._token_count -= self._estimate_tokens(oldest)

    def get_context(self, session_id: str = None) -> List[Dict[str, Any]]:
        """获取当前上下文"""
        return list(self._messages)

    def clear(self):
        """清空上下文"""
        self._messages.clear()
        self._token_count = 0

    def get_token_count(self) -> int:
        """获取当前token数量"""
        return self._token_count

    def is_full(self) -> bool:
        """检查上下文是否已满"""
        return self._token_count >= self.max_tokens * 0.9
