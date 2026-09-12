import os
import time
import random
import sys
import asyncio
import threading
import multiprocessing as mp
from loguru import logger
from typing import Optional, Callable
from src.config.settings import MIN_SLEEP, MAX_SLEEP
from src.infrastructure.runtime_paths import get_log_dir

_logger_initialized = False
_logger_lock = threading.Lock()


def build_process_scoped_log_file(base_name: str = "douyin_bot") -> str:
    """为当前进程生成独立日志文件，避免 Windows 下多进程轮转冲突。"""
    process_name = str(getattr(mp.current_process(), "name", "") or "process").strip().lower()
    safe_process_name = "".join(ch if ch.isalnum() else "_" for ch in process_name).strip("_") or "process"
    return f"{base_name}.{safe_process_name}.{os.getpid()}.log"

def setup_logger(name="main"):
    """配置日志（防止重复添加handler，线程安全）"""
    global _logger_initialized
    with _logger_lock:
        if _logger_initialized:
            return logger
        _logger_initialized = True
        logger.add(
            sys.stderr,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            level="INFO"
        )
        logger.add(
            str(get_log_dir() / build_process_scoped_log_file()),
            rotation="10 MB",
            retention="7 days",
            level="DEBUG",
            enqueue=True,
            backtrace=True,
            diagnose=False
        )
        return logger

def random_sleep(min_seconds=None, max_seconds=None):
    """
    随机等待一段时间，模拟人类操作间隔

    Args:
        min_seconds: 最小等待时间，默认使用配置
        max_seconds: 最大等待时间，默认使用配置
    """
    if min_seconds is None:
        min_seconds = MIN_SLEEP
    if max_seconds is None:
        max_seconds = MAX_SLEEP


    sleep_time = random.uniform(min_seconds, max_seconds)
    logger.info(f"等待 {sleep_time:.2f} 秒...")
    time.sleep(sleep_time)


def interruptible_sleep(
    total_seconds: float,
    stop_check: Optional[Callable[[], bool]] = None,
    poll_interval: float = 1.0,
) -> bool:
    """
    阶段C·C-2：可被 stop_check 中断的 sleep。

    每 `poll_interval` 秒检查一次 stop_check()，若为 True 则立刻返回 True，
    主循环据此退出；否则完整 sleep 总时长后返回 False。

    Args:
        total_seconds: 总睡眠时长（秒）
        stop_check: 接收一个无参可调用对象，返回 True 时中断
        poll_interval: 检查间隔，1s 较均衡；最长 1s 响应停止信号

    Returns:
        True 表示被 stop_check 提前中断，False 表示正常完成。
    """
    if total_seconds <= 0:
        return bool(stop_check and stop_check())
    interval = max(0.1, min(float(poll_interval or 1.0), 2.0))
    elapsed = 0.0
    while elapsed < total_seconds:
        if stop_check is not None and stop_check():
            return True
        sleep_chunk = min(interval, total_seconds - elapsed)
        time.sleep(sleep_chunk)
        elapsed += sleep_chunk
    if stop_check is not None and stop_check():
        return True
    return False


_event_loop_lock = __import__('threading').Lock()

def get_safe_event_loop():
    """
    安全获取asyncio事件循环，避免asyncio.new_event_loop()冲突
    
    在多线程环境中，直接调用asyncio.new_event_loop()可能导致：
    1. 与现有运行中的事件循环冲突
    2. 资源泄漏（未关闭的旧事件循环）
    3. 线程安全问题
    
    Returns:
        asyncio.AbstractEventLoop: 安全的事件循环实例
    """
    with _event_loop_lock:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            return loop
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop


def run_async_safe(coro, raise_on_error=False):
    """
    安全运行异步协程，自动处理事件循环
    
    Args:
        coro: 异步协程对象
        raise_on_error: 是否在失败时抛出异常
        
    Returns:
        协程的返回值
    """
    loop = get_safe_event_loop()
    try:
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        else:
            return loop.run_until_complete(coro)
    except Exception as e:
        logger.warning(f"异步执行失败: {e}")
        if raise_on_error:
            raise
        return None


class _JiebaLazyLoader:
    """jieba延迟加载单例，避免每次调用都重新初始化"""
    _instance = None
    _lock = __import__('threading').Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._module = None
        return cls._instance

    def __getattr__(self, name):
        if self._module is None:
            with self._lock:
                if self._module is None:
                    import jieba
                    self._module = jieba
        return getattr(self._module, name)


_jieba_module = _JiebaLazyLoader()


import uuid as _uuid

def generate_message_id(prefix: str = "msg") -> str:
    """统一消息ID生成函数，确保全局格式一致

    格式: {prefix}_{timestamp_ms}_{uuid_hex[:8]}
    示例: msg_1713945600000_a1b2c3d4

    Args:
        prefix: ID前缀，如msg(入站)、auto(自动回复)、rpa(RPA消息)

    Returns:
        str: 统一格式的消息ID
    """
    return f"{prefix}_{int(time.time() * 1000)}_{_uuid.uuid4().hex[:8]}"


