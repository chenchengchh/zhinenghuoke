"""
企业级熔断器

提供：
1. 熔断模式
2. 限流控制
3. 自动恢复
"""

import time
import threading
from typing import Callable, Any
from enum import Enum
from loguru import logger
from dataclasses import dataclass


class CircuitState(Enum):
    """熔断状态"""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """熔断器配置"""
    failure_threshold: int = 5
    success_threshold: int = 3
    timeout: float = 60.0
    half_open_max_calls: int = 3
    max_timeout: float = 600.0
    backoff_factor: float = 2.0


class CircuitOpenError(Exception):
    """熔断器打开异常"""
    pass


class CircuitBreaker:
    """
    企业级熔断器

    特性：
    1. 三态切换 - CLOSED/OPEN/HALF_OPEN
    2. 失败计数 - 连续失败触发熔断
    3. 自动恢复 - 超时后尝试恢复
    4. 统计监控 - 记录成功/失败
    """

    def __init__(self, name: str, config: CircuitBreakerConfig = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0
        self._half_open_calls = 0
        self._lock = threading.RLock()
        self._current_timeout = self.config.timeout
        self._consecutive_opens = 0

        self._stats = {
            "total_calls": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "rejected_calls": 0,
            "state_changes": 0
        }

    @property
    def state(self) -> CircuitState:
        """获取当前状态（只读，不触发状态转换）"""
        with self._lock:
            return self._state

    def _transition_to(self, new_state: CircuitState):
        """统一状态转换方法（带指数退避，调用方需持有锁或在锁上下文中调用）"""
        if self._state != new_state:
            logger.warning(f"Circuit {self.name}: {self._state.value} -> {new_state.value}")
            self._state = new_state
            self._stats["state_changes"] += 1

            if new_state == CircuitState.HALF_OPEN:
                self._half_open_calls = 0
                self._success_count = 0
            elif new_state == CircuitState.CLOSED:
                self._failure_count = 0
                self._success_count = 0
                self._consecutive_opens = 0
                self._current_timeout = self.config.timeout
            elif new_state == CircuitState.OPEN:
                self._consecutive_opens += 1
                self._current_timeout = min(
                    self.config.timeout * (self.config.backoff_factor ** (self._consecutive_opens - 1)),
                    self.config.max_timeout
                )
                logger.info(f"Circuit {self.name}: 指数退避，当前超时={self._current_timeout:.0f}s, 连续打开次数={self._consecutive_opens}")

    def _check_and_prepare_call(self):
        """检查熔断器状态并准备调用（统一入口，避免__enter__和call中重复逻辑）

        调用方需持有锁或在锁上下文中调用。

        Raises:
            CircuitOpenError: 熔断器打开时抛出
        """
        self._stats["total_calls"] += 1
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time >= self._current_timeout:
                self._transition_to(CircuitState.HALF_OPEN)
        current_state = self._state
        if current_state == CircuitState.OPEN:
            self._stats["rejected_calls"] += 1
            raise CircuitOpenError(f"Circuit {self.name} is OPEN")
        if current_state == CircuitState.HALF_OPEN:
            self._half_open_calls += 1
            if self._half_open_calls > self.config.half_open_max_calls:
                self._stats["rejected_calls"] += 1
                raise CircuitOpenError(f"Circuit {self.name} is HALF_OPEN (max test calls reached)")

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        通过熔断器调用函数

        Args:
            func: 要执行的函数
            *args, **kwargs: 函数参数

        Returns:
            函数执行结果

        Raises:
            CircuitOpenError: 熔断器打开时抛出
        """
        with self._lock:
            self._check_and_prepare_call()

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def __enter__(self):
        """上下文管理器入口"""
        with self._lock:
            self._check_and_prepare_call()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        if exc_type is None:
            self._on_success()
        else:
            self._on_failure()
        return False

    def _on_success(self):
        """处理成功（仅在CLOSED/HALF_OPEN状态下更新计数器，避免飞行中请求污染OPEN状态）"""
        with self._lock:
            self._stats["successful_calls"] += 1
            if self._state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                self._success_count += 1

            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self.config.success_threshold:
                    self._failure_count = 0
                    self._transition_to(CircuitState.CLOSED)
            elif self._state == CircuitState.CLOSED:
                if self._failure_count > 0:
                    self._failure_count = max(0, self._failure_count - 1)

    def _on_failure(self):
        """处理失败（仅在CLOSED/HALF_OPEN状态下更新计时器，避免飞行中请求重置OPEN超时）"""
        with self._lock:
            self._stats["failed_calls"] += 1
            if self._state == CircuitState.OPEN:
                return
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.OPEN)
            elif self._failure_count >= self.config.failure_threshold:
                self._transition_to(CircuitState.OPEN)

    def record_failure(self):
        """手动记录一次失败（供外部调用，如LLM超时场景）"""
        self._on_failure()

    def record_success(self):
        """手动记录一次成功（供外部调用）"""
        self._on_success()

    def get_stats(self) -> dict:
        """获取统计信息"""
        with self._lock:
            return {
                **self._stats,
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count
            }

    def reset(self):
        """重置熔断器"""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0
            logger.info(f"Circuit {self.name} 已重置")


class RateLimiter:
    """
    限流器

    特性：
    1. 滑动窗口算法
    2. 令牌桶算法
    3. 线程安全
    """

    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls = []
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        """检查是否允许调用"""
        with self._lock:
            current_time = time.time()
            self._calls = [t for t in self._calls if current_time - t < self.window_seconds]

            if len(self._calls) < self.max_calls:
                self._calls.append(current_time)
                return True
            return False

    def acquire(self, blocking: bool = True, timeout: float = None) -> bool:
        """
        获取限流许可

        Args:
            blocking: 是否阻塞
            timeout: 超时时间

        Returns:
            是否获取成功
        """
        if not blocking:
            return self.is_allowed()

        start_time = time.time()
        _wait_event = threading.Event()
        while True:
            if self.is_allowed():
                return True
            if timeout and (time.time() - start_time) >= timeout:
                return False
            _wait_event.wait(timeout=0.1)

    def get_remaining(self) -> int:
        """获取剩余调用次数"""
        with self._lock:
            current_time = time.time()
            self._calls = [t for t in self._calls if current_time - t < self.window_seconds]
            return max(0, self.max_calls - len(self._calls))

    def reset(self):
        """重置限流器"""
        with self._lock:
            self._calls.clear()
