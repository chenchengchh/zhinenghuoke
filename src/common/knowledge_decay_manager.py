"""
知识衰减管理器
实现基于时间和使用的知识优先级衰减机制
"""
import math
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from loguru import logger


class MemoryTier(Enum):
    """
    记忆层级枚举
    
    定义知识的存储层级
    """
    WORKING = "working"      # 工作记忆：高频使用，快速访问
    CORE = "core"           # 核心记忆：重要知识，长期保存
    ARCHIVE = "archive"     # 档案记忆：低频使用，归档存储


@dataclass
class DecayConfig:
    """
    衰减配置
    
    定义知识衰减的各项参数
    """
    # 时间衰减系数 (λ)
    time_decay_lambda: float = 0.02
    
    # 使用频率因子权重
    usage_weight: float = 0.3
    
    # 时间衰减权重
    time_weight: float = 0.4
    
    # 质量权重
    quality_weight: float = 0.3
    
    # 层级迁移阈值
    working_to_core_threshold: float = 0.6
    core_to_archive_threshold: float = 0.3
    
    # 归档天数阈值
    archive_days_threshold: int = 90
    
    # 清理阈值（低于此值可删除）
    cleanup_threshold: float = 0.1


@dataclass
class KnowledgeState:
    """
    知识状态
    
    跟踪单个知识条目的状态
    """
    knowledge_id: str
    base_priority: float = 1.0
    current_priority: float = 1.0
    memory_tier: MemoryTier = MemoryTier.WORKING
    
    # 使用统计
    use_count: int = 0
    last_used: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    
    # 质量分数
    quality_score: float = 0.8
    
    # 衰减历史
    decay_history: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'knowledge_id': self.knowledge_id,
            'base_priority': self.base_priority,
            'current_priority': self.current_priority,
            'memory_tier': self.memory_tier.value,
            'use_count': self.use_count,
            'last_used': self.last_used.isoformat() if self.last_used else None,
            'created_at': self.created_at.isoformat(),
            'quality_score': self.quality_score
        }


