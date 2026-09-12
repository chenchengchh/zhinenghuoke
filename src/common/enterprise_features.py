"""
企业级特性模块

提供多租户、权限控制、审计日志、性能监控等企业级功能

功能：
1. 多租户架构
2. 权限控制
3. 审计日志
4. 性能监控
"""

import time
import logging
import threading
import uuid
import json
from typing import List, Dict, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from collections import defaultdict

from loguru import logger


class Role(Enum):
    """用户角色"""
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"
    GUEST = "guest"


class Permission(Enum):
    """权限类型"""
    KB_READ = "kb_read"
    KB_WRITE = "kb_write"
    KB_DELETE = "kb_delete"
    KB_AUDIT = "kb_audit"
    USER_MANAGE = "user_manage"
    SYSTEM_CONFIG = "system_config"
    VIEW_ANALYTICS = "view_analytics"
    EXPORT_DATA = "export_data"


ROLE_PERMISSIONS: Dict[Role, Set[Permission]] = {
    Role.ADMIN: {
        Permission.KB_READ, Permission.KB_WRITE, Permission.KB_DELETE, Permission.KB_AUDIT,
        Permission.USER_MANAGE, Permission.SYSTEM_CONFIG, Permission.VIEW_ANALYTICS, Permission.EXPORT_DATA
    },
    Role.EDITOR: {
        Permission.KB_READ, Permission.KB_WRITE, Permission.VIEW_ANALYTICS
    },
    Role.VIEWER: {
        Permission.KB_READ, Permission.VIEW_ANALYTICS
    },
    Role.GUEST: {
        Permission.KB_READ
    }
}


@dataclass
class Tenant:
    """租户定义"""
    tenant_id: str
    name: str
    plan: str = "standard"
    max_users: int = 10
    max_knowledge_items: int = 1000
    features: Set[str] = field(default_factory=set)
    created_at: datetime = field(default_factory=datetime.now)
    status: str = "active"
    
    def to_dict(self) -> Dict:
        return {
            "tenant_id": self.tenant_id,
            "name": self.name,
            "plan": self.plan,
            "max_users": self.max_users,
            "max_knowledge_items": self.max_knowledge_items,
            "features": list(self.features),
            "created_at": self.created_at.isoformat(),
            "status": self.status
        }


@dataclass
class User:
    """用户定义"""
    user_id: str
    tenant_id: str
    username: str
    email: str = ""
    role: Role = Role.VIEWER
    permissions: Set[Permission] = field(default_factory=set)
    created_at: datetime = field(default_factory=datetime.now)
    last_login: datetime = None
    status: str = "active"
    
    def __post_init__(self):
        if not self.permissions:
            self.permissions = ROLE_PERMISSIONS.get(self.role, set()).copy()
    
    def has_permission(self, permission: Permission) -> bool:
        """检查用户是否拥有指定权限"""
        if self.role == Role.ADMIN:
            return True
        return permission in self.permissions
    
    def to_dict(self) -> Dict:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "username": self.username,
            "email": self.email,
            "role": self.role.value,
            "permissions": [p.value for p in self.permissions],
            "created_at": self.created_at.isoformat(),
            "last_login": self.last_login.isoformat() if self.last_login else None,
            "status": self.status
        }


@dataclass
class AuditLog:
    """审计日志"""
    log_id: str
    tenant_id: str
    user_id: str
    action: str
    resource_type: str
    resource_id: str
    details: Dict = field(default_factory=dict)
    ip_address: str = ""
    status: str = "success"
    timestamp: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict:
        return {
            "log_id": self.log_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "details": self.details,
            "ip_address": self.ip_address,
            "status": self.status,
            "timestamp": self.timestamp.isoformat()
        }


@dataclass
class PerformanceMetric:
    """性能指标"""
    metric_id: str
    metric_type: str
    value: float
    unit: str = "ms"
    tenant_id: str = ""
    session_id: str = ""
    metadata: Dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


