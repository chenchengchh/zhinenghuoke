"""
rpa_engine - 抖音RPA引擎子包

阶段D·D-1 拆分产物：
将原 rpa_engine.py（7279 行单体）按职责拆为多层子模块。

当前子模块：
- core    : DouYinRPAEngine 主类实现
- models  : 枚举 / 数据类 / DOM 选择器池 / 状态标记

后续阶段将进一步把 core 拆为：
- browser   : 浏览器/页面交互
- navigator : 页面导航与状态
- sender    : 私信发送流水线
- monitor   : 消息监控与去重

公共 API（保持向后兼容）：
原 rpa_engine.py 中导出的所有符号，本子包在 __all__ 中统一重新导出，
旧引用（`from src.douyin_bot.rpa_engine import DouYinRPAEngine, ...`）
可以无修改继续工作。
"""

from .core import DouYinRPAEngine
from .models import (
    MessageDirection,
    OperationMode,
    PageState,
    RPAMessage,
    OperationResult,
    InboundDecisionResult,
    SELECTOR_POOL,
    _STATE_UNSET,
)

__all__ = [
    "DouYinRPAEngine",
    "MessageDirection",
    "OperationMode",
    "PageState",
    "RPAMessage",
    "OperationResult",
    "InboundDecisionResult",
    "SELECTOR_POOL",
    # _STATE_UNSET 是内部标记对象，外部代码不应依赖，但允许从子包导出
    # 以便核心代码或测试可以 import。
    "_STATE_UNSET",
]
