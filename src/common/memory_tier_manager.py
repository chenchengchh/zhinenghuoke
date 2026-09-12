"""
分层记忆管理器
实现工作记忆、核心记忆、档案记忆的三层架构
"""
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from loguru import logger


class MemoryTier(Enum):
    """
    记忆层级枚举
    
    工作记忆：高频使用，快速访问，容量有限
    核心记忆：重要知识，长期保存，中等容量
    档案记忆：低频使用，归档存储，大容量
    """
    WORKING = "working"
    CORE = "core"
    ARCHIVE = "archive"


@dataclass
class MemoryConfig:
    """
    分层记忆配置
    
    定义各层级的容量和行为参数
    """
    # 工作记忆配置
    working_capacity: int = 100          # 工作记忆容量
    working_ttl: int = 7                 # 工作记忆TTL（天）
    working_access_threshold: int = 5    # 进入工作记忆的访问次数阈值
    
    # 核心记忆配置
    core_capacity: int = 1000            # 核心记忆容量
    core_ttl: int = 90                   # 核心记忆TTL（天）
    core_access_threshold: int = 3       # 进入核心记忆的访问次数阈值
    
    # 档案记忆配置
    archive_capacity: int = 10000        # 档案记忆容量
    archive_ttl: int = 365               # 档案记忆TTL（天）
    
    # 迁移配置
    promotion_threshold: float = 0.7     # 晋升阈值
    demotion_threshold: float = 0.3      # 降级阈值
    
    # 清理配置
    cleanup_interval: int = 24           # 清理间隔（小时）
    cleanup_batch_size: int = 100        # 每次清理数量


@dataclass
class MemoryEntry:
    """
    记忆条目
    
    表示存储在分层记忆中的一个知识条目
    """
    knowledge_id: str
    tier: MemoryTier = MemoryTier.WORKING
    
    # 访问统计
    access_count: int = 0
    last_accessed: datetime = field(default_factory=datetime.now)
    created_at: datetime = field(default_factory=datetime.now)
    
    # 重要性评分
    importance_score: float = 0.5
    quality_score: float = 0.5
    
    # 元数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # 过期时间
    expires_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'knowledge_id': self.knowledge_id,
            'tier': self.tier.value,
            'access_count': self.access_count,
            'last_accessed': self.last_accessed.isoformat(),
            'created_at': self.created_at.isoformat(),
            'importance_score': self.importance_score,
            'quality_score': self.quality_score,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None
        }
    
    def calculate_priority(self) -> float:
        """
        计算优先级
        
        Returns:
            优先级分数 (0-1)
        """
        # 基础分
        score = self.importance_score * 0.4 + self.quality_score * 0.3
        
        # 访问频率加分
        if self.access_count > 0:
            access_score = min(self.access_count / 10, 0.3)
            score += access_score * 0.3
        
        return min(score, 1.0)