class TenantManager:
    """租户管理器"""
    
    def __init__(self):
        self._tenants: Dict[str, Tenant] = {}
        self._users: Dict[str, User] = {}
        self._tenant_users: Dict[str, Set[str]] = defaultdict(set)
        self._lock = threading.Lock()
        self._create_default_tenant()
    
    def _create_default_tenant(self):
        default_tenant = Tenant(
            tenant_id="default",
            name="Default Tenant",
            plan="enterprise",
            max_users=100,
            max_knowledge_items=10000,
            features={"vector_search", "multi_turn", "quality_eval"}
        )
        self._tenants["default"] = default_tenant
    
    def create_tenant(
        self,
        name: str,
        plan: str = "standard",
        max_users: int = 10,
        max_knowledge_items: int = 1000,
        features: List[str] = None
    ) -> Tenant:
        tenant_id = str(uuid.uuid4())[:8]
        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            plan=plan,
            max_users=max_users,
            max_knowledge_items=max_knowledge_items,
            features=set(features or [])
        )
        with self._lock:
            self._tenants[tenant_id] = tenant
        return tenant
    
    def get_tenant(self, tenant_id: str) -> Optional[Tenant]:
        return self._tenants.get(tenant_id)
    
    def update_tenant(self, tenant_id: str, **kwargs) -> bool:
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return False
        for key, value in kwargs.items():
            if hasattr(tenant, key):
                setattr(tenant, key, value)
        return True
    
    def delete_tenant(self, tenant_id: str) -> bool:
        if tenant_id == "default":
            return False
        with self._lock:
            if tenant_id in self._tenants:
                del self._tenants[tenant_id]
                if tenant_id in self._tenant_users:
                    for user_id in self._tenant_users[tenant_id]:
                        if user_id in self._users:
                            del self._users[user_id]
                    del self._tenant_users[tenant_id]
                return True
        return False
    
    def create_user(
        self,
        tenant_id: str,
        username: str,
        email: str = "",
        role: Role = Role.VIEWER
    ) -> Optional[User]:
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return None
        current_users = len(self._tenant_users.get(tenant_id, set()))
        if current_users >= tenant.max_users:
            return None
        user_id = str(uuid.uuid4())[:8]
        user = User(
            user_id=user_id,
            tenant_id=tenant_id,
            username=username,
            email=email,
            role=role
        )
        with self._lock:
            self._users[user_id] = user
            self._tenant_users[tenant_id].add(user_id)
        return user
    
    def get_user(self, user_id: str) -> Optional[User]:
        return self._users.get(user_id)
    
    def get_tenant_users(self, tenant_id: str) -> List[User]:
        user_ids = self._tenant_users.get(tenant_id, set())
        return [self._users[uid] for uid in user_ids if uid in self._users]
    
    def update_user_role(self, user_id: str, role: Role) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        user.role = role
        user.permissions = ROLE_PERMISSIONS.get(role, set()).copy()
        return True
    
    def check_permission(self, user_id: str, permission: Permission) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        return user.has_permission(permission)


class PermissionManager:
    """权限管理器"""
    
    def __init__(self, tenant_manager: TenantManager = None):
        self.tenant_manager = tenant_manager or TenantManager()
    
    def check_permission(
        self,
        user_id: str,
        permission: Permission,
        resource_id: str = None
    ) -> bool:
        return self.tenant_manager.check_permission(user_id, permission)
    
    def grant_permission(self, user_id: str, permission: Permission) -> bool:
        user = self.tenant_manager.get_user(user_id)
        if not user:
            return False
        user.permissions.add(permission)
        return True
    
    def revoke_permission(self, user_id: str, permission: Permission) -> bool:
        user = self.tenant_manager.get_user(user_id)
        if not user:
            return False
        user.permissions.discard(permission)
        return True
    
    def get_user_permissions(self, user_id: str) -> Set[Permission]:
        user = self.tenant_manager.get_user(user_id)
        if not user:
            return set()
        return user.permissions.copy()


