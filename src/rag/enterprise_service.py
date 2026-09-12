"""
多企业管理模块
支持多租户数据隔离
"""
import os
import json
import sqlite3
import hashlib
import secrets
import threading
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from loguru import logger


class EnterpriseStatus(Enum):
    """企业状态"""
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TRIAL = "trial"
    EXPIRED = "expired"


class SubscriptionPlan(Enum):
    """订阅计划"""
    FREE = "free"
    BASIC = "basic"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


@dataclass
class Enterprise:
    """企业实体"""
    id: str
    name: str
    code: str
    status: EnterpriseStatus = EnterpriseStatus.ACTIVE
    subscriptionPlan: SubscriptionPlan = SubscriptionPlan.FREE
    
    config: Dict = field(default_factory=dict)
    
    qwenApiKey: str = ""
    qwenModel: str = "qwen-plus"
    qwenDailyQuota: int = 10000
    
    maxDocuments: int = 100
    maxStorageMb: int = 500
    
    contactName: str = ""
    contactEmail: str = ""
    contactPhone: str = ""
    
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None
    expiresAt: Optional[datetime] = None
    
    def __post_init__(self):
        if self.createdAt is None:
            self.createdAt = datetime.now()
        if self.updatedAt is None:
            self.updatedAt = datetime.now()
    
    def isActive(self) -> bool:
        """检查企业是否有效"""
        if self.status != EnterpriseStatus.ACTIVE:
            return False
        if self.expiresAt and self.expiresAt < datetime.now():
            return False
        return True
    
    def toDict(self) -> Dict:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "status": self.status.value,
            "subscriptionPlan": self.subscriptionPlan.value,
            "config": self.config,
            "qwenModel": self.qwenModel,
            "qwenDailyQuota": self.qwenDailyQuota,
            "maxDocuments": self.maxDocuments,
            "maxStorageMb": self.maxStorageMb,
            "contactName": self.contactName,
            "contactEmail": self.contactEmail,
            "contactPhone": self.contactPhone,
            "createdAt": self.createdAt.isoformat() if self.createdAt else None,
            "updatedAt": self.updatedAt.isoformat() if self.updatedAt else None,
            "expiresAt": self.expiresAt.isoformat() if self.expiresAt else None
        }


@dataclass
class EnterpriseConfig:
    """企业配置"""
    enterpriseId: str
    
    autoReplyEnabled: bool = True
    replyTone: str = "friendly"
    replyLanguage: str = "zh-CN"
    maxReplyLength: int = 200
    
    intentAnalysisEnabled: bool = True
    intentThresholdA: int = 80
    intentThresholdB: int = 60
    intentThresholdC: int = 40
    intentThresholdD: int = 20
    
    knowledgeCategories: List[str] = field(default_factory=lambda: ["general"])
    defaultCategory: str = "general"
    
    autoEscalation: bool = True
    escalationKeywords: List[str] = field(default_factory=lambda: ["人工", "客服"])
    
    customSystemPrompt: str = ""
    customGreeting: str = ""
    
    platforms: List[str] = field(default_factory=lambda: ["douyin"])
    
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None
    
    def __post_init__(self):
        if self.createdAt is None:
            self.createdAt = datetime.now()
        if self.updatedAt is None:
            self.updatedAt = datetime.now()
    
    def toDict(self) -> Dict:
        """转换为字典"""
        return {
            "enterpriseId": self.enterpriseId,
            "autoReplyEnabled": self.autoReplyEnabled,
            "replyTone": self.replyTone,
            "replyLanguage": self.replyLanguage,
            "maxReplyLength": self.maxReplyLength,
            "intentAnalysisEnabled": self.intentAnalysisEnabled,
            "intentThresholdA": self.intentThresholdA,
            "intentThresholdB": self.intentThresholdB,
            "intentThresholdC": self.intentThresholdC,
            "intentThresholdD": self.intentThresholdD,
            "knowledgeCategories": self.knowledgeCategories,
            "defaultCategory": self.defaultCategory,
            "autoEscalation": self.autoEscalation,
            "escalationKeywords": self.escalationKeywords,
            "customSystemPrompt": self.customSystemPrompt,
            "customGreeting": self.customGreeting,
            "platforms": self.platforms
        }