class KnowledgeDecayManager:
    """
    知识衰减管理器
    
    管理知识的优先级衰减和层级迁移：
    - 基于时间的衰减
    - 基于使用的衰减
    - 自动层级迁移
    - 低价值知识清理
    """
    
    def __init__(self, config: DecayConfig = None, knowledge_base=None):
        """
        初始化知识衰减管理器
        
        Args:
            config: 衰减配置
            knowledge_base: 知识库实例
        """
        self.config = config or DecayConfig()
        self.knowledge_base = knowledge_base
        
        # 知识状态存储
        self._knowledge_states: Dict[str, KnowledgeState] = {}
        
        # 层级统计
        self._tier_stats: Dict[MemoryTier, int] = {
            MemoryTier.WORKING: 0,
            MemoryTier.CORE: 0,
            MemoryTier.ARCHIVE: 0
        }
        
        # 衰减统计
        self._decay_stats = {
            'total_decays': 0,
            'tier_transitions': 0,
            'cleanups': 0,
            'last_decay_time': None
        }
    
    def register_knowledge(
        self,
        knowledge_id: str,
        quality_score: float = 0.8,
        initial_tier: MemoryTier = MemoryTier.WORKING
    ) -> KnowledgeState:
        """
        注册新知识
        
        Args:
            knowledge_id: 知识ID
            quality_score: 初始质量分数
            initial_tier: 初始记忆层级
            
        Returns:
            知识状态
        """
        state = KnowledgeState(
            knowledge_id=knowledge_id,
            base_priority=1.0,
            current_priority=1.0,
            memory_tier=initial_tier,
            quality_score=quality_score,
            created_at=datetime.now()
        )
        
        self._knowledge_states[knowledge_id] = state
        self._tier_stats[initial_tier] += 1
        
        logger.info(f"注册新知识: {knowledge_id}, 层级: {initial_tier.value}")
        return state
    
    def record_usage(self, knowledge_id: str, boost_factor: float = 1.0):
        """
        记录知识使用
        
        Args:
            knowledge_id: 知识ID
            boost_factor: 优先级提升因子
        """
        state = self._knowledge_states.get(knowledge_id)
        if not state:
            state = self.register_knowledge(knowledge_id)
        
        state.use_count += 1
        state.last_used = datetime.now()
        
        # 使用时提升优先级
        boost = min(boost_factor * 0.1, 0.5)
        state.current_priority = min(state.current_priority + boost, 1.5)
        
        logger.debug(f"记录知识使用: {knowledge_id}, 使用次数: {state.use_count}")
    
    def calculate_decay(self, knowledge_id: str) -> float:
        """
        计算知识衰减后的优先级
        
        使用公式: priority = base_priority × exp(-λ × days) × usage_factor
        
        Args:
            knowledge_id: 知识ID
            
        Returns:
            衰减后的优先级
        """
        state = self._knowledge_states.get(knowledge_id)
        if not state:
            # 未注册知识不应把检索分数直接压成 0，返回中性权重以避免
            # 新知识或尚未建立衰减状态的知识被整体误判为无关结果。
            return 1.0
        
        config = self.config
        
        # 计算时间衰减因子
        days_since_use = 0
        if state.last_used:
            days_since_use = (datetime.now() - state.last_used).days
        elif state.created_at:
            days_since_use = (datetime.now() - state.created_at).days
        
        time_factor = math.exp(-config.time_decay_lambda * days_since_use)
        
        # 计算使用频率因子
        usage_factor = 1.0
        if state.use_count > 0:
            # 使用次数越多，因子越高（对数增长）
            usage_factor = 1.0 + 0.1 * math.log(1 + state.use_count)
        
        # 计算综合优先级（乘法模型：时间衰减 × 使用频率 × 质量评分）
        priority = (
            state.base_priority *
            time_factor *
            (config.time_weight + usage_factor * config.usage_weight + state.quality_score * config.quality_weight)
        )
        
        return min(max(priority, 0.0), 1.5)
    
    def apply_decay(self, knowledge_id: str) -> Tuple[float, Optional[MemoryTier]]:
        """
        应用衰减并检查层级迁移
        
        Args:
            knowledge_id: 知识ID
            
        Returns:
            (新优先级, 新层级或None)
        """
        state = self._knowledge_states.get(knowledge_id)
        if not state:
            return 0.0, None
        
        old_priority = state.current_priority
        old_tier = state.memory_tier
        
        # 计算新优先级
        new_priority = self.calculate_decay(knowledge_id)
        state.current_priority = new_priority
        
        # 记录衰减历史
        state.decay_history.append({
            'timestamp': datetime.now().isoformat(),
            'old_priority': old_priority,
            'new_priority': new_priority,
            'tier': old_tier.value
        })
        
        # 检查层级迁移
        new_tier = self._check_tier_transition(state)
        
        # 更新统计
        self._decay_stats['total_decays'] += 1
        self._decay_stats['last_decay_time'] = datetime.now()
        
        if new_tier != old_tier:
            self._tier_stats[old_tier] -= 1
            self._tier_stats[new_tier] += 1
            self._decay_stats['tier_transitions'] += 1
            
            logger.info(f"知识层级迁移: {knowledge_id}, {old_tier.value} -> {new_tier.value}")
        
        return new_priority, new_tier if new_tier != old_tier else None
    
    def _check_tier_transition(self, state: KnowledgeState) -> MemoryTier:
        """
        检查并执行层级迁移
        
        Args:
            state: 知识状态
            
        Returns:
            新的层级
        """
        current_tier = state.memory_tier
        priority = state.current_priority
        config = self.config
        
        # 检查是否需要降级
        if current_tier == MemoryTier.WORKING:
            if priority < config.working_to_core_threshold:
                state.memory_tier = MemoryTier.CORE
                return MemoryTier.CORE
        
        elif current_tier == MemoryTier.CORE:
            if priority < config.core_to_archive_threshold:
                state.memory_tier = MemoryTier.ARCHIVE
                return MemoryTier.ARCHIVE
        
        # 检查是否需要升级（高使用频率）
        if state.use_count >= 10 and priority > 0.8:
            if current_tier == MemoryTier.ARCHIVE:
                state.memory_tier = MemoryTier.CORE
                return MemoryTier.CORE
            elif current_tier == MemoryTier.CORE and state.use_count >= 20:
                state.memory_tier = MemoryTier.WORKING
                return MemoryTier.WORKING
        
        return current_tier
    
    def batch_decay(self, knowledge_ids: List[str] = None) -> Dict[str, Any]:
        """
        批量应用衰减
        
        Args:
            knowledge_ids: 要处理的知识ID列表，None表示全部
            
        Returns:
            衰减结果统计
        """
        if knowledge_ids is None:
            knowledge_ids = list(self._knowledge_states.keys())
        
        results = {
            'processed': 0,
            'tier_transitions': 0,
            'cleaned': 0,
            'transitions': []
        }
        
        to_clean = []
        
        for kid in knowledge_ids:
            priority, new_tier = self.apply_decay(kid)
            results['processed'] += 1
            
            if new_tier:
                results['tier_transitions'] += 1
                results['transitions'].append({
                    'knowledge_id': kid,
                    'new_tier': new_tier.value,
                    'priority': priority
                })
            
            # 检查是否需要清理
            if priority < self.config.cleanup_threshold:
                to_clean.append(kid)
        
        # 清理低价值知识
        for kid in to_clean:
            self._cleanup_knowledge(kid)
            results['cleaned'] += 1
        
        logger.info(f"批量衰减完成: 处理 {results['processed']} 条, "
                   f"迁移 {results['tier_transitions']} 条, "
                   f"清理 {results['cleaned']} 条")
        
        return results
    
    def _cleanup_knowledge(self, knowledge_id: str):
        """
        清理低价值知识
        
        Args:
            knowledge_id: 知识ID
        """
        state = self._knowledge_states.get(knowledge_id)
        if state:
            self._tier_stats[state.memory_tier] -= 1
            del self._knowledge_states[knowledge_id]
            self._decay_stats['cleanups'] += 1
            
            logger.info(f"清理低价值知识: {knowledge_id}")
    
    def get_knowledge_state(self, knowledge_id: str) -> Optional[KnowledgeState]:
        """
        获取知识状态
        
        Args:
            knowledge_id: 知识ID
            
        Returns:
            知识状态或None
        """
        return self._knowledge_states.get(knowledge_id)
    
    def get_tier_knowledge(self, tier: MemoryTier) -> List[str]:
        """
        获取指定层级的所有知识ID
        
        Args:
            tier: 记忆层级
            
        Returns:
            知识ID列表
        """
        return [
            kid for kid, state in self._knowledge_states.items()
            if state.memory_tier == tier
        ]
    
    def get_priority_sorted(self, limit: int = 100) -> List[Tuple[str, float]]:
        """
        获取按优先级排序的知识列表
        
        Args:
            limit: 返回数量限制
            
        Returns:
            (知识ID, 优先级) 列表
        """
        sorted_items = sorted(
            self._knowledge_states.items(),
            key=lambda x: x[1].current_priority,
            reverse=True
        )
        
        return [(kid, state.current_priority) for kid, state in sorted_items[:limit]]
    
    def get_decay_candidates(self) -> Dict[str, List[str]]:
        """
        获取待衰减处理的知识候选
        
        Returns:
            各层级的候选知识ID列表
        """
        candidates = {
            'to_core': [],      # 待降级到核心记忆
            'to_archive': [],   # 待降级到档案记忆
            'to_cleanup': []    # 待清理
        }
        
        for kid, state in self._knowledge_states.items():
            priority = state.current_priority
            
            if state.memory_tier == MemoryTier.WORKING:
                if priority < self.config.working_to_core_threshold:
                    candidates['to_core'].append(kid)
            
            elif state.memory_tier == MemoryTier.CORE:
                if priority < self.config.core_to_archive_threshold:
                    candidates['to_archive'].append(kid)
            
            if priority < self.config.cleanup_threshold:
                candidates['to_cleanup'].append(kid)
        
        return candidates
    
    def boost_knowledge(self, knowledge_id: str, boost_amount: float = 0.2):
        """
        手动提升知识优先级
        
        Args:
            knowledge_id: 知识ID
            boost_amount: 提升量
        """
        state = self._knowledge_states.get(knowledge_id)
        if state:
            state.current_priority = min(state.current_priority + boost_amount, 1.5)
            state.quality_score = min(state.quality_score + 0.1, 1.0)
            logger.info(f"手动提升知识优先级: {knowledge_id}, 新优先级: {state.current_priority:.2f}")
    
    def archive_old_knowledge(self, days_threshold: int = None) -> int:
        """
        归档长期未使用的知识
        
        Args:
            days_threshold: 天数阈值
            
        Returns:
            归档数量
        """
        threshold = days_threshold or self.config.archive_days_threshold
        archived = 0
        
        for kid, state in self._knowledge_states.items():
            if state.memory_tier == MemoryTier.WORKING:
                last_activity = state.last_used or state.created_at
                if last_activity:
                    days_inactive = (datetime.now() - last_activity).days
                    
                    if days_inactive > threshold:
                        state.memory_tier = MemoryTier.ARCHIVE
                        self._tier_stats[MemoryTier.WORKING] -= 1
                        self._tier_stats[MemoryTier.ARCHIVE] += 1
                        archived += 1
        
        if archived > 0:
            logger.info(f"归档长期未使用知识: {archived} 条")
        
        return archived
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        获取衰减管理统计信息
        
        Returns:
            统计信息字典
        """
        total = len(self._knowledge_states)
        
        avg_priority = 0.0
        if total > 0:
            avg_priority = sum(
                s.current_priority for s in self._knowledge_states.values()
            ) / total
        
        return {
            'total_knowledge': total,
            'tier_distribution': {
                tier.value: count for tier, count in self._tier_stats.items()
            },
            'average_priority': round(avg_priority, 3),
            'decay_stats': self._decay_stats.copy(),
            'config': {
                'time_decay_lambda': self.config.time_decay_lambda,
                'working_to_core_threshold': self.config.working_to_core_threshold,
                'core_to_archive_threshold': self.config.core_to_archive_threshold,
                'cleanup_threshold': self.config.cleanup_threshold
            }
        }
    
    def export_states(self) -> List[Dict[str, Any]]:
        """
        导出所有知识状态
        
        Returns:
            状态列表
        """
        return [state.to_dict() for state in self._knowledge_states.values()]
    
    def import_states(self, states: List[Dict[str, Any]]):
        """
        导入知识状态
        
        Args:
            states: 状态列表
        """
        for state_dict in states:
            try:
                state = KnowledgeState(
                    knowledge_id=state_dict['knowledge_id'],
                    base_priority=state_dict.get('base_priority', 1.0),
                    current_priority=state_dict.get('current_priority', 1.0),
                    memory_tier=MemoryTier(state_dict.get('memory_tier', 'working')),
                    use_count=state_dict.get('use_count', 0),
                    quality_score=state_dict.get('quality_score', 0.8)
                )
                
                if state_dict.get('last_used'):
                    state.last_used = datetime.fromisoformat(state_dict['last_used'])
                if state_dict.get('created_at'):
                    state.created_at = datetime.fromisoformat(state_dict['created_at'])
                
                self._knowledge_states[state.knowledge_id] = state
                self._tier_stats[state.memory_tier] += 1
                
            except Exception as e:
                logger.warning(f"导入知识状态失败: {e}")
        
        logger.info(f"导入知识状态: {len(states)} 条")


# 全局实例
_decay_manager = None


def get_decay_manager(config: DecayConfig = None, knowledge_base=None) -> KnowledgeDecayManager:
    """
    获取知识衰减管理器单例
    
    Args:
        config: 衰减配置
        knowledge_base: 知识库实例
        
    Returns:
        知识衰减管理器实例
    """
    global _decay_manager
    if _decay_manager is None:
        _decay_manager = KnowledgeDecayManager(config, knowledge_base)
    return _decay_manager
