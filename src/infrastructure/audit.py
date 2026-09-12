# -*- coding: utf-8 -*-
"""
审计日志模块
提供企业级应用的审计追踪功能
"""
import os
import json
import time
import uuid
import threading
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field, asdict

from src.infrastructure.logger import get_logger

logger = get_logger("audit")


class AuditAction(str, Enum):
    """审计动作类型"""
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    LOGOUT = "logout"
    EXPORT = "export"
    IMPORT = "import"
    CONFIG_CHANGE = "config_change"
    PERMISSION_CHANGE = "permission_change"
    API_ACCESS = "api_access"
    DATA_ACCESS = "data_access"
    SECURITY_EVENT = "security_event"


class AuditResult(str, Enum):
    """审计结果"""
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


@dataclass
class AuditLog:
    """审计日志记录"""
    audit_id: str = field(default_factory=lambda: f"audit_{uuid.uuid4().hex[:12]}")
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    action: str = ""
    resource_type: str = ""
    resource_id: str = ""
    user_id: str = ""
    enterprise_id: str = ""
    ip_address: str = ""
    user_agent: str = ""
    result: str = AuditResult.SUCCESS.value
    details: Dict[str, Any] = field(default_factory=dict)
    old_values: Dict[str, Any] = field(default_factory=dict)
    new_values: Dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    duration_ms: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)
    
    def to_json(self) -> str:
        """转换为JSON字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


class AuditService:
    """
    审计服务
    
    提供完整的审计日志记录功能
    """
    
    def __init__(self, log_dir: str = "logs/audit"):
        self.log_dir = log_dir
        self._ensure_log_dir()
        self._buffer: List[AuditLog] = []
        self._buffer_lock = threading.Lock()
        self._buffer_size = 100
        self._last_flush = time.time()
        self._flush_interval = 60
    
    def _ensure_log_dir(self):
        """确保日志目录存在"""
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
    
    def _get_log_file(self) -> str:
        """获取当前日志文件路径"""
        date_str = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.log_dir, f"audit_{date_str}.jsonl")
    
    def _write_log(self, audit_log: AuditLog):
        """写入审计日志"""
        log_file = self._get_log_file()
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(audit_log.to_json() + "\n")
        except Exception as e:
            logger.error(f"写入审计日志失败: {e}")
    
    def _flush_buffer(self):
        """刷新缓冲区"""
        with self._buffer_lock:
            if not self._buffer:
                return
            buffer_copy = self._buffer[:]
            self._buffer.clear()
            self._last_flush = time.time()

        for audit_log in buffer_copy:
            self._write_log(audit_log)
    
    def log(
        self,
        action: AuditAction,
        resource_type: str,
        resource_id: str = "",
        user_id: str = "",
        enterprise_id: str = "",
        ip_address: str = "",
        user_agent: str = "",
        result: AuditResult = AuditResult.SUCCESS,
        details: Optional[Dict[str, Any]] = None,
        old_values: Optional[Dict[str, Any]] = None,
        new_values: Optional[Dict[str, Any]] = None,
        request_id: str = "",
        duration_ms: float = 0.0
    ) -> AuditLog:
        """
        记录审计日志
        
        Args:
            action: 操作类型
            resource_type: 资源类型
            resource_id: 资源ID
            user_id: 用户ID
            enterprise_id: 企业ID
            ip_address: IP地址
            user_agent: 用户代理
            result: 操作结果
            details: 详细信息
            old_values: 修改前的值
            new_values: 修改后的值
            request_id: 请求ID
            duration_ms: 操作耗时(毫秒)
            
        Returns:
            审计日志记录
        """
        audit_log = AuditLog(
            action=action.value,
            resource_type=resource_type,
            resource_id=resource_id,
            user_id=user_id,
            enterprise_id=enterprise_id,
            ip_address=ip_address,
            user_agent=user_agent,
            result=result.value,
            details=details or {},
            old_values=old_values or {},
            new_values=new_values or {},
            request_id=request_id,
            duration_ms=duration_ms
        )
        
        with self._buffer_lock:
            self._buffer.append(audit_log)
            should_flush = len(self._buffer) >= self._buffer_size or time.time() - self._last_flush >= self._flush_interval

        if should_flush:
            self._flush_buffer()
        
        logger.debug(f"审计日志: {action.value} - {resource_type}:{resource_id}")
        
        return audit_log
    
    def log_create(
        self,
        resource_type: str,
        resource_id: str,
        new_values: Dict[str, Any],
        **kwargs
    ) -> AuditLog:
        """记录创建操作"""
        return self.log(
            action=AuditAction.CREATE,
            resource_type=resource_type,
            resource_id=resource_id,
            new_values=new_values,
            **kwargs
        )
    
    def log_update(
        self,
        resource_type: str,
        resource_id: str,
        old_values: Dict[str, Any],
        new_values: Dict[str, Any],
        **kwargs
    ) -> AuditLog:
        """记录更新操作"""
        return self.log(
            action=AuditAction.UPDATE,
            resource_type=resource_type,
            resource_id=resource_id,
            old_values=old_values,
            new_values=new_values,
            **kwargs
        )
    
    def log_delete(
        self,
        resource_type: str,
        resource_id: str,
        old_values: Dict[str, Any],
        **kwargs
    ) -> AuditLog:
        """记录删除操作"""
        return self.log(
            action=AuditAction.DELETE,
            resource_type=resource_type,
            resource_id=resource_id,
            old_values=old_values,
            **kwargs
        )
    
    def log_login(
        self,
        user_id: str,
        ip_address: str,
        user_agent: str,
        result: AuditResult = AuditResult.SUCCESS,
        details: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> AuditLog:
        """记录登录操作"""
        return self.log(
            action=AuditAction.LOGIN,
            resource_type="user_session",
            user_id=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
            result=result,
            details=details,
            **kwargs
        )
    
    def log_api_access(
        self,
        method: str,
        path: str,
        status_code: int,
        user_id: str = "",
        ip_address: str = "",
        duration_ms: float = 0.0,
        **kwargs
    ) -> AuditLog:
        """记录API访问"""
        return self.log(
            action=AuditAction.API_ACCESS,
            resource_type="api_endpoint",
            resource_id=f"{method}:{path}",
            user_id=user_id,
            ip_address=ip_address,
            result=AuditResult.SUCCESS if status_code < 400 else AuditResult.FAILURE,
            details={"method": method, "path": path, "status_code": status_code},
            duration_ms=duration_ms,
            **kwargs
        )
    
    def log_security_event(
        self,
        event: str,
        user_id: str = "",
        ip_address: str = "",
        details: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> AuditLog:
        """记录安全事件"""
        return self.log(
            action=AuditAction.SECURITY_EVENT,
            resource_type="security",
            user_id=user_id,
            ip_address=ip_address,
            result=AuditResult.FAILURE,
            details={"event": event, **(details or {})},
            **kwargs
        )
    
    def query_logs(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        action: Optional[AuditAction] = None,
        resource_type: Optional[str] = None,
        result: Optional[AuditResult] = None,
        limit: int = 100
    ) -> List[AuditLog]:
        """
        查询审计日志
        
        Args:
            start_time: 开始时间
            end_time: 结束时间
            user_id: 用户ID
            enterprise_id: 企业ID
            action: 操作类型
            resource_type: 资源类型
            result: 操作结果
            limit: 返回数量限制
            
        Returns:
            审计日志列表
        """
        self._flush_buffer()
        
        logs = []
        log_files = sorted(
            [f for f in os.listdir(self.log_dir) if f.startswith("audit_") and f.endswith(".jsonl")],
            reverse=True
        )
        
        for log_file in log_files:
            file_path = os.path.join(self.log_dir, log_file)
            
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        data = json.loads(line.strip())
                        audit_log = AuditLog(**data)
                        
                        if start_time and datetime.fromisoformat(audit_log.timestamp) < start_time:
                            continue
                        if end_time and datetime.fromisoformat(audit_log.timestamp) > end_time:
                            continue
                        if user_id and audit_log.user_id != user_id:
                            continue
                        if enterprise_id and audit_log.enterprise_id != enterprise_id:
                            continue
                        if action and audit_log.action != action.value:
                            continue
                        if resource_type and audit_log.resource_type != resource_type:
                            continue
                        if result and audit_log.result != result.value:
                            continue
                        
                        logs.append(audit_log)
                        
                        if len(logs) >= limit:
                            return logs
                            
                    except Exception as e:
                        logger.warning(f"解析审计日志失败: {e}")
        
        return logs
    
    def flush(self):
        """手动刷新缓冲区"""
        self._flush_buffer()


_audit_service: Optional[AuditService] = None
_audit_service_lock = threading.Lock()


def get_audit_service() -> AuditService:
    """获取审计服务实例（线程安全）"""
    global _audit_service
    if _audit_service is None:
        with _audit_service_lock:
            if _audit_service is None:
                _audit_service = AuditService()
    return _audit_service


def audit_decorator(
    action: AuditAction,
    resource_type: str,
    get_resource_id: Optional[Callable] = None
):
    """
    审计装饰器
    
    自动记录函数调用的审计日志
    """
    def decorator(func):
        from functools import wraps
        
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            result_val = None
            error = None
            
            try:
                result_val = await func(*args, **kwargs)
                audit_result = AuditResult.SUCCESS
            except Exception as e:
                error = str(e)
                audit_result = AuditResult.FAILURE
                raise
            finally:
                duration_ms = (time.time() - start_time) * 1000
                resource_id = get_resource_id(result_val, *args, **kwargs) if get_resource_id else ""
                
                get_audit_service().log(
                    action=action,
                    resource_type=resource_type,
                    resource_id=str(resource_id),
                    result=audit_result,
                    details={"error": error} if error else {},
                    duration_ms=duration_ms
                )
            
            return result_val
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = time.time()
            result_val = None
            error = None
            
            try:
                result_val = func(*args, **kwargs)
                audit_result = AuditResult.SUCCESS
            except Exception as e:
                error = str(e)
                audit_result = AuditResult.FAILURE
                raise
            finally:
                duration_ms = (time.time() - start_time) * 1000
                resource_id = get_resource_id(result_val, *args, **kwargs) if get_resource_id else ""
                
                get_audit_service().log(
                    action=action,
                    resource_type=resource_type,
                    resource_id=str(resource_id),
                    result=audit_result,
                    details={"error": error} if error else {},
                    duration_ms=duration_ms
                )
            
            return result_val
        
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    
    return decorator
