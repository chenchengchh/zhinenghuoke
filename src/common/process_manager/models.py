"""
process_manager.models - 进程管理的数据模型与常量层

阶段D·D-2 拆分产物：
将原 process_manager.py 中"纯数据 + 纯配置"的部分抽出，使 core.py 专注于类实现。

本模块只放：
- 枚举（ProcessRole / ProcessState）
- 数据类 / 值对象（IPCMessage）
- 模块级配置常量
"""

import time
from enum import Enum
from typing import Dict, Any


class ProcessRole(Enum):
    """进程角色枚举"""
    CRAWLER = "crawler"       # 爬取/私信进程


class ProcessState(Enum):
    """进程状态枚举"""
    STOPPED = "stopped"       # 已停止
    STARTING = "starting"     # 启动中
    RUNNING = "running"       # 运行中
    STOPPING = "stopping"     # 停止中
    ERROR = "error"           # 错误


class IPCMessage:
    """进程间通信消息"""

    def __init__(self, msg_type: str, source: str, data: Dict[str, Any] = None):
        """
        初始化IPC消息

        Args:
            msg_type: 消息类型
            source: 消息来源 (crawler/monitor/main)
            data: 消息数据
        """
        self.msg_type = msg_type
        self.source = source
        self.data = data or {}
        self.timestamp = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可跨进程传输的字典。"""
        return {
            "msg_type": self.msg_type,
            "source": self.source,
            "data": self.data,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IPCMessage":
        """从字典反序列化。"""
        msg = cls(d["msg_type"], d["source"], d.get("data", {}))
        msg.timestamp = d.get("timestamp", time.time())
        return msg


__all__ = [
    "ProcessRole",
    "ProcessState",
    "IPCMessage",
]
