"""
统一单例装饰器

替代代码中散落的 `_instance = None` + `_lock` + `if _instance is None: ...` 模式。
使用 `@singleton` 装饰器，一行代码即可获得线程安全的单例。
"""
import functools
import threading
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def singleton(cls: type) -> Callable:
    """
    线程安全的单例装饰器（基于双重检查锁定）。

    用法:
        @singleton
        class MyService:
            def __init__(self, ...):
                ...

        # 调用
        instance = MyService(...)

    特性:
    - 线程安全（双重检查锁）
    - 首次调用时构造
    - 后续调用返回同一实例
    - 忽略额外参数（如果不同调用方传不同参数，后续调用以首次为准）
    """
    _instance = None
    _lock = threading.Lock()

    @functools.wraps(cls)
    def wrapper(*args, **kwargs):
        nonlocal _instance
        if _instance is None:
            with _lock:
                # 双重检查，避免重复构造
                if _instance is None:
                    _instance = cls(*args, **kwargs)
        return _instance

    # 提供 reset 方法用于测试
    def _reset_singleton():
        nonlocal _instance
        with _lock:
            _instance = None

    wrapper._reset_singleton = _reset_singleton
    wrapper._cls = cls
    return wrapper


def lru_singleton(maxsize: int = 1):
    """
    工厂方法版单例装饰器，支持参数化实例缓存。

    与 @singleton 的区别：lru_singleton 会按参数缓存不同实例。
    注意：参数必须可哈希。

    用法:
        @lru_singleton(maxsize=8)
        def get_db(connect_str):
            return Database(connect_str)

        db1 = get_db("postgresql://...")
        db2 = get_db("postgresql://...")  # 同一实例
        db3 = get_db("mongodb://...")     # 不同实例
    """
    from functools import lru_cache

    def decorator(func: Callable) -> Callable:
        return lru_cache(maxsize=maxsize)(func)

    return decorator
