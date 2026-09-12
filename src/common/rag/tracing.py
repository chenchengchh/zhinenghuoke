"""
RAG 调用追踪装饰器

提供 span 级别的调用追踪，便于排查慢请求和失败请求。
"""
import time
import functools
import logging
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)


def trace_rag_call(span_name: str = None):
    """RAG 调用追踪装饰器。

    用法:
        @trace_rag_call("process_message")
        def process_message(self, ...):
            ...

    日志格式:
        [RAG:abcd1234] → process_message start
        [RAG:abcd1234] ✓ process_message done (0.12s)
        [RAG:abcd1234] ✗ process_message failed (0.05s): ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            span_id = str(uuid.uuid4())[:8]
            name = span_name or func.__name__
            start = time.time()
            try:
                logger.info(f"[RAG:{span_id}] → {name} start")
                result = func(*args, **kwargs)
                latency = time.time() - start
                logger.info(f"[RAG:{span_id}] ✓ {name} done ({latency:.2f}s)")
                return result
            except Exception as e:
                latency = time.time() - start
                logger.exception(
                    f"[RAG:{span_id}] ✗ {name} failed ({latency:.2f}s): {e}"
                )
                raise
        return wrapper
    return decorator
