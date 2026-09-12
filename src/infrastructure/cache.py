# -*- coding: utf-8 -*-
"""
缓存服务模块
提供企业级应用的缓存功能
"""
import os
import json
import time
import hashlib
import asyncio
import threading
from typing import Any, Callable, Dict, Generic, List, Optional, TypeVar
from datetime import datetime, timedelta
from functools import wraps, lru_cache
from collections import OrderedDict
from threading import Lock

from src.infrastructure.config import get_config
from src.infrastructure.logger import get_logger

logger = get_logger("cache")
T = TypeVar("T")


class CacheItem(Generic[T]):
    """缓存项"""
    
    def __init__(self, value: T, ttl: int, created_at: Optional[float] = None):
        self.value = value
        self.ttl = ttl
        self.created_at = created_at or time.time()
    
    def is_expired(self) -> bool:
        """检查是否过期"""
        if self.ttl <= 0:
            return False
        return time.time() - self.created_at > self.ttl
    
    @property
    def remaining_ttl(self) -> float:
        """剩余过期时间"""
        if self.ttl <= 0:
            return float("inf")
        return max(0, self.ttl - (time.time() - self.created_at))


class MemoryCache:
    """
    内存缓存实现
    
    特点：
    - LRU淘汰策略
    - TTL过期支持
    - 线程安全
    """
    
    def __init__(self, max_size: int = 1000, default_ttl: int = 3600):
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, CacheItem] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存值"""
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            
            item = self._cache[key]
            if item.is_expired():
                del self._cache[key]
                self._misses += 1
                return None
            
            self._cache.move_to_end(key)
            self._hits += 1
            return item.value
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """设置缓存值"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
            
            while len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            
            self._cache[key] = CacheItem(value, ttl or self.default_ttl)
    
    def delete(self, key: str) -> bool:
        """删除缓存"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    def clear(self) -> None:
        """清空缓存"""
        with self._lock:
            self._cache.clear()
    
    def exists(self, key: str) -> bool:
        """检查缓存是否存在"""
        with self._lock:
            if key not in self._cache:
                return False
            item = self._cache[key]
            if item.is_expired():
                del self._cache[key]
                return False
            return True
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": f"{hit_rate:.2%}"
            }
    
    def cleanup_expired(self) -> int:
        """清理过期缓存"""
        with self._lock:
            expired_keys = [
                key for key, item in self._cache.items()
                if item.is_expired()
            ]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)


class CacheService:
    """
    缓存服务
    
    提供统一的缓存接口，支持多种后端
    """
    _REDIS_HEALTH_CHECK_INTERVAL = 30

    def __init__(
        self,
        cache_type: str = "memory",
        max_size: int = 1000,
        default_ttl: int = 3600,
        redis_url: Optional[str] = None
    ):
        self.cache_type = cache_type
        self.default_ttl = default_ttl
        self._redis_url = redis_url
        self._degraded = False
        self._last_health_check = 0.0
        self._health_lock = threading.Lock()
        
        if cache_type == "memory":
            self._cache = MemoryCache(max_size=max_size, default_ttl=default_ttl)
        elif cache_type == "redis":
            self._cache = self._create_redis_cache(redis_url)
        else:
            raise ValueError(f"不支持的缓存类型: {cache_type}")
    
    def _create_redis_cache(self, redis_url: str):
        """创建Redis缓存"""
        try:
            import redis
            client = redis.from_url(redis_url)
            client.ping()
            return client
        except Exception as e:
            logger.warning(f"Redis不可用({e})，使用内存缓存替代")
            self._degraded = True
            self.cache_type = "memory"
            return MemoryCache(max_size=1000, default_ttl=self.default_ttl)

    def _try_recover_redis(self):
        """尝试恢复Redis连接（降级恢复机制）"""
        if not self._degraded or not self._redis_url:
            return
        now = time.time()
        if now - self._last_health_check < self._REDIS_HEALTH_CHECK_INTERVAL:
            return
        with self._health_lock:
            if now - self._last_health_check < self._REDIS_HEALTH_CHECK_INTERVAL:
                return
            self._last_health_check = now
            try:
                import redis
                client = redis.from_url(self._redis_url)
                client.ping()
                self._cache = client
                self.cache_type = "redis"
                self._degraded = False
                logger.info("Redis连接已恢复，切换回Redis缓存")
            except Exception as e:
                logger.debug(f"Redis恢复检查失败: {e}")
    
    def _generate_key(self, *args, **kwargs) -> str:
        """生成缓存键"""
        key_parts = [str(arg) for arg in args]
        key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
        key_string = ":".join(key_parts)
        return hashlib.md5(key_string.encode()).hexdigest()
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        if self._degraded:
            self._try_recover_redis()
        if self.cache_type == "redis":
            try:
                value = self._cache.get(key)
            except Exception:
                self._degraded = True
                self.cache_type = "memory"
                self._cache = MemoryCache(max_size=1000, default_ttl=self.default_ttl)
                logger.warning("Redis读取失败，降级为内存缓存")
                return self._cache.get(key)
            if value is not None:
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    return None
            return None
        return self._cache.get(key)
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """设置缓存"""
        if self.cache_type == "redis":
            try:
                self._cache.setex(
                    key,
                    ttl or self.default_ttl,
                    json.dumps(value, default=str)
                )
            except Exception:
                self._degraded = True
                self.cache_type = "memory"
                self._cache = MemoryCache(max_size=1000, default_ttl=self.default_ttl)
                logger.warning("Redis写入失败，降级为内存缓存")
                self._cache.set(key, value, ttl)
        else:
            self._cache.set(key, value, ttl)
    
    def delete(self, key: str) -> bool:
        """删除缓存"""
        if self.cache_type == "redis":
            return bool(self._cache.delete(key))
        return self._cache.delete(key)
    
    def get_or_set(
        self,
        key: str,
        factory: Callable[[], T],
        ttl: Optional[int] = None
    ) -> T:
        """
        获取缓存，不存在则创建（双重检查防止竞态）
        
        Args:
            key: 缓存键
            factory: 创建缓存的函数
            ttl: 过期时间
            
        Returns:
            缓存值
        """
        value = self.get(key)
        if value is not None:
            return value
        
        value = factory()
        existing = self.get(key)
        if existing is not None:
            return existing
        self.set(key, value, ttl)
        return value
    
    async def get_or_set_async(
        self,
        key: str,
        factory: Callable[[], T],
        ttl: Optional[int] = None
    ) -> T:
        """
        异步获取缓存，不存在则创建
        """
        value = self.get(key)
        if value is not None:
            return value
        
        if asyncio.iscoroutinefunction(factory):
            value = await factory()
        else:
            value = factory()
        
        self.set(key, value, ttl)
        return value
    
    def invalidate_pattern(self, pattern: str) -> int:
        """
        使匹配模式的缓存失效
        
        Args:
            pattern: 键模式（仅Redis支持）
            
        Returns:
            删除的键数量
        """
        if self.cache_type == "redis":
            keys = self._cache.keys(pattern)
            if keys:
                return self._cache.delete(*keys)
            return 0
        else:
            logger.warning("内存缓存不支持模式匹配删除")
            return 0
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        stats = {
            "cache_type": self.cache_type,
            "default_ttl": self.default_ttl
        }
        
        if self.cache_type == "memory":
            stats.update(self._cache.get_stats())
        
        return stats


def cached(
    key_prefix: str = "",
    ttl: Optional[int] = None,
    key_builder: Optional[Callable] = None
):
    """
    缓存装饰器
    
    自动缓存函数结果
    
    Args:
        key_prefix: 键前缀
        ttl: 过期时间
        key_builder: 自定义键构建函数
    """
    def decorator(func: Callable) -> Callable:
        _cache = MemoryCache()
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            if key_builder:
                cache_key = key_builder(*args, **kwargs)
            else:
                cache_key = f"{key_prefix}:{func.__name__}:{hashlib.md5(str((args, sorted(kwargs.items()))).encode()).hexdigest()}"
            
            result = _cache.get(cache_key)
            if result is not None:
                logger.debug(f"缓存命中: {cache_key}")
                return result
            
            result = func(*args, **kwargs)
            _cache.set(cache_key, result, ttl)
            logger.debug(f"缓存设置: {cache_key}")
            return result
        
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            if key_builder:
                cache_key = key_builder(*args, **kwargs)
            else:
                cache_key = f"{key_prefix}:{func.__name__}:{hashlib.md5(str((args, sorted(kwargs.items()))).encode()).hexdigest()}"
            
            result = _cache.get(cache_key)
            if result is not None:
                logger.debug(f"缓存命中: {cache_key}")
                return result
            
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            
            _cache.set(cache_key, result, ttl)
            logger.debug(f"缓存设置: {cache_key}")
            return result
        
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper
    
    return decorator


_cache_service: Optional[CacheService] = None
_cache_service_lock = threading.Lock()


def get_cache_service() -> CacheService:
    """获取缓存服务实例（线程安全）"""
    global _cache_service
    if _cache_service is None:
        with _cache_service_lock:
            if _cache_service is None:
                config = get_config()
                _cache_service = CacheService(
                    cache_type=config.cache.type,
                    max_size=config.cache.max_size,
                    default_ttl=config.cache.default_ttl,
                    redis_url=config.cache.redis_url
                )
    return _cache_service