class AuditLogger:
    """审计日志记录器"""
    
    MAX_LOGS = 10000
    
    def __init__(self):
        self._logs: Dict[str, AuditLog] = {}
        self._tenant_logs: Dict[str, List[str]] = defaultdict(list)
        self._lock = threading.Lock()
    
    def log(
        self,
        tenant_id: str,
        user_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        details: Dict = None,
        ip_address: str = "",
        status: str = "success"
    ) -> AuditLog:
        log_id = str(uuid.uuid4())[:8]
        log_entry = AuditLog(
            log_id=log_id,
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            ip_address=ip_address,
            status=status
        )
        with self._lock:
            self._logs[log_id] = log_entry
            self._tenant_logs[tenant_id].append(log_id)
            if len(self._logs) > self.MAX_LOGS:
                self._cleanup_old_logs()
        return log_entry
    
    def _cleanup_old_logs(self):
        all_log_ids = list(self._logs.keys())
        to_remove = all_log_ids[:len(all_log_ids) - self.MAX_LOGS // 2]
        for log_id in to_remove:
            log_entry = self._logs.get(log_id)
            if log_entry:
                tenant_id = log_entry.tenant_id
                if log_id in self._tenant_logs.get(tenant_id, []):
                    self._tenant_logs[tenant_id].remove(log_id)
                del self._logs[log_id]
    
    def get_logs(
        self,
        tenant_id: str = None,
        user_id: str = None,
        action: str = None,
        start_time: datetime = None,
        end_time: datetime = None,
        limit: int = 100
    ) -> List[AuditLog]:
        logs = []
        log_ids = []
        if tenant_id:
            log_ids = self._tenant_logs.get(tenant_id, [])
        else:
            log_ids = list(self._logs.keys())
        for log_id in log_ids:
            log_entry = self._logs.get(log_id)
            if not log_entry:
                continue
            if user_id and log_entry.user_id != user_id:
                continue
            if action and log_entry.action != action:
                continue
            if start_time and log_entry.timestamp < start_time:
                continue
            if end_time and log_entry.timestamp > end_time:
                continue
            logs.append(log_entry)
        logs.sort(key=lambda x: x.timestamp, reverse=True)
        return logs[:limit]


class PerformanceMonitor:
    """性能监控器"""
    
    def __init__(self):
        self._metrics: Dict[str, List[PerformanceMetric]] = defaultdict(list)
        self._counters: Dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()
    
    def record(
        self,
        metric_type: str,
        value: float,
        tenant_id: str = "",
        session_id: str = "",
        metadata: Dict = None
    ) -> PerformanceMetric:
        metric_id = str(uuid.uuid4())[:8]
        metric = PerformanceMetric(
            metric_id=metric_id,
            metric_type=metric_type,
            value=value,
            tenant_id=tenant_id,
            session_id=session_id,
            metadata=metadata or {}
        )
        with self._lock:
            self._metrics[metric_type].append(metric)
            if len(self._metrics[metric_type]) > 1000:
                self._metrics[metric_type] = self._metrics[metric_type][-500:]
        return metric
    
    def increment(self, counter_name: str, value: int = 1):
        with self._lock:
            self._counters[counter_name] += value
    
    def get_stats(
        self,
        metric_type: str = None,
        time_range: timedelta = None
    ) -> Dict[str, Any]:
        if metric_type:
            metrics = self._metrics.get(metric_type, [])
        else:
            metrics = []
            for m_list in self._metrics.values():
                metrics.extend(m_list)
        if time_range:
            cutoff = datetime.now() - time_range
            metrics = [m for m in metrics if m.timestamp >= cutoff]
        if not metrics:
            return {"count": 0}
        values = [m.value for m in metrics]
        return {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
            "sum": sum(values)
        }
    
    def get_counters(self) -> Dict[str, int]:
        return dict(self._counters)


class EnterpriseManager:
    """企业级管理器"""
    
    def __init__(self):
        self.tenant_manager = TenantManager()
        self.permission_manager = PermissionManager(self.tenant_manager)
        self.audit_logger = AuditLogger()
        self.performance_monitor = PerformanceMonitor()
    
    def initialize(self):
        logger.info("企业级管理器初始化完成")
    
    def get_system_stats(self) -> Dict[str, Any]:
        return {
            "tenants": len(self.tenant_manager._tenants),
            "users": len(self.tenant_manager._users),
            "audit_logs": len(self.audit_logger._logs),
            "performance_metrics": sum(len(m) for m in self.performance_monitor._metrics.values()),
            "counters": self.performance_monitor.get_counters()
        }


_enterprise_manager: Optional[EnterpriseManager] = None
_enterprise_manager_lock = threading.Lock()


def get_enterprise_manager() -> EnterpriseManager:
    """获取企业级管理器实例（线程安全）"""
    global _enterprise_manager
    if _enterprise_manager is None:
        with _enterprise_manager_lock:
            if _enterprise_manager is None:
                _enterprise_manager = EnterpriseManager()
                _enterprise_manager.initialize()
    return _enterprise_manager
