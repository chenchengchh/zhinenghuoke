# -*- coding: utf-8 -*-
"""
统一响应模型模块
提供标准化的API响应格式
"""
from typing import Any, Dict, Generic, List, Optional, TypeVar
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """
    统一API响应模型
    
    所有API响应都应使用此模型
    """
    
    code: int = Field(default=0, description="状态码，0表示成功")
    message: str = Field(default="操作成功", description="响应消息")
    data: Optional[T] = Field(default=None, description="响应数据")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="响应时间戳"
    )
    request_id: Optional[str] = Field(default=None, description="请求追踪ID")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": 0,
                "message": "操作成功",
                "data": None,
                "timestamp": "2024-01-01T12:00:00",
                "request_id": "req_123456",
            }
        }
    )


class SuccessResponse(ApiResponse[T]):
    """成功响应"""
    
    def __init__(
        self,
        data: Optional[T] = None,
        message: str = "操作成功",
        request_id: Optional[str] = None
    ):
        super().__init__(
            code=0,
            message=message,
            data=data,
            request_id=request_id
        )
    
    @classmethod
    def ok(cls, data: Optional[T] = None, message: str = "操作成功") -> "SuccessResponse[T]":
        """创建成功响应"""
        return cls(data=data, message=message)
    
    @classmethod
    def created(cls, data: Optional[T] = None, message: str = "创建成功") -> "SuccessResponse[T]":
        """创建资源创建成功响应"""
        return cls(data=data, message=message)


class ErrorResponse(ApiResponse):
    """错误响应"""
    
    def __init__(
        self,
        code: int,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None
    ):
        super().__init__(
            code=code,
            message=message,
            data={"details": details} if details else None,
            request_id=request_id
        )
    
    @classmethod
    def bad_request(cls, message: str = "请求参数错误", details: Optional[Dict[str, Any]] = None) -> "ErrorResponse":
        """创建参数错误响应"""
        return cls(code=400, message=message, details=details)
    
    @classmethod
    def unauthorized(cls, message: str = "未授权访问") -> "ErrorResponse":
        """创建未授权响应"""
        return cls(code=401, message=message)
    
    @classmethod
    def forbidden(cls, message: str = "禁止访问") -> "ErrorResponse":
        """创建禁止访问响应"""
        return cls(code=403, message=message)
    
    @classmethod
    def not_found(cls, message: str = "资源不存在") -> "ErrorResponse":
        """创建资源不存在响应"""
        return cls(code=404, message=message)
    
    @classmethod
    def internal_error(cls, message: str = "服务器内部错误", details: Optional[Dict[str, Any]] = None) -> "ErrorResponse":
        """创建服务器错误响应"""
        return cls(code=500, message=message, details=details)


class PageInfo(BaseModel):
    """分页信息"""
    
    page: int = Field(default=1, ge=1, description="当前页码")
    page_size: int = Field(default=20, ge=1, le=500, description="每页数量")
    total: int = Field(default=0, ge=0, description="总记录数")
    total_pages: int = Field(default=0, ge=0, description="总页数")
    has_next: bool = Field(default=False, description="是否有下一页")
    has_prev: bool = Field(default=False, description="是否有上一页")


class PaginatedResponse(ApiResponse[List[T]]):
    """分页响应"""
    
    page_info: PageInfo = Field(default_factory=PageInfo, description="分页信息")
    
    def __init__(
        self,
        data: List[T],
        page: int = 1,
        page_size: int = 20,
        total: int = 0,
        message: str = "操作成功",
        request_id: Optional[str] = None
    ):
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        page_info = PageInfo(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_prev=page > 1
        )
        super().__init__(
            code=0,
            message=message,
            data=data,
            request_id=request_id
        )
        self.page_info = page_info
    
    @classmethod
    def create(
        cls,
        items: List[T],
        page: int = 1,
        page_size: int = 20,
        total: int = 0
    ) -> "PaginatedResponse[T]":
        """创建分页响应"""
        return cls(data=items, page=page, page_size=page_size, total=total)


class BulkOperationResult(BaseModel):
    """批量操作结果"""
    
    total: int = Field(default=0, description="总数量")
    success: int = Field(default=0, description="成功数量")
    failed: int = Field(default=0, description="失败数量")
    errors: List[Dict[str, Any]] = Field(default_factory=list, description="错误详情")


class BulkOperationResponse(ApiResponse[BulkOperationResult]):
    """批量操作响应"""
    
    def __init__(
        self,
        total: int,
        success: int,
        failed: int,
        errors: Optional[List[Dict[str, Any]]] = None,
        message: str = "批量操作完成",
        request_id: Optional[str] = None
    ):
        result = BulkOperationResult(
            total=total,
            success=success,
            failed=failed,
            errors=errors or []
        )
        super().__init__(
            code=0 if failed == 0 else 207,
            message=message,
            data=result,
            request_id=request_id
        )