class MemoryTierManager:
    """
    分层记忆管理器
    
    管理知识的三层存储：
    - 工作记忆：最近使用、高频访问的知识
    - 核心记忆：重要、经过验证的知识
    - 档案记忆：历史知识、低频访问的知识
    """
    
    def __init__(self, config: MemoryConfig = None, knowledge_base=None):
        """
        初始化分层记忆管理器
        
        Args:
            config: 记忆配置
            knowledge_base: 知识库实例
        """
        self.config = config or MemoryConfig()
        self.knowledge_base = knowledge_base
        
        # 各层级存储
        self._tiers: Dict[MemoryTier, Dict[str, MemoryEntry]] = {
            MemoryTier.WORKING: {},
            MemoryTier.CORE: {},
            MemoryTier.ARCHIVE: {}
        }
        
        # 索引
        self._id_to_tier: Dict[str, MemoryTier] = {}
        
        # 统计
        self._stats = {
            'total_entries': 0,
            'promotions': 0,
            'demotions': 0,
            'evictions': 0,
            'last_cleanup': None
        }
    
    def add(
        self,
        knowledge_id: str,
        tier: MemoryTier = MemoryTier.WORKING,
        importance_score: float = 0.5,
        quality_score: float = 0.5,
        metadata: Dict[str, Any] = None
    ) -> MemoryEntry:
        """
        添加知识到记忆系统
        
        Args:
            knowledge_id: 知识ID
            tier: 目标层级
            importance_score: 重要性评分
            quality_score: 质量评分
            metadata: 元数据
            
        Returns:
            创建的记忆条目
        """
        # 检查是否已存在
        if knowledge_id in self._id_to_tier:
            existing_tier = self._id_to_tier[knowledge_id]
            entry = self._tiers[existing_tier][knowledge_id]
            entry.access_count += 1
            entry.last_accessed = datetime.now()
            return entry
        
        # 创建新条目
        entry = MemoryEntry(
            knowledge_id=knowledge_id,
            tier=tier,
            importance_score=importance_score,
            quality_score=quality_score,
            metadata=metadata or {},
            created_at=datetime.now()
        )
        
        # 设置过期时间
        entry.expires_at = self._calculate_expiry(tier)
        
        # 检查容量
        self._ensure_capacity(tier)
        
        # 添加到目标层级
        self._tiers[tier][knowledge_id] = entry
        self._id_to_tier[knowledge_id] = tier
        self._stats['total_entries'] += 1
        
        logger.debug(f"添加知识到{tier.value}记忆: {knowledge_id}")
        return entry
    
    def get(self, knowledge_id: str) -> Optional[MemoryEntry]:
        """
        获取知识条目
        
        Args:
            knowledge_id: 知识ID
            
        Returns:
            记忆条目或None
        """
        tier = self._id_to_tier.get(knowledge_id)
        if tier:
            entry = self._tiers[tier].get(knowledge_id)
            if entry:
                # 更新访问统计
                entry.access_count += 1
                entry.last_accessed = datetime.now()
                
                # 检查是否需要晋升
                self._check_promotion(entry)
            
            return entry
        return None
    
    def remove(self, knowledge_id: str) -> bool:
        """
        移除知识条目
        
        Args:
            knowledge_id: 知识ID
            
        Returns:
            是否成功移除
        """
        tier = self._id_to_tier.get(knowledge_id)
        if tier:
            del self._tiers[tier][knowledge_id]
            del self._id_to_tier[knowledge_id]
            self._stats['total_entries'] -= 1
            return True
        return False
    
    def promote(self, knowledge_id: str) -> bool:
        """
        晋升知识到更高层级
        
        Args:
            knowledge_id: 知识ID
            
        Returns:
            是否成功晋升
        """
        tier = self._id_to_tier.get(knowledge_id)
        if not tier:
            return False
        
        # 确定目标层级
        target_tier = None
        if tier == MemoryTier.ARCHIVE:
            target_tier = MemoryTier.CORE
        elif tier == MemoryTier.CORE:
            target_tier = MemoryTier.WORKING
        
        if not target_tier:
            return False
        
        # 执行晋升
        entry = self._tiers[tier].pop(knowledge_id)
        entry.tier = target_tier
        entry.expires_at = self._calculate_expiry(target_tier)
        
        self._ensure_capacity(target_tier)
        self._tiers[target_tier][knowledge_id] = entry
        self._id_to_tier[knowledge_id] = target_tier
        
        self._stats['promotions'] += 1
        logger.info(f"知识晋升: {knowledge_id} {tier.value} -> {target_tier.value}")
        return True
    
    def demote(self, knowledge_id: str) -> bool:
        """
        降级知识到更低层级
        
        Args:
            knowledge_id: 知识ID
            
        Returns:
            是否成功降级
        """
        tier = self._id_to_tier.get(knowledge_id)
        if not tier:
            return False
        
        # 确定目标层级
        target_tier = None
        if tier == MemoryTier.WORKING:
            target_tier = MemoryTier.CORE
        elif tier == MemoryTier.CORE:
            target_tier = MemoryTier.ARCHIVE
        
        if not target_tier:
            return False
        
        # 执行降级
        entry = self._tiers[tier].pop(knowledge_id)
        entry.tier = target_tier
        entry.expires_at = self._calculate_expiry(target_tier)
        
        self._tiers[target_tier][knowledge_id] = entry
        self._id_to_tier[knowledge_id] = target_tier
        
        self._stats['demotions'] += 1
        logger.info(f"知识降级: {knowledge_id} {tier.value} -> {target_tier.value}")
        return True
    
    def _check_promotion(self, entry: MemoryEntry):
        """
        检查是否需要晋升
        
        Args:
            entry: 记忆条目
        """
        priority = entry.calculate_priority()
        
        if priority >= self.config.promotion_threshold:
            if entry.tier == MemoryTier.ARCHIVE:
                self.promote(entry.knowledge_id)
            elif entry.tier == MemoryTier.CORE and entry.access_count >= self.config.working_access_threshold:
                self.promote(entry.knowledge_id)
    
    def _ensure_capacity(self, tier: MemoryTier):
        """
        确保层级容量
        
        Args:
            tier: 记忆层级
        """
        capacity_map = {
            MemoryTier.WORKING: self.config.working_capacity,
            MemoryTier.CORE: self.config.core_capacity,
            MemoryTier.ARCHIVE: self.config.archive_capacity
        }
        
        capacity = capacity_map[tier]
        current_size = len(self._tiers[tier])
        
        if current_size >= capacity:
            # 需要驱逐
            self._evict(tier, current_size - capacity + 1)
    
    def _evict(self, tier: MemoryTier, count: int):
        """
        驱逐低优先级条目
        
        Args:
            tier: 记忆层级
            count: 驱逐数量
        """
        entries = list(self._tiers[tier].values())
        
        # 按优先级排序
        entries.sort(key=lambda e: e.calculate_priority())
        
        # 驱逐最低优先级的
        for entry in entries[:count]:
            if tier == MemoryTier.WORKING:
                # 工作记忆驱逐到核心记忆
                self.demote(entry.knowledge_id)
            elif tier == MemoryTier.CORE:
                # 核心记忆驱逐到档案记忆
                self.demote(entry.knowledge_id)
            else:
                # 档案记忆直接删除
                self.remove(entry.knowledge_id)
                self._stats['evictions'] += 1
    
    def _calculate_expiry(self, tier: MemoryTier) -> datetime:
        """
        计算过期时间
        
        Args:
            tier: 记忆层级
            
        Returns:
            过期时间
        """
        ttl_map = {
            MemoryTier.WORKING: self.config.working_ttl,
            MemoryTier.CORE: self.config.core_ttl,
            MemoryTier.ARCHIVE: self.config.archive_ttl
        }
        
        ttl = ttl_map[tier]
        return datetime.now() + timedelta(days=ttl)
    
    def cleanup_expired(self) -> int:
        """
        清理过期条目
        
        Returns:
            清理数量
        """
        cleaned = 0
        now = datetime.now()
        
        for tier in [MemoryTier.WORKING, MemoryTier.CORE, MemoryTier.ARCHIVE]:
            expired = [
                kid for kid, entry in self._tiers[tier].items()
                if entry.expires_at and entry.expires_at < now
            ]
            
            for kid in expired:
                if tier == MemoryTier.ARCHIVE:
                    self.remove(kid)
                    cleaned += 1
                else:
                    # 降级而非删除
                    self.demote(kid)
                    cleaned += 1
        
        self._stats['last_cleanup'] = now.isoformat()
        
        if cleaned > 0:
            logger.info(f"清理过期条目: {cleaned} 条")
        
        return cleaned
    
    def get_tier_entries(self, tier: MemoryTier) -> List[MemoryEntry]:
        """
        获取指定层级的所有条目
        
        Args:
            tier: 记忆层级
            
        Returns:
            条目列表
        """
        return list(self._tiers[tier].values())
    
    def get_tier_size(self, tier: MemoryTier) -> int:
        """
        获取指定层级的大小
        
        Args:
            tier: 记忆层级
            
        Returns:
            条目数量
        """
        return len(self._tiers[tier])
    
    def get_tier_for_knowledge(self, knowledge_id: str) -> Optional[MemoryTier]:
        """
        获取知识所在的层级
        
        Args:
            knowledge_id: 知识ID
            
        Returns:
            记忆层级或None
        """
        return self._id_to_tier.get(knowledge_id)
    
    def get_high_priority_entries(self, limit: int = 10) -> List[MemoryEntry]:
        """
        获取高优先级条目
        
        Args:
            limit: 返回数量限制
            
        Returns:
            高优先级条目列表
        """
        all_entries = []
        for tier_entries in self._tiers.values():
            all_entries.extend(tier_entries.values())
        
        # 按优先级排序
        all_entries.sort(key=lambda e: e.calculate_priority(), reverse=True)
        
        return all_entries[:limit]
    
    def get_recent_entries(self, limit: int = 10) -> List[MemoryEntry]:
        """
        获取最近访问的条目
        
        Args:
            limit: 返回数量限制
            
        Returns:
            最近访问条目列表
        """
        all_entries = []
        for tier_entries in self._tiers.values():
            all_entries.extend(tier_entries.values())
        
        # 按最后访问时间排序
        all_entries.sort(key=lambda e: e.last_accessed, reverse=True)
        
        return all_entries[:limit]
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计信息字典
        """
        return {
            'total_entries': self._stats['total_entries'],
            'tier_sizes': {
                tier.value: len(entries)
                for tier, entries in self._tiers.items()
            },
            'tier_capacities': {
                'working': self.config.working_capacity,
                'core': self.config.core_capacity,
                'archive': self.config.archive_capacity
            },
            'promotions': self._stats['promotions'],
            'demotions': self._stats['demotions'],
            'evictions': self._stats['evictions'],
            'last_cleanup': self._stats['last_cleanup']
        }
    
    def export_state(self) -> Dict[str, Any]:
        """
        导出状态
        
        Returns:
            状态字典
        """
        return {
            'config': {
                'working_capacity': self.config.working_capacity,
                'core_capacity': self.config.core_capacity,
                'archive_capacity': self.config.archive_capacity
            },
            'tiers': {
                tier.value: [e.to_dict() for e in entries.values()]
                for tier, entries in self._tiers.items()
            },
            'stats': self._stats.copy()
        }
    
    def import_state(self, state: Dict[str, Any]):
        """
        导入状态
        
        Args:
            state: 状态字典
        """
        # 清空现有数据
        for tier in self._tiers:
            self._tiers[tier].clear()
        self._id_to_tier.clear()
        
        # 导入数据
        for tier_name, entries in state.get('tiers', {}).items():
            tier = MemoryTier(tier_name)
            for entry_dict in entries:
                entry = MemoryEntry(
                    knowledge_id=entry_dict['knowledge_id'],
                    tier=tier,
                    access_count=entry_dict.get('access_count', 0),
                    importance_score=entry_dict.get('importance_score', 0.5),
                    quality_score=entry_dict.get('quality_score', 0.5)
                )
                
                if entry_dict.get('last_accessed'):
                    entry.last_accessed = datetime.fromisoformat(entry_dict['last_accessed'])
                if entry_dict.get('created_at'):
                    entry.created_at = datetime.fromisoformat(entry_dict['created_at'])
                if entry_dict.get('expires_at'):
                    entry.expires_at = datetime.fromisoformat(entry_dict['expires_at'])
                
                self._tiers[tier][entry.knowledge_id] = entry
                self._id_to_tier[entry.knowledge_id] = tier
        
        # 导入统计
        self._stats.update(state.get('stats', {}))
        
        logger.info(f"导入分层记忆状态: {self._stats['total_entries']} 条")


# 全局实例
_memory_tier_manager = None


def get_memory_tier_manager(config: MemoryConfig = None, knowledge_base=None) -> MemoryTierManager:
    """
    获取分层记忆管理器单例
    
    Args:
        config: 记忆配置
        knowledge_base: 知识库实例
        
    Returns:
        分层记忆管理器实例
    """
    global _memory_tier_manager
    if _memory_tier_manager is None:
        _memory_tier_manager = MemoryTierManager(config, knowledge_base)
    return _memory_tier_manager
