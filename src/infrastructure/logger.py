# -*- coding: utf-8 -*-
"""
结构化日志模块
提供企业级应用的日志记录功能
"""
import os
import sys
import json
import logging
import traceback
from datetime import datetime
from typing import Any, Dict, Optional
from functools import lru_cache
from contextvars import ContextVar
from loguru import logger

request_context: ContextVar[Dict[str, Any]] = ContextVar("request_context", default=None)


def _is_writable_text_stream(stream) -> bool:
    if stream is None:
        return False
    try:
        stream.write("")
        stream.flush()
        return True
    except Exception:
        return False


def get_request_context() -> Dict[str, Any]:
    """获取请求上下文（避免可变默认值共享问题）"""
    ctx = request_context.get(None)
    return ctx if ctx is not None else {}


class StructuredLogger:
    """
    结构化日志记录器
    
    提供统一的日志记录接口，支持：
    - 结构化JSON格式输出
    - 请求追踪ID
    - 敏感信息脱敏
    - 日志级别控制
    - 文件轮转
    """
    
    def __init__(
        self,
        name: str,
        level: str = "INFO",
        log_dir: str = "logs",
        json_format: bool = True,
        rotation: str = "10 MB",
        retention: str = "7 days"
    ):
        self.name = name
        self.level = level.upper()
        self.log_dir = log_dir
        self.json_format = json_format
        self.rotation = rotation
        self.retention = retention
        
        self._setup_logger()
    
    def _setup_logger(self):
        """配置日志记录器"""
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
        
        logger.remove()
        
        console_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        )
        
        if _is_writable_text_stream(sys.stdout):
            logger.add(
                sys.stdout,
                format=console_format,
                level=self.level,
                colorize=True,
                enqueue=True
            )
        
        logger.add(
            os.path.join(self.log_dir, "app_{time:YYYY-MM-DD}.log"),
            format=lambda record: self._escaped_json_format(record),
            level=self.level,
            rotation=self.rotation,
            retention=self.retention,
            compression="gz",
            serialize=False,
            enqueue=True
        )
        
        logger.add(
            os.path.join(self.log_dir, "error_{time:YYYY-MM-DD}.log"),
            format=lambda record: self._escaped_json_format(record),
            level="ERROR",
            rotation=self.rotation,
            retention=self.retention,
            compression="gz",
            serialize=False,
            enqueue=True
        )
        
        self._logger = logger.bind(name=self.name)
    
    def _json_format(self, record) -> str:
        """生成JSON格式日志"""
        log_data = {
            "timestamp": record["time"].isoformat(),
            "level": record["level"].name,
            "logger": self.name,
            "message": record["message"],
            "module": record["module"],
            "function": record["function"],
            "line": record["line"]
        }
        
        ctx = get_request_context()
        if ctx:
            log_data["request_id"] = ctx.get("request_id", "")
            log_data["trace_id"] = ctx.get("trace_id", "")
            log_data["span_id"] = ctx.get("span_id", "")
            log_data["root_span_id"] = ctx.get("root_span_id", "")
            log_data["current_span_id"] = ctx.get("current_span_id", ctx.get("span_id", ""))
            span_stack = list(ctx.get("span_stack", []) or [])
            log_data["parent_span_id"] = span_stack[-2] if len(span_stack) >= 2 else ""
            log_data["user_id"] = ctx.get("user_id", "")
            log_data["enterprise_id"] = ctx.get("enterprise_id", "")
        
        if record["extra"]:
            log_data["extra"] = self._sanitize(record["extra"])
        
        if record["exception"]:
            log_data["exception"] = {
                "type": record["exception"].type.__name__ if record["exception"].type else None,
                "value": str(record["exception"].value) if record["exception"].value else None,
                "traceback": record["exception"].traceback if record["exception"].traceback else None
            }
        
        return json.dumps(log_data, ensure_ascii=False, default=str) + "\n"

    def _escaped_json_format(self, record) -> str:
        """将已渲染 JSON 转成安全模板，避免 Loguru 把 JSON 花括号当占位符。"""
        return self._json_format(record).replace("{", "{{").replace("}", "}}")
    
    def _sanitize(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """脱敏敏感信息"""
        sensitive_keys = {
            "password", "passwd", "pwd", "secret", "token", "api_key",
            "access_token", "refresh_token", "credential", "private_key",
            "phone", "mobile", "email", "id_card", "bank_card"
        }
        
        result = {}
        for key, value in data.items():
            lower_key = key.lower()
            if any(sensitive in lower_key for sensitive in sensitive_keys):
                result[key] = "***REDACTED***"
            elif isinstance(value, dict):
                result[key] = self._sanitize(value)
            elif isinstance(value, str) and len(value) > 100:
                result[key] = value[:100] + "..."
            else:
                result[key] = value
        return result
    
    def _get_caller_info(self) -> Dict[str, Any]:
        """获取调用者信息"""
        import inspect
        frame = inspect.currentframe()
        try:
            caller_frame = frame.f_back.f_back.f_back
            return {
                "caller_module": caller_frame.f_globals.get("__name__", ""),
                "caller_function": caller_frame.f_code.co_name,
                "caller_line": caller_frame.f_lineno
            }
        finally:
            del frame
    
    def debug(self, message: str, **kwargs):
        """记录DEBUG级别日志"""
        self._logger.bind(**kwargs).debug(message)
    
    def info(self, message: str, **kwargs):
        """记录INFO级别日志"""
        self._logger.bind(**kwargs).info(message)
    
    def warning(self, message: str, **kwargs):
        """记录WARNING级别日志"""
        self._logger.bind(**kwargs).warning(message)
    
    def error(self, message: str, exc: Optional[Exception] = None, **kwargs):
        """记录ERROR级别日志"""
        if exc:
            self._logger.bind(**kwargs).exception(message)
        else:
            self._logger.bind(**kwargs).error(message)
    
    def critical(self, message: str, exc: Optional[Exception] = None, **kwargs):
        """记录CRITICAL级别日志"""
        if exc:
            self._logger.bind(**kwargs).exception(message)
        else:
            self._logger.bind(**kwargs).critical(message)
    
    def log_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        **kwargs
    ):
        """记录HTTP请求日志"""
        log_data = {
            "type": "http_request",
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 2),
            "user_id": user_id,
            "enterprise_id": enterprise_id,
            **kwargs
        }
        
        if status_code >= 500:
            self.error(f"HTTP {method} {path} - {status_code}", **log_data)
        elif status_code >= 400:
            self.warning(f"HTTP {method} {path} - {status_code}", **log_data)
        else:
            self.info(f"HTTP {method} {path} - {status_code}", **log_data)
    
    def log_business(
        self,
        action: str,
        entity: str,
        entity_id: Optional[str] = None,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        **kwargs
    ):
        """记录业务操作日志"""
        log_data = {
            "type": "business_operation",
            "action": action,
            "entity": entity,
            "entity_id": entity_id,
            "user_id": user_id,
            "enterprise_id": enterprise_id,
            "details": details or {},
            **kwargs
        }
        self.info(f"业务操作: {action} - {entity}", **log_data)
    
    def log_security(
        self,
        event: str,
        user_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        level: str = "warning",
        **kwargs
    ):
        """记录安全事件日志"""
        log_data = {
            "type": "security_event",
            "event": event,
            "user_id": user_id,
            "ip_address": ip_address,
            "details": details or {},
            **kwargs
        }
        
        if level == "critical":
            self.critical(f"安全事件: {event}", **log_data)
        elif level == "error":
            self.error(f"安全事件: {event}", **log_data)
        else:
            self.warning(f"安全事件: {event}", **log_data)
    
    def log_performance(
        self,
        operation: str,
        duration_ms: float,
        threshold_ms: float = 1000,
        details: Optional[Dict[str, Any]] = None,
        **kwargs
    ):
        """记录性能日志"""
        log_data = {
            "type": "performance",
            "operation": operation,
            "duration_ms": round(duration_ms, 2),
            "threshold_ms": threshold_ms,
            "slow": duration_ms > threshold_ms,
            "details": details or {},
            **kwargs
        }
        
        if duration_ms > threshold_ms:
            self.warning(f"慢操作: {operation} - {duration_ms:.2f}ms", **log_data)
        else:
            self.debug(f"性能: {operation} - {duration_ms:.2f}ms", **log_data)


@lru_cache(maxsize=32)
def get_logger(name: str, level: str = "INFO") -> StructuredLogger:
    """
    获取日志记录器实例
    
    使用缓存避免重复创建
    """
    return StructuredLogger(name=name, level=level)


def setup_logging(
    level: str = "INFO",
    log_dir: str = "logs",
    json_format: bool = True
) -> None:
    """
    全局日志配置
    
    在应用启动时调用
    """
    global _default_logger
    
    _default_logger = StructuredLogger(
        name="app",
        level=level,
        log_dir=log_dir,
        json_format=json_format
    )
    
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


_default_logger: Optional[StructuredLogger] = None


def get_default_logger() -> StructuredLogger:
    """获取默认日志记录器"""
    global _default_logger
    if _default_logger is None:
        _default_logger = get_logger("app")
    return _default_logger