import concurrent.futures as _concurrent_futures
_llm_call_executor = _concurrent_futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm-safe")
_llm_runtime_lock = threading.Lock()
_llm_runtime_inflight = 0


def _llm_runtime_begin() -> int:
    global _llm_runtime_inflight
    with _llm_runtime_lock:
        _llm_runtime_inflight += 1
        return _llm_runtime_inflight


def _llm_runtime_end() -> int:
    global _llm_runtime_inflight
    with _llm_runtime_lock:
        _llm_runtime_inflight = max(_llm_runtime_inflight - 1, 0)
        return _llm_runtime_inflight


def get_llm_runtime_inflight() -> int:
    with _llm_runtime_lock:
        return _llm_runtime_inflight

def call_llm_safe(
    llm_service,
    prompt: str,
    system_prompt: str = "",
    timeout: float = 30.0,
    **kwargs,
) -> str:
    """统一LLM调用工具函数（安全事件循环处理+超时保护+多接口适配）

    适配以下LLM服务接口：
    1. llm_service.chat(message=..., systemPrompt=..., history=...)
    2. llm_service.chat(prompt=..., system_prompt=...)
    3. llm_service.client.chat(message=..., temperature=...)
    4. llm_service.generate(prompt)
    5. llm_service.generate_sync(prompt)

    Args:
        llm_service: LLM服务实例
        prompt: 用户提示词
        system_prompt: 系统提示词
        timeout: 超时时间（秒）

    Returns:
        str: LLM返回的文本，失败返回空字符串
    """
    if not llm_service:
        return ""

    safe_timeout = max(float(timeout or 0), 0.1)
    request_id = _uuid.uuid4().hex[:8]
    provider_name = type(llm_service).__name__
    model_name = str(getattr(llm_service, "model", provider_name) or provider_name)
    prompt_len = len(str(prompt or ""))
    system_prompt_len = len(str(system_prompt or ""))
    started_at = time.perf_counter()
    inflight_after_begin = _llm_runtime_begin()
    completion_state = {"timed_out": False}
    logger.info(
        f"[LLM-RUNTIME] start request_id={request_id} provider={provider_name} model={model_name} "
        f"timeout={safe_timeout:.1f}s inflight={inflight_after_begin} prompt_len={prompt_len} "
        f"system_prompt_len={system_prompt_len}"
    )

    def _do_call():
        try:
            if hasattr(llm_service, 'chat'):
                import asyncio
                if asyncio.iscoroutinefunction(llm_service.chat):
                    result = run_async_safe(
                        llm_service.chat(
                            message=prompt,
                            systemPrompt=system_prompt,
                            history=[],
                            timeout=safe_timeout,
                            **kwargs,
                        )
                    )
                    return result if isinstance(result, str) else str(result) if result else ""
                else:
                    try:
                        result = llm_service.chat(
                            message=prompt,
                            systemPrompt=system_prompt,
                            history=[],
                            timeout=safe_timeout,
                            **kwargs,
                        )
                    except TypeError:
                        result = llm_service.chat(
                            prompt=prompt,
                            system_prompt=system_prompt,
                            timeout=safe_timeout,
                            **kwargs,
                        )
                    return result if isinstance(result, str) else str(result) if result else ""
            elif hasattr(llm_service, 'generate'):
                try:
                    result = llm_service.generate(prompt, timeout=safe_timeout, **kwargs)
                except TypeError:
                    result = llm_service.generate(prompt)
                return result if isinstance(result, str) else str(result) if result else ""
            elif hasattr(llm_service, 'generate_sync'):
                try:
                    result = llm_service.generate_sync(prompt, timeout=safe_timeout, **kwargs)
                except TypeError:
                    result = llm_service.generate_sync(prompt)
                return result if isinstance(result, str) else str(result) if result else ""
            elif hasattr(llm_service, 'client') and llm_service.client:
                if hasattr(llm_service.client, 'chat'):
                    result = run_async_safe(
                        llm_service.client.chat(
                            message=prompt,
                            temperature=0.7,
                            timeout=safe_timeout,
                        )
                    )
                    return result if isinstance(result, str) else str(result) if result else ""
            return ""
        except Exception as e:
            logger.warning(f"LLM调用内部错误: {e}")
            return ""

    try:
        future = _llm_call_executor.submit(_do_call)
    except Exception as submit_e:
        inflight_after_end = _llm_runtime_end()
        logger.warning(f"LLM调用线程池饱和: {submit_e}")
        logger.warning(
            f"[LLM-RUNTIME] submit_failed request_id={request_id} provider={provider_name} model={model_name} "
            f"inflight={inflight_after_end} error={str(submit_e)[:120]}"
        )
        return ""

    def _on_done(done_future):
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        suppress_late_logging = bool(completion_state.get("timed_out"))
        try:
            result = done_future.result()
            result_len = len(str(result or ""))
            inflight_after_end = _llm_runtime_end()
            if suppress_late_logging:
                return
            logger.info(
                f"[LLM-RUNTIME] done request_id={request_id} provider={provider_name} model={model_name} "
                f"elapsed={elapsed_ms}ms inflight={inflight_after_end} result_len={result_len}"
            )
        except _concurrent_futures.CancelledError:
            inflight_after_end = _llm_runtime_end()
            if suppress_late_logging:
                return
            logger.warning(
                f"[LLM-RUNTIME] cancelled request_id={request_id} provider={provider_name} model={model_name} "
                f"elapsed={elapsed_ms}ms inflight={inflight_after_end}"
            )
        except Exception as done_e:
            inflight_after_end = _llm_runtime_end()
            if suppress_late_logging:
                return
            logger.warning(
                f"[LLM-RUNTIME] failed request_id={request_id} provider={provider_name} model={model_name} "
                f"elapsed={elapsed_ms}ms inflight={inflight_after_end} error={str(done_e)[:120]}"
            )

    future.add_done_callback(_on_done)

    try:
        return future.result(timeout=safe_timeout)
    except _concurrent_futures.TimeoutError:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        completion_state["timed_out"] = True
        cancelled = future.cancel()
        logger.warning(f"LLM调用超时({safe_timeout}秒)")
        logger.warning(
            f"[LLM-RUNTIME] timeout request_id={request_id} provider={provider_name} model={model_name} "
            f"elapsed={elapsed_ms}ms inflight={get_llm_runtime_inflight()} cancel_requested={cancelled}"
        )
        return ""
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        logger.error(f"LLM调用异常: {e}")
        logger.error(
            f"[LLM-RUNTIME] outer_exception request_id={request_id} provider={provider_name} model={model_name} "
            f"elapsed={elapsed_ms}ms inflight={get_llm_runtime_inflight()} error={str(e)[:120]}"
        )
        return ""


