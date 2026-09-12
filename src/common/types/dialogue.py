"""
统一对话类型定义

整合所有对话相关的数据模型，避免重复定义
"""
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from datetime import datetime
from enum import Enum


class DialogueStateEnum(Enum):
    """
    对话状态枚举
    
    整合了 dialogue_state_tracker.py 中的定义
    """
    INIT = "init"                       # 初始状态
    INTENT_RECOGNIZED = "intent"        # 意图已识别
    SLOT_FILLING = "slots"              # 槽位填充中
    PENDING_CONFIRM = "confirm"         # 待确认
    ANSWERING = "answering"             # 回答中
    TOPIC_SWITCH = "topic_switch"       # 话题切换
    COMPLETED = "completed"             # 已完成
    HUMAN_TRANSFER = "human_transfer"   # 转人工


@dataclass
class DialogueStateData:
    """
    对话状态数据类
    
    整合了 context_understanding.py 中的定义
    """
    current_topic: str = ""
    current_intent: str = ""
    pending_slots: Dict[str, Any] = field(default_factory=dict)
    confirmed_slots: Dict[str, Any] = field(default_factory=dict)
    last_entity_mentioned: Optional[str] = None
    topic_history: List[str] = field(default_factory=list)
    intent_history: List[str] = field(default_factory=list)
    updated_at: datetime = field(default_factory=datetime.now)
    
    # 扩展字段
    session_id: str = ""
    customer_id: str = ""
    state: DialogueStateEnum = DialogueStateEnum.INIT
    turn_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    pending_questions: List[str] = field(default_factory=list)
    context_variables: Dict[str, Any] = field(default_factory=dict)
    slots: Dict[str, 'Slot'] = field(default_factory=dict)
    topic_stack: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "current_topic": self.current_topic,
            "current_intent": self.current_intent,
            "pending_slots": self.pending_slots,
            "confirmed_slots": self.confirmed_slots,
            "last_entity_mentioned": self.last_entity_mentioned,
            "topic_history": self.topic_history,
            "intent_history": self.intent_history,
            "updated_at": self.updated_at.isoformat(),
            "session_id": self.session_id,
            "customer_id": self.customer_id,
            "state": self.state.value,
            "turn_count": self.turn_count,
            "created_at": self.created_at.isoformat(),
            "pending_questions": self.pending_questions,
            "context_variables": self.context_variables
        }


@dataclass
class Slot:
    """槽位"""
    name: str
    value: Any = None
    status: str = "empty"  # empty, filled, confirmed, denied
    confirmed_at: Optional[datetime] = None
    source: str = ""  # user_input, system_inferred, default


@dataclass
class DialogueTurn:
    """对话轮次"""
    turn_id: int
    user_message: str
    bot_response: str
    intent: str
    slots: Dict[str, Slot]
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


class EntityType(Enum):
    """实体类型"""
    PERSON = "person"
    PRODUCT = "product"
    PRICE = "price"
    TIME = "time"
    COMPANY = "company"
    BRAND = "brand"
    FEATURE = "feature"
    SERVICE = "service"
    UNKNOWN = "unknown"


@dataclass
class Entity:
    """实体"""
    name: str
    type: EntityType
    aliases: List[str] = field(default_factory=list)
    first_mentioned: int = 0
    last_mentioned: int = 0
    mentions: int = 1


@dataclass
class ContextWindow:
    """上下文窗口"""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    entities: Dict[str, Entity] = field(default_factory=dict)
    state: DialogueStateData = field(default_factory=DialogueStateData)
    window_size: int = 5
    last_accessed: float = field(default_factory=lambda: __import__('time').time())
