"""
rpa_engine.models - RPA 引擎的数据模型与常量层

阶段D·D-1 拆分产物：
将原 rpa_engine.py 中"纯数据 + 纯配置"的部分抽出，使 core.py 专注于类实现。

本模块只放：
- 枚举（MessageDirection / OperationMode / PageState）
- 数据类（RPAMessage / OperationResult / InboundDecisionResult）
- DOM 选择器池 SELECTOR_POOL
- 标记对象 _STATE_UNSET
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MessageDirection(Enum):
    """消息方向"""
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class OperationMode(Enum):
    """操作模式"""
    DOM = "dom"
    SMART = "smart"


class PageState(Enum):
    """页面状态"""
    NORMAL = "normal"
    LOGGED_OUT = "logged_out"
    EXCEPTION = "exception"


@dataclass
class RPAMessage:
    """RPA消息对象"""
    customer_name: str
    content: str
    direction: MessageDirection
    timestamp: float
    conversation_id: str = ""
    customer_id: str = ""
    sender_id: str = ""
    is_new: bool = False
    msg_id: str = ""
    create_time: str = ""
    unread_count: int = 0
    signal_source: str = ""
    direction_confidence: str = "high"


@dataclass
class OperationResult:
    """操作结果"""
    success: bool
    mode: OperationMode
    error: Optional[str] = None
    retry_count: int = 0
    duration: float = 0
    message: Optional[str] = None


@dataclass
class InboundDecisionResult:
    """统一描述入站状态机的决策结果，供内部收口后再兼容回旧返回值。"""
    action: str
    content: str
    unread_count: int
    evidence: str = "reject"
    signal_source: str = ""


# DOM选择器池 - 提高抗风控能力
SELECTOR_POOL = {
    "search_input": [
        'input[placeholder*="搜索"]',
        'input[placeholder*="search"]',
        'input[type="search"]',
        '[role="searchbox"] input',
        '[class*="search"] input'
    ],
    "conversation_item": [
        '[data-e2e="conversation-item"]',
        '.chat-list-item',
        '[class*="conversationItem"]',
        '[class*="chat-item"]',
        '[class*="conversation"]',
        '[class*="session"]'
    ],
    "input_box": [
        'div[contenteditable="true"]',
        '[data-e2e="chat-input"]',
        '.public-DraftEditor-content',
        '[class*="editor"]',
        '[class*="input-area"]'
    ],
    "send_button": [
        '[data-e2e="send-msg-btn"]',
        '[data-e2e="chat-send"]',
        'button:has-text("发送")',
        '[class*="send-btn"]',
        '[class*="sendMsgBtn"]'
    ]
}

# 占位符：用于在状态字段中表示"未设置"
_STATE_UNSET = object()


__all__ = [
    "MessageDirection",
    "OperationMode",
    "PageState",
    "RPAMessage",
    "OperationResult",
    "InboundDecisionResult",
    "SELECTOR_POOL",
    "_STATE_UNSET",
]
