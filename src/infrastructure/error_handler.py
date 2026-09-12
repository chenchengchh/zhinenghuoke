"""
统一错误处理模块

提供统一的错误处理装饰器和异常类体系

功能:
1. 统一的错误处理装饰器
2. 结构化的异常类体系
3. 自动日志记录
4. 错误码管理
"""

import functools
import traceback
from typing import Optional, Callable, Any, Dict
from datetime import datetime
from loguru import logger


class SystemException(Exception):
    """系统基础异常类"""
    
    def __init__(
        self,
        message: str,
        error_code: str = "SYSTEM_ERROR",
        status_code: int = 500,
        detail: Optional[Dict] = None
    ):
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.detail = detail or {}
        self.timestamp = datetime.now().isoformat()
        
        super().__init__(self.message)
    
    def to_dict(self, include_traceback: bool = False) -> Dict:
        """转换为字典格式"""
        result = {
            "error_code": self.error_code,
            "message": self.message,
            "status_code": self.status_code,
            "detail": self.detail,
            "timestamp": self.timestamp
        }
        if include_traceback:
            result["traceback"] = traceback.format_exc()
        return result


class RPAException(SystemException):
    """RPA 相关异常"""
    
    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            error_code=kwargs.get("error_code", "RPA_ERROR"),
            status_code=kwargs.get("status_code", 500),
            detail=kwargs.get("detail")
        )


class LLMException(SystemException):
    """LLM 相关异常"""
    
    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            error_code=kwargs.get("error_code", "LLM_ERROR"),
            status_code=kwargs.get("status_code", 500),
            detail=kwargs.get("detail")
        )


class RAGException(SystemException):
    """RAG 相关异常"""
    
    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            error_code=kwargs.get("error_code", "RAG_ERROR"),
            status_code=kwargs.get("status_code", 500),
            detail=kwargs.get("detail")
        )


class IntentException(SystemException):
    """意图识别相关异常"""
    
    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            error_code=kwargs.get("error_code", "INTENT_ERROR"),
            status_code=kwargs.get("status_code", 500),
            detail=kwargs.get("detail")
        )


class DatabaseException(SystemException):
    """数据库相关异常"""
    
    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            error_code=kwargs.get("error_code", "DATABASE_ERROR"),
            status_code=kwargs.get("status_code", 500),
            detail=kwargs.get("detail")
        )


class ConfigException(SystemException):
    """配置相关异常"""
    
    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            error_code=kwargs.get("error_code", "CONFIG_ERROR"),
            status_code=kwargs.get("status_code", 500),
            detail=kwargs.get("detail")
        )


def safe_execute(
    func: Optional[Callable] = None,
    *,
    error_message: str = "操作失败",
    default_value: Any = None,
    reraise: bool = False,
    log_error: bool = True,
    log_level: str = "ERROR",
    on_error: Optional[Callable] = None,
    exception_class: type = SystemException
):
    """
    统一错误处理装饰器
    
    Args:
        func: 被装饰的函数
        error_message: 错误消息
        default_value: 默认返回值
        reraise: 是否重新抛出异常
        log_error: 是否记录日志
        log_level: 日志级别
        on_error: 错误处理回调
        exception_class: 异常类
        
    Returns:
        装饰后的函数
    
    Example:
        @safe_execute(error_message="数据库操作失败", default_value=[])
        def get_data():
            ...
    """
    def decorator(f: Callable):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            
            except SystemException as e:
                # 已知的系统异常
                if log_error:
                    getattr(logger, log_level.lower())(
                        f"{error_message}: [{e.error_code}] {e.message}"
                    )
                
                if on_error:
                    on_error(e)
                
                if reraise:
                    raise
                return default_value
            
            except Exception as e:
                # 未知异常
                if log_error:
                    getattr(logger, log_level.lower())(
                        f"{error_message}: {type(e).__name__} - {str(e)}\n"
                        f"{traceback.format_exc()}"
                    )
                
                # 转换为系统异常
                system_exception = exception_class(
                    message=str(e),
                    error_code="INTERNAL_ERROR",
                    detail={"original_exception": type(e).__name__}
                )
                
                if on_error:
                    on_error(system_exception)
                
                if reraise:
                    raise system_exception
                return default_value
        
        return wrapper
    
    if func is not None:
        return decorator(func)
    return decorator