def normalize_direction(raw_direction, default="unknown"):
    if raw_direction is None:
        return default
    if hasattr(raw_direction, "value"):
        raw_direction = getattr(raw_direction, "value", raw_direction)
    text = str(raw_direction).strip().lower()
    if text in {"inbound", "incoming", "receive", "received", "recv", "user", "customer"}:
        return "inbound"
    if text in {"outbound", "outgoing", "send", "sent", "self", "me", "assistant", "agent", "bot", "service"}:
        return "outbound"
    if text in {"system", "notification", "notice"}:
        return "system"
    return default


import threading as _threading

class ThreadPoolManager:
    """统一线程池管理器

    集中管理所有线程池的生命周期，提供：
    1. 统一创建和注册线程池
    2. 健康监控（活跃线程数、任务队列长度）
    3. 优雅关闭（按依赖顺序关闭）
    4. 防止重复创建

    使用方式：
        manager = ThreadPoolManager()
        executor = manager.get_or_create("llm", max_workers=5)
        manager.shutdown_all()
    """

    def __init__(self):
        self._pools: dict = {}
        self._lock = _threading.Lock()

    def get_or_create(self, name: str, max_workers: int = 2, prefix: str = "") -> _concurrent_futures.ThreadPoolExecutor:
        """获取或创建命名线程池

        Args:
            name: 线程池名称（如llm、reply、tool、embed）
            max_workers: 最大工作线程数
            prefix: 线程名前缀

        Returns:
            ThreadPoolExecutor: 线程池实例
        """
        with self._lock:
            if name in self._pools:
                pool = self._pools[name]["executor"]
                if not pool._shutdown:
                    return pool
            thread_prefix = prefix or f"tpm_{name}"
            executor = _concurrent_futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=thread_prefix
            )
            self._pools[name] = {
                "executor": executor,
                "max_workers": max_workers,
                "created_at": time.time()
            }
            return executor

    def get_health(self) -> dict:
        """获取所有线程池的健康状态"""
        with self._lock:
            health = {}
            for name, info in self._pools.items():
                pool = info["executor"]
                try:
                    active = sum(1 for t in getattr(pool, '_threads', set()) if t.is_alive())
                except Exception:
                    active = -1
                health[name] = {
                    "max_workers": info["max_workers"],
                    "active_threads": active,
                    "shutdown": pool._shutdown,
                    "age_seconds": int(time.time() - info["created_at"])
                }
            return health

    def shutdown(self, name: str, wait: bool = True):
        """关闭指定名称的线程池"""
        with self._lock:
            if name in self._pools:
                pool = self._pools.pop(name)["executor"]
                pool.shutdown(wait=wait)

    def shutdown_all(self, wait: bool = True):
        """关闭所有线程池（按创建时间倒序，后创建的先关闭）"""
        with self._lock:
            names = sorted(self._pools.keys(), key=lambda n: self._pools[n]["created_at"], reverse=True)
            for name in names:
                pool = self._pools.pop(name)["executor"]
                pool.shutdown(wait=wait)


_global_pool_manager: ThreadPoolManager = None
_pool_manager_lock = _threading.Lock()

def get_thread_pool_manager() -> ThreadPoolManager:
    """获取全局线程池管理器单例"""
    global _global_pool_manager
    if _global_pool_manager is None:
        with _pool_manager_lock:
            if _global_pool_manager is None:
                _global_pool_manager = ThreadPoolManager()
    return _global_pool_manager
