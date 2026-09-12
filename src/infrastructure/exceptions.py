# -*- coding: utf-8 -*-
"""
统一异常处理模块
提供企业级应用的异常处理机制
"""
from enum import Enum
from typing import Any, Dict, Optional
from datetime import datetime


class ErrorCode(Enum):
    """错误码枚举"""
    SUCCESS = (0, "操作成功")
    
    BAD_REQUEST = (400, "请求参数错误")
    UNAUTHORIZED = (401, "未授权访问")
    FORBIDDEN = (403, "禁止访问")
    NOT_FOUND = (404, "资源不存在")
    METHOD_NOT_ALLOWED = (405, "方法不允许")
    CONFLICT = (409, "资源冲突")
    VALIDATION_ERROR = (422, "数据验证失败")
    RATE_LIMIT_EXCEEDED = (429, "请求频率超限")
    
    INTERNAL_ERROR = (500, "服务器内部错误")
    DATABASE_ERROR = (501, "数据库操作失败")
    EXTERNAL_SERVICE_ERROR = (502, "外部服务调用失败")
    SERVICE_UNAVAILABLE = (503, "服务暂时不可用")
    GATEWAY_TIMEOUT = (504, "网关超时")
    
    KNOWLEDGE_NOT_FOUND = (1001, "知识库内容不存在")
    EMBEDDING_ERROR = (1002, "向量嵌入失败")
    VECTOR_SEARCH_ERROR = (1003, "向量检索失败")
    LLM_SERVICE_ERROR = (1004, "LLM服务调用失败")
    RAG_PIPELINE_ERROR = (1005, "RAG管道处理失败")
    
    SESSION_EXPIRED = (2001, "会话已过期")
    TOKEN_INVALID = (2002, "令牌无效")
    PERMISSION_DENIED = (2003, "权限不足")
    
    DATA_SYNC_ERROR = (3001, "数据同步失败")
    CACHE_ERROR = (3002, "缓存操作失败")
    FILE_UPLOAD_ERROR = (3003, "文件上传失败")
    
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


class AppException(Exception):
    """
    应用基础异常类
    
    所有业务异常都应继承此类
    """
    
    def __init__(
        self,
        error_code: ErrorCode,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[Exception] = None
    ):
        self.error_code = error_code
        self.message = message or error_code.message
        self.details = details or {}
        self.cause = cause
        self.timestamp = datetime.now().isoformat()
        super().__init__(self.message)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        result = {
            "code": self.error_code.code,
            "message": self.message,
            "timestamp": self.timestamp
        }
        if self.details:
            result["details"] = self.details
        return result
    
    def __str__(self) -> str:
        return f"[{self.error_code.code}] {self.message}"


class ValidationError(AppException):
    """数据验证异常"""
    
    def __init__(
        self,
        message: str = "数据验证失败",
        field: Optional[str] = None,
        value: Optional[Any] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        error_details = details or {}
        if field:
            error_details["field"] = field
        if value is not None:
            error_details["value"] = str(value)[:100]
        super().__init__(ErrorCode.VALIDATION_ERROR, message, error_details)


class AuthenticationError(AppException):
    """认证异常"""
    
    def __init__(
        self,
        message: str = "认证失败",
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(ErrorCode.UNAUTHORIZED, message, details)


class AuthorizationError(AppException):
    """授权异常"""
    
    def __init__(
        self,
        message: str = "权限不足",
        resource: Optional[str] = None,
        action: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        error_details = details or {}
        if resource:
            error_details["resource"] = resource
        if action:
            error_details["action"] = action
        super().__init__(ErrorCode.FORBIDDEN, message, error_details)


class NotFoundError(AppException):
    """资源不存在异常"""
    
    def __init__(
        self,
        resource: str = "资源",
        resource_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        message = f"{resource}不存在"
        error_details = details or {}
        if resource_id:
            error_details["resource_id"] = resource_id
        super().__init__(ErrorCode.NOT_FOUND, message, error_details)


class ConflictError(AppException):
    """资源冲突异常"""
    
    def __init__(
        self,
        message: str = "资源冲突",
        resource: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        error_details = details or {}
        if resource:
            error_details["resource"] = resource
        super().__init__(ErrorCode.CONFLICT, message, error_details)


class RateLimitError(AppException):
    """请求频率限制异常"""
    
    def __init__(
        self,
        message: str = "请求频率超限",
        retry_after: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        error_details = details or {}
        if retry_after:
            error_details["retry_after"] = retry_after
        super().__init__(ErrorCode.RATE_LIMIT_EXCEEDED, message, error_details)


class ServiceUnavailableError(AppException):
    """服务不可用异常"""
    
    def __init__(
        self,
        service: str = "服务",
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        error_message = message or f"{service}暂时不可用"
        error_details = details or {}
        error_details["service"] = service
        super().__init__(ErrorCode.SERVICE_UNAVAILABLE, error_message, error_details)


class DatabaseError(AppException):
    """数据库操作异常"""
    
    def __init__(
        self,
        message: str = "数据库操作失败",
        operation: Optional[str] = None,
        table: Optional[str] = None,
        cause: Optional[Exception] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        error_details = details or {}
        if operation:
            error_details["operation"] = operation
        if table:
            error_details["table"] = table
        super().__init__(ErrorCode.DATABASE_ERROR, message, error_details, cause)


class ExternalServiceError(AppException):
    """外部服务调用异常"""
    
    def __init__(
        self,
        service: str,
        message: Optional[str] = None,
        status_code: Optional[int] = None,
        cause: Optional[Exception] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        error_message = message or f"{service}服务调用失败"
        error_details = details or {}
        error_details["service"] = service
        if status_code:
            error_details["status_code"] = status_code
        super().__init__(ErrorCode.EXTERNAL_SERVICE_ERROR, error_message, error_details, cause)


def handle_exception(exc: Exception) -> AppException:
    """
    统一异常处理函数
    
    将任意异常转换为AppException
    """
    if isinstance(exc, AppException):
        return exc
    
    if isinstance(exc, ValueError):
        return ValidationError(str(exc))
    
    if isinstance(exc, KeyError):
        return ValidationError(f"缺少必要参数: {exc}", field=str(exc))
    
    if isinstance(exc, PermissionError):
        return AuthorizationError("权限不足")
    
    if isinstance(exc, FileNotFoundError):
        return NotFoundError("文件", str(exc))
    
    if isinstance(exc, TimeoutError):
        return ServiceUnavailableError(message="操作超时")
    
    return AppException(
        ErrorCode.INTERNAL_ERROR,
        "服务器内部错误",
        {"original_error": str(exc)},
        cause=exc
    )