class TenantContext:
    """
    租户上下文
    管理当前请求的租户信息（线程安全）
    """

    _local = threading.local()

    @classmethod
    def setTenant(cls, enterpriseId: str, enterprise: Optional[Enterprise] = None):
        """设置当前租户"""
        cls._local.tenant = enterpriseId
        cls._local.enterprise = enterprise

    @classmethod
    def getTenant(cls) -> Optional[str]:
        """获取当前租户"""
        return getattr(cls._local, 'tenant', None)

    @classmethod
    def getEnterprise(cls) -> Optional[Enterprise]:
        """获取当前企业"""
        return getattr(cls._local, 'enterprise', None)

    @classmethod
    def clear(cls):
        """清除租户上下文"""
        cls._local.tenant = None
        cls._local.enterprise = None


class DataIsolationManager:
    """
    数据隔离管理器
    实现多层级数据隔离
    """
    
    def __init__(self, storageRoot: str = "data"):
        self.storageRoot = storageRoot
    
    def getVectorCollectionName(self, enterpriseId: str) -> str:
        """获取向量集合名称"""
        return f"enterprise_{enterpriseId}"
    
    def getFileDirectory(self, enterpriseId: str) -> str:
        """获取文件目录"""
        return os.path.join(self.storageRoot, "knowledge_files", enterpriseId)
    
    def getCacheKey(self, enterpriseId: str, key: str) -> str:
        """获取缓存键"""
        return f"tenant:{enterpriseId}:{key}"
    
    def getGraphName(self, enterpriseId: str) -> str:
        """获取图谱名称"""
        return f"graph_{enterpriseId}"
    
    def buildQueryFilter(self, enterpriseId: str) -> Dict:
        """构建查询过滤条件"""
        return {"enterpriseId": enterpriseId}


