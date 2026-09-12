"""
轻量级调试埋点（DBG-XXX）工具。

[REFACTOR-INST:convergence] 替代散落在 5+ 个文件中的 [DBG-INST:greeting-no-reply] 风格埋点：

```python
from src.common.debug_instrument import debug_event

# 自动判断是否启用（默认开启，可通过 HUOKE_DEBUG_TOPICS=DBG-VERIFY,DBG-SNAP 控制白名单）
debug_event("DBG-VERIFY", "enter", timeout_ms=800, js_len=20)
debug_event("DBG-SANITIZE", "dispatch_pre_click", empty_blocks=3)
```

支持 topics 白名单 / 全局开关 / 结构化字段，**不再需要手工 try/except 包裹 logger.warning**。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

# 全局开关：HUOKE_DEBUG_OFF=1 关闭所有埋点
_OFF = bool(int(os.environ.get("HUOKE_DEBUG_OFF", "0") or 0))
# Topics 白名单：HUOKE_DEBUG_TOPICS=DBG-VERIFY,DBG-SANITIZE（逗号分隔）
_TOPICS = {
    t.strip()
    for t in (os.environ.get("HUOKE_DEBUG_TOPICS", "") or "").split(",")
    if t.strip()
}


def is_enabled(topic: str) -> bool:
    """判断 topic 是否启用。

    规则：
    - HUOKE_DEBUG_OFF=1 → 全部关闭
    - HUOKE_DEBUG_TOPICS 非空 → 仅白名单内开启
    - 默认（无环境变量）→ 全部开启
    """
    if _OFF:
        return False
    if _TOPICS:
        return topic in _TOPICS
    return True


def debug_event(
    topic: str,
    event: str,
    **fields: Any,
) -> Optional[Dict[str, Any]]:
    """发出一条埋点日志。

    Args:
        topic: 埋点主题，建议 `DBG-XXX` 形式（如 "DBG-VERIFY"）
        event: 事件名（enter / exit / exception / fast_path 等）
        **fields: 结构化字段，会被合并到日志中

    Returns:
        实际写入的 dict（调试用），未启用时返回 None
    """
    if not is_enabled(topic):
        return None
    try:
        from loguru import logger
        payload: Dict[str, Any] = {"topic": topic, "event": event}
        payload.update(fields)
        logger.bind(**payload).debug(f"[{topic}] {event}")
        return payload
    except Exception:
        # 埋点失败不能影响业务
        return None