def api_response(func: Callable):
    """
    API 响应装饰器
    
    统一 API 响应格式，自动处理异常
    
    Example:
        @api_response
        async def get_data(request):
            ...
    """
    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        try:
            result = await func(*args, **kwargs)
            return {
                "success": True,
                "data": result,
                "timestamp": datetime.now().isoformat()
            }
        except SystemException as e:
            logger.warning(f"API 业务异常：{e.message}")
            return {
                "success": False,
                "error": e.to_dict(),
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"API 系统异常：{e}\n{traceback.format_exc()}")
            import os
            is_debug = os.getenv("ENVIRONMENT", "development") == "development"
            return {
                "success": False,
                "error": {
                    "error_code": "SYSTEM_ERROR",
                    "message": "系统内部错误",
                    "detail": str(e) if is_debug else "内部错误详情仅在开发环境显示"
                },
                "timestamp": datetime.now().isoformat()
            }
    
    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
            return {
                "success": True,
                "data": result,
                "timestamp": datetime.now().isoformat()
            }
        except SystemException as e:
            logger.warning(f"API 业务异常：{e.message}")
            return {
                "success": False,
                "error": e.to_dict(),
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"API 系统异常：{e}\n{traceback.format_exc()}")
            import os
            is_debug = os.getenv("ENVIRONMENT", "development") == "development"
            return {
                "success": False,
                "error": {
                    "error_code": "SYSTEM_ERROR",
                    "message": "系统内部错误",
                    "detail": str(e) if is_debug else "内部错误详情仅在开发环境显示"
                },
                "timestamp": datetime.now().isoformat()
            }
    
    # 判断原函数是否为异步函数
    if functools.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper


def retry_on_failure(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,)
):
    """
    失败重试装饰器
    
    Args:
        max_retries: 最大重试次数
        delay: 初始延迟 (秒)
        backoff: 延迟倍增系数
        exceptions: 需要重试的异常类型
    
    Example:
        @retry_on_failure(max_retries=3, delay=1.0)
        def unstable_operation():
            ...
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                
                except exceptions as e:
                    last_exception = e
                    logger.warning(
                        f"操作失败 (尝试 {attempt+1}/{max_retries}): {e}"
                    )
                    
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(current_delay)
                        current_delay *= backoff
            
            # 所有重试失败
            logger.error(f"所有重试失败：{last_exception}")
            raise last_exception
        
        return wrapper
    return decorator


class ErrorCodeManager:
    """
    错误码管理器
    
    统一管理所有错误码
    """
    
    # 通用错误码
    SUCCESS = "SUCCESS"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    INVALID_PARAM = "INVALID_PARAM"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    
    # RPA 错误码
    RPA_ERROR = "RPA_ERROR"
    RPA_BROWSER_ERROR = "RPA_BROWSER_ERROR"
    RPA_ELEMENT_NOT_FOUND = "RPA_ELEMENT_NOT_FOUND"
    RPA_TIMEOUT = "RPA_TIMEOUT"
    
    # LLM 错误码
    LLM_ERROR = "LLM_ERROR"
    LLM_API_ERROR = "LLM_API_ERROR"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_RATE_LIMIT = "LLM_RATE_LIMIT"
    
    # RAG 错误码
    RAG_ERROR = "RAG_ERROR"
    RAG_RETRIEVAL_ERROR = "RAG_RETRIEVAL_ERROR"
    RAG_RERANK_ERROR = "RAG_RERANK_ERROR"
    
    # 意图识别错误码
    INTENT_ERROR = "INTENT_ERROR"
    INTENT_LOW_CONFIDENCE = "INTENT_LOW_CONFIDENCE"
    
    # 数据库错误码
    DATABASE_ERROR = "DATABASE_ERROR"
    DATABASE_CONNECTION_ERROR = "DATABASE_CONNECTION_ERROR"
    DATABASE_QUERY_ERROR = "DATABASE_QUERY_ERROR"
    
    _descriptions: Dict[str, str] = {}
    
    @classmethod
    def register(cls, code: str, description: str):
        """注册错误码描述"""
        cls._descriptions[code] = description
    
    @classmethod
    def get_description(cls, code: str) -> str:
        """获取错误码描述"""
        return cls._descriptions.get(code, "")
    
    @classmethod
    def get_all_codes(cls) -> Dict[str, str]:
        """获取所有错误码"""
        return cls._descriptions.copy()


# 注册错误码描述
ErrorCodeManager.register(ErrorCodeManager.SUCCESS, "操作成功")
ErrorCodeManager.register(ErrorCodeManager.SYSTEM_ERROR, "系统内部错误")
ErrorCodeManager.register(ErrorCodeManager.INVALID_PARAM, "参数无效")
ErrorCodeManager.register(ErrorCodeManager.NOT_FOUND, "资源不存在")
ErrorCodeManager.register(ErrorCodeManager.UNAUTHORIZED, "未授权访问")
ErrorCodeManager.register(ErrorCodeManager.RPA_ERROR, "RPA 操作失败")
ErrorCodeManager.register(ErrorCodeManager.RPA_ELEMENT_NOT_FOUND, "未找到页面元素")
ErrorCodeManager.register(ErrorCodeManager.LLM_ERROR, "LLM 调用失败")
ErrorCodeManager.register(ErrorCodeManager.LLM_TIMEOUT, "LLM 调用超时")
ErrorCodeManager.register(ErrorCodeManager.RAG_ERROR, "RAG 检索失败")