class EnterpriseService:
    """
    企业管理服务
    处理企业的CRUD操作
    """
    
    def __init__(self, dataPath: str = "data/enterprises"):
        self.dataPath = dataPath
        self.enterprises: Dict[str, Enterprise] = {}
        self.configs: Dict[str, EnterpriseConfig] = {}
        self.isolation = DataIsolationManager()
        self._db_path = os.path.join(dataPath, "enterprises.db")
        self._db_lock = threading.Lock()
        
        os.makedirs(dataPath, exist_ok=True)
        self._init_db()
        self._loadData()
    
    def _init_db(self):
        with self._db_lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS enterprises (
                        id TEXT PRIMARY KEY,
                        data TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS configs (
                        enterprise_id TEXT PRIMARY KEY,
                        data TEXT NOT NULL
                    )
                """)
                conn.commit()
            finally:
                conn.close()

    def _migrate_json_to_sqlite(self):
        enterprises_file = os.path.join(self.dataPath, "enterprises.json")
        configs_file = os.path.join(self.dataPath, "configs.json")
        if not os.path.exists(ent_file := enterprises_file) and not os.path.exists(configs_file):
            return
        with self._db_lock:
            conn = sqlite3.connect(self._db_path)
            try:
                if os.path.exists(ent_file):
                    with open(ent_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    for ent_id, ent_data in data.items():
                        conn.execute(
                            "INSERT OR REPLACE INTO enterprises (id, data) VALUES (?, ?)",
                            (ent_id, json.dumps(ent_data, ensure_ascii=False))
                        )
                    backup_path = ent_file + ".migrated"
                    os.replace(ent_file, backup_path)
                if os.path.exists(configs_file):
                    with open(configs_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    for ent_id, config_data in data.items():
                        conn.execute(
                            "INSERT OR REPLACE INTO configs (enterprise_id, data) VALUES (?, ?)",
                            (ent_id, json.dumps(config_data, ensure_ascii=False))
                        )
                    backup_path = configs_file + ".migrated"
                    os.replace(configs_file, backup_path)
                conn.commit()
            except Exception as e:
                logger.error(f"JSON迁移SQLite失败: {e}")
            finally:
                conn.close()

    def _loadData(self):
        self._migrate_json_to_sqlite()
        with self._db_lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cursor = conn.execute("SELECT id, data FROM enterprises")
                for row in cursor:
                    ent_id, data_str = row
                    try:
                        ent_data = json.loads(data_str)
                        self.enterprises[ent_id] = Enterprise(
                            id=ent_data["id"],
                            name=ent_data["name"],
                            code=ent_data["code"],
                            status=EnterpriseStatus(ent_data.get("status", "active")),
                            subscriptionPlan=SubscriptionPlan(ent_data.get("subscriptionPlan", "free")),
                            config=ent_data.get("config", {}),
                            qwenModel=ent_data.get("qwenModel", "qwen-plus"),
                            qwenDailyQuota=ent_data.get("qwenDailyQuota", 10000),
                            maxDocuments=ent_data.get("maxDocuments", 100),
                            maxStorageMb=ent_data.get("maxStorageMb", 500),
                            contactName=ent_data.get("contactName", ""),
                            contactEmail=ent_data.get("contactEmail", ""),
                            contactPhone=ent_data.get("contactPhone", ""),
                            createdAt=datetime.fromisoformat(ent_data["createdAt"]) if ent_data.get("createdAt") else None,
                            updatedAt=datetime.fromisoformat(ent_data["updatedAt"]) if ent_data.get("updatedAt") else None,
                            expiresAt=datetime.fromisoformat(ent_data["expiresAt"]) if ent_data.get("expiresAt") else None
                        )
                    except Exception as e:
                        logger.error(f"加载企业 {ent_id} 失败: {e}")

                cursor = conn.execute("SELECT enterprise_id, data FROM configs")
                for row in cursor:
                    ent_id, data_str = row
                    try:
                        config_data = json.loads(data_str)
                        self.configs[ent_id] = EnterpriseConfig(
                            enterpriseId=config_data["enterpriseId"],
                            autoReplyEnabled=config_data.get("autoReplyEnabled", True),
                            replyTone=config_data.get("replyTone", "friendly"),
                            replyLanguage=config_data.get("replyLanguage", "zh-CN"),
                            maxReplyLength=config_data.get("maxReplyLength", 200),
                            intentAnalysisEnabled=config_data.get("intentAnalysisEnabled", True),
                            intentThresholdA=config_data.get("intentThresholdA", 80),
                            intentThresholdB=config_data.get("intentThresholdB", 60),
                            intentThresholdC=config_data.get("intentThresholdC", 40),
                            intentThresholdD=config_data.get("intentThresholdD", 20),
                            knowledgeCategories=config_data.get("knowledgeCategories", ["general"]),
                            defaultCategory=config_data.get("defaultCategory", "general"),
                            autoEscalation=config_data.get("autoEscalation", True),
                            escalationKeywords=config_data.get("escalationKeywords", ["人工", "客服"]),
                            customSystemPrompt=config_data.get("customSystemPrompt", ""),
                            customGreeting=config_data.get("customGreeting", ""),
                            platforms=config_data.get("platforms", ["douyin"])
                        )
                    except Exception as e:
                        logger.error(f"加载企业配置 {ent_id} 失败: {e}")
            finally:
                conn.close()
    
    def _saveData(self):
        with self._db_lock:
            conn = sqlite3.connect(self._db_path)
            try:
                for ent_id, ent in self.enterprises.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO enterprises (id, data) VALUES (?, ?)",
                        (ent_id, json.dumps(ent.toDict(), ensure_ascii=False))
                    )
                for ent_id, config in self.configs.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO configs (enterprise_id, data) VALUES (?, ?)",
                        (ent_id, json.dumps(config.toDict(), ensure_ascii=False))
                    )
                conn.commit()
            except Exception as e:
                logger.error(f"保存企业数据失败: {e}")
            finally:
                conn.close()
    
    def createEnterprise(
        self,
        name: str,
        contactEmail: str,
        plan: SubscriptionPlan = SubscriptionPlan.FREE,
        **kwargs
    ) -> Enterprise:
        """
        创建企业
        
        Args:
            name: 企业名称
            contactEmail: 联系邮箱
            plan: 订阅计划
            **kwargs: 其他参数
        
        Returns:
            创建的企业实体
        """
        enterpriseId = self._generateId(name)
        code = self._generateCode(name)
        
        enterprise = Enterprise(
            id=enterpriseId,
            name=name,
            code=code,
            subscriptionPlan=plan,
            contactEmail=contactEmail,
            **kwargs
        )
        
        self.enterprises[enterpriseId] = enterprise
        
        defaultConfig = EnterpriseConfig(enterpriseId=enterpriseId)
        self.configs[enterpriseId] = defaultConfig
        
        self._saveData()
        
        return enterprise
    
    def getEnterprise(self, enterpriseId: str) -> Optional[Enterprise]:
        """获取企业"""
        return self.enterprises.get(enterpriseId)
    
    def getEnterpriseByCode(self, code: str) -> Optional[Enterprise]:
        """通过代码获取企业"""
        for enterprise in self.enterprises.values():
            if enterprise.code == code:
                return enterprise
        return None
    
    def updateEnterprise(
        self,
        enterpriseId: str,
        **kwargs
    ) -> Optional[Enterprise]:
        """更新企业"""
        enterprise = self.getEnterprise(enterpriseId)
        if not enterprise:
            return None
        
        for key, value in kwargs.items():
            if hasattr(enterprise, key):
                if key == "status" and isinstance(value, str):
                    value = EnterpriseStatus(value)
                elif key == "subscriptionPlan" and isinstance(value, str):
                    value = SubscriptionPlan(value)
                setattr(enterprise, key, value)
        
        enterprise.updatedAt = datetime.now()
        self._saveData()
        
        return enterprise
    
    def deleteEnterprise(self, enterpriseId: str) -> bool:
        """删除企业"""
        if enterpriseId not in self.enterprises:
            return False
        
        del self.enterprises[enterpriseId]
        if enterpriseId in self.configs:
            del self.configs[enterpriseId]
        
        self._saveData()
        
        return True
    
    def getConfig(self, enterpriseId: str) -> Optional[EnterpriseConfig]:
        """获取企业配置"""
        return self.configs.get(enterpriseId)
    
    def updateConfig(
        self,
        enterpriseId: str,
        **kwargs
    ) -> Optional[EnterpriseConfig]:
        """更新企业配置"""
        config = self.getConfig(enterpriseId)
        if not config:
            return None
        
        for key, value in kwargs.items():
            if hasattr(config, key):
                setattr(config, key, value)
        
        config.updatedAt = datetime.now()
        self._saveData()
        
        return config
    
    def listEnterprises(
        self,
        status: Optional[EnterpriseStatus] = None,
        plan: Optional[SubscriptionPlan] = None
    ) -> List[Enterprise]:
        """列出企业"""
        enterprises = list(self.enterprises.values())
        
        if status:
            enterprises = [e for e in enterprises if e.status == status]
        if plan:
            enterprises = [e for e in enterprises if e.subscriptionPlan == plan]
        
        return sorted(enterprises, key=lambda x: x.createdAt, reverse=True)
    
    def checkQuota(
        self,
        enterpriseId: str,
        quotaType: str,
        currentUsage: Optional[Dict] = None
    ) -> bool:
        """检查配额"""
        enterprise = self.getEnterprise(enterpriseId)
        if not enterprise or not enterprise.isActive():
            return False
        
        currentUsage = currentUsage or {}
        
        if quotaType == "storage":
            return currentUsage.get("storageMb", 0) < enterprise.maxStorageMb
        
        elif quotaType == "documents":
            return currentUsage.get("documentCount", 0) < enterprise.maxDocuments
        
        return True
    
    def _generateId(self, name: str) -> str:
        """生成企业ID"""
        content = f"{name}_{datetime.now().timestamp()}"
        return hashlib.md5(content.encode()).hexdigest()[:16]
    
    def _generateCode(self, name: str) -> str:
        """生成企业代码"""
        return secrets.token_urlsafe(8)
    
    def setTenantContext(self, enterpriseId: str):
        """设置租户上下文"""
        enterprise = self.getEnterprise(enterpriseId)
        TenantContext.setTenant(enterpriseId, enterprise)
    
    def clearTenantContext(self):
        """清除租户上下文"""
        TenantContext.clear()
