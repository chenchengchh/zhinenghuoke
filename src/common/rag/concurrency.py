"""
LLM 推理并发控制和重试

提供：
- 信号量限流（控制同时进行的 LLM 调用数）
- 指数退避重试（自动重试临时性失败）
- 速率统计（调用次数、平均延迟、成功率）
"""
import time
import threading
import logging
import asyncio
from typing import Any, Callable, Optional, TypeVar, Awaitable
from functools import wraps
from functools import lru_cache

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ConcurrencyLimiter:
    """并发限流器（基于信号量）。"""

    def __init__(self, max_concurrent: int = 5):
        self.max_concurrent = max(1, int(max_concurrent))
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)
        # 统计
        self._lock = threading.Lock()
        self._stats = {
            "active_count": 0,
            "total_acquired": 0,
            "total_released": 0,
            "peak_active": 0,
        }

    @property
    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """获取一个许可。"""
        acquired = self._semaphore.acquire(timeout=timeout)
        if acquired:
            with self._lock:
                self._stats["active_count"] += 1
                self._stats["total_acquired"] += 1
                if self._stats["active_count"] > self._stats["peak_active"]:
                    self._stats["peak_active"] = self._stats["active_count"]
        return acquired

    def release(self) -> None:
        """释放一个许可。"""
        try:
            self._semaphore.release()
            with self._lock:
                self._stats["active_count"] = max(0, self._stats["active_count"] - 1)
                self._stats["total_released"] += 1
        except ValueError:
            # release 超过 acquire 次数
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def with_retry(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 10.0,
    backoff_factor: float = 2.0,
    retriable_exceptions: tuple = (Exception,),
):
    """指数退避重试装饰器。

    Args:
        max_attempts: 最大尝试次数
        initial_delay: 首次重试延迟（秒）
        max_delay: 最大重试延迟（秒）
        backoff_factor: 退避系数
        retriable_exceptions: 可重试的异常类型
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            delay = initial_delay
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retriable_exceptions as e:
                    last_exc = e
                    if attempt >= max_attempts:
                        logger.error(
                            f"[{func.__name__}] 重试 {max_attempts} 次仍失败: {e}"
                        )
                        raise
                    logger.warning(
                        f"[{func.__name__}] 第 {attempt} 次失败，{delay:.1f}s 后重试: {e}"
                    )
                    time.sleep(delay)
                    delay = min(delay * backoff_factor, max_delay)
            # 不应到达此处
            raise last_exc  # type: ignore
        return wrapper
    return decorator


class LLMCallStats:
    """LLM 调用统计。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "total_calls": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "retried_calls": 0,
            "timeout_calls": 0,
            "total_latency_ms": 0.0,
            "min_latency_ms": float("inf"),
            "max_latency_ms": 0.0,
        }

    def record_call(
        self,
        success: bool,
        latency_ms: float,
        retried: bool = False,
        timeout: bool = False,
    ) -> None:
        with self._lock:
            self._data["total_calls"] += 1
            if success:
                self._data["successful_calls"] += 1
            else:
                self._data["failed_calls"] += 1
            if retried:
                self._data["retried_calls"] += 1
            if timeout:
                self._data["timeout_calls"] += 1
            self._data["total_latency_ms"] += latency_ms
            if latency_ms < self._data["min_latency_ms"]:
                self._data["min_latency_ms"] = latency_ms
            if latency_ms > self._data["max_latency_ms"]:
                self._data["max_latency_ms"] = latency_ms

    def snapshot(self) -> dict:
        with self._lock:
            total = self._data["total_calls"]
            avg = (
                self._data["total_latency_ms"] / total if total > 0 else 0.0
            )
            success_rate = (
                self._data["successful_calls"] / total if total > 0 else 0.0
            )
            return {
                **self._data,
                "avg_latency_ms": round(avg, 2),
                "success_rate": round(success_rate, 4),
            }


class LLMConcurrencyController:
    """LLM 并发控制器。"""

    def __init__(self, max_concurrent: int = 5):
        self.limiter = ConcurrencyLimiter(max_concurrent)
        self.stats = LLMCallStats()

    def call_with_control(
        self,
        func: Callable[..., T],
        *args,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> T:
        """在并发控制下调用 LLM 函数。"""
        start = time.time()
        acquired = self.limiter.acquire(timeout=timeout)
        if not acquired:
            self.stats.record_call(
                success=False, latency_ms=0, timeout=True
            )
            raise TimeoutError(
                f"获取 LLM 许可超时（{timeout}s），可能并发已满"
            )
        try:
            result = func(*args, **kwargs)
            latency_ms = (time.time() - start) * 1000
            self.stats.record_call(success=True, latency_ms=latency_ms)
            return result
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            self.stats.record_call(
                success=False, latency_ms=latency_ms, timeout=isinstance(e, TimeoutError)
            )
            raise
        finally:
            self.limiter.release()

    def get_stats(self) -> dict:
        """获取完整统计。"""
        return {
            "limiter": self.limiter.stats,
            "calls": self.stats.snapshot(),
        }


@lru_cache(maxsize=1)
def get_llm_concurrency_controller() -> LLMConcurrencyController:
    """获取 LLM 并发控制器单例。"""
    return LLMConcurrencyController(max_concurrent=5)
