"""
知识冲突检测器
检测知识库中相互矛盾或冲突的知识条目
"""
import re
from typing import Dict, List, Any, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from loguru import logger


class ConflictType(Enum):
    """
    冲突类型枚举
    """
    CONTRADICTION = "contradiction"     # 矛盾冲突
    DUPLICATE = "duplicate"             # 重复内容
    OUTDATED = "outdated"               # 过时信息
    INCONSISTENT = "inconsistent"       # 不一致


class ConflictSeverity(Enum):
    """
    冲突严重程度
    """
    HIGH = "high"       # 高严重度：需要立即处理
    MEDIUM = "medium"   # 中严重度：建议处理
    LOW = "low"         # 低严重度：可选处理


@dataclass
class ConflictResult:
    """
    冲突检测结果
    
    表示检测到的知识冲突
    """
    conflict_id: str
    conflict_type: ConflictType
    severity: ConflictSeverity
    
    # 冲突的知识条目
    knowledge_ids: List[str]
    knowledge_questions: List[str]
    knowledge_answers: List[str]
    
    # 冲突描述
    description: str
    suggestion: str
    
    # 检测信息
    detected_at: datetime = field(default_factory=datetime.now)
    resolved: bool = False
    resolution: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'conflict_id': self.conflict_id,
            'conflict_type': self.conflict_type.value,
            'severity': self.severity.value,
            'knowledge_ids': self.knowledge_ids,
            'knowledge_questions': self.knowledge_questions,
            'description': self.description,
            'suggestion': self.suggestion,
            'detected_at': self.detected_at.isoformat(),
            'resolved': self.resolved
        }


@dataclass
class ConflictStats:
    """
    冲突统计
    """
    total_conflicts: int = 0
    by_type: Dict[str, int] = field(default_factory=dict)
    by_severity: Dict[str, int] = field(default_factory=dict)
    resolved_count: int = 0


class ConflictDetector:
    """
    知识冲突检测器
    
    检测知识库中的各种冲突：
    - 矛盾冲突：相同问题有不同答案
    - 重复内容：相似度过高的知识条目
    - 过时信息：与最新信息冲突的旧知识
    - 不一致：格式或分类不一致
    """
    
    # 数字提取模式
    NUMBER_PATTERN = r'\d+(?:\.\d+)?'
    
    # 价格相关关键词
    PRICE_KEYWORDS = ['价格', '费用', '多少钱', '收费', '套餐', '元', '块']
    
    # 矛盾指示词
    CONTRADICTION_INDICATORS = [
        ('是', '不是'),
        ('可以', '不能'),
        ('有', '没有'),
        ('支持', '不支持'),
        ('包含', '不包含'),
        ('需要', '不需要')
    ]
    
    def __init__(self, knowledge_base=None, similarity_threshold: float = 0.85):
        """
        初始化冲突检测器
        
        Args:
            knowledge_base: 知识库实例
            similarity_threshold: 相似度阈值（用于检测重复）
        """
        self.knowledge_base = knowledge_base
        self.similarity_threshold = similarity_threshold
        
        # 冲突存储
        self._conflicts: Dict[str, ConflictResult] = {}
        
        # 统计
        self._stats = ConflictStats()
        
        # 忽略列表（已确认不是冲突的）
        self._ignore_list: Set[str] = set()
    
    def detect_all_conflicts(
        self,
        knowledge_items: List[Any]
    ) -> List[ConflictResult]:
        """
        检测所有类型的冲突
        
        Args:
            knowledge_items: 知识条目列表
            
        Returns:
            检测到的冲突列表
        """
        all_conflicts = []
        
        # 检测矛盾冲突
        contradictions = self.detect_contradictions(knowledge_items)
        all_conflicts.extend(contradictions)
        
        # 检测重复内容
        duplicates = self.detect_duplicates(knowledge_items)
        all_conflicts.extend(duplicates)
        
        # 检测过时信息
        outdated = self.detect_outdated(knowledge_items)
        all_conflicts.extend(outdated)
        
        # 检测不一致
        inconsistent = self.detect_inconsistencies(knowledge_items)
        all_conflicts.extend(inconsistent)
        
        # 更新统计
        self._update_stats(all_conflicts)
        
        # 存储冲突结果到内部字典
        for conflict in all_conflicts:
            self._conflicts[conflict.conflict_id] = conflict
        
        return all_conflicts
    
    def detect_contradictions(
        self,
        knowledge_items: List[Any]
    ) -> List[ConflictResult]:
        """
        检测矛盾冲突
        
        相同或相似问题有不同答案
        
        Args:
            knowledge_items: 知识条目列表
            
        Returns:
            矛盾冲突列表
        """
        conflicts = []
        
        # 按问题分组
        question_groups = self._group_by_similarity(knowledge_items)
        
        for group_key, items in question_groups.items():
            if len(items) < 2:
                continue
            
            # 检查组内是否有矛盾
            for i, item1 in enumerate(items):
                for item2 in items[i + 1:]:
                    contradiction = self._check_contradiction(item1, item2)
                    if contradiction:
                        conflicts.append(contradiction)
        
        return conflicts
    
    def _group_by_similarity(
        self,
        knowledge_items: List[Any]
    ) -> Dict[str, List[Any]]:
        """
        按相似度分组
        
        Args:
            knowledge_items: 知识条目列表
            
        Returns:
            分组字典
        """
        groups: Dict[str, List[Any]] = {}
        
        for item in knowledge_items:
            question = getattr(item, 'question', '')
            if not question:
                continue
            
            # 简化问题用于分组
            simplified = self._simplify_question(question)
            
            # 查找相似组
            found_group = None
            for group_key in groups:
                if self._calculate_similarity(simplified, group_key) > self.similarity_threshold:
                    found_group = group_key
                    break
            
            if found_group:
                groups[found_group].append(item)
            else:
                groups[simplified] = [item]
        
        return groups
    
    def _simplify_question(self, question: str) -> str:
        """
        简化问题（去除标点、空格等）
        
        Args:
            question: 原始问题
            
        Returns:
            简化后的问题
        """
        simplified = re.sub(r'[？?！!。，,、]', '', question)
        simplified = simplified.replace(' ', '')
        return simplified.lower()
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """
        计算文本相似度
        
        Args:
            text1: 文本1
            text2: 文本2
            
        Returns:
            相似度 (0-1)
        """
        if not text1 or not text2:
            return 0.0
        
        # 优先使用向量嵌入计算语义相似度
        semantic_sim = self._semantic_similarity(text1, text2)
        if semantic_sim is not None:
            return semantic_sim
        
        # 回退到编辑距离计算相似度
        distance = self._levenshtein_distance(text1, text2)
        max_len = max(len(text1), len(text2))
        
        return 1 - distance / max_len
    
    def _semantic_similarity(self, text1: str, text2: str) -> Optional[float]:
        """
        使用向量嵌入计算语义相似度
        
        Args:
            text1: 文本1
            text2: 文本2
            
        Returns:
            语义相似度 (0-1)，如果嵌入服务不可用返回None
        """
        try:
            from src.rag.embedding_service import build_embedding_config_from_app_config, get_embedding_service
            if not hasattr(self, '_embedding_service') or self._embedding_service is None:
                config = build_embedding_config_from_app_config()
                self._embedding_service = get_embedding_service(config)
            
            vec1 = self._embedding_service.embedSingle(text1)
            vec2 = self._embedding_service.embedSingle(text2)
            
            if vec1 is not None and vec2 is not None:
                import numpy as np
                v1 = np.array(vec1)
                v2 = np.array(vec2)
                norm1 = np.linalg.norm(v1)
                norm2 = np.linalg.norm(v2)
                if norm1 > 0 and norm2 > 0:
                    cosine_sim = np.dot(v1, v2) / (norm1 * norm2)
                    return float(max(0, min(1, (cosine_sim + 1) / 2)))
        except Exception:
            pass
        
        return None
    
    def _levenshtein_distance(self, s1: str, s2: str) -> int:
        """
        计算编辑距离
        
        Args:
            s1: 字符串1
            s2: 字符串2
            
        Returns:
            编辑距离
        """
        if len(s1) < len(s2):
            return self._levenshtein_distance(s2, s1)
        
        if len(s2) == 0:
            return len(s1)
        
        previous_row = range(len(s2) + 1)
        
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        
        return previous_row[-1]
    
    def _check_contradiction(
        self,
        item1: Any,
        item2: Any
    ) -> Optional[ConflictResult]:
        """
        检查两个知识条目是否矛盾
        
        Args:
            item1: 知识条目1
            item2: 知识条目2
            
        Returns:
            冲突结果或None
        """
        answer1 = getattr(item1, 'answer', '')
        answer2 = getattr(item2, 'answer', '')
        question1 = getattr(item1, 'question', '')
        question2 = getattr(item2, 'question', '')
        
        id1 = getattr(item1, 'id', str(id(item1)))
        id2 = getattr(item2, 'id', str(id(item2)))
        
        # 检查冲突ID是否在忽略列表
        conflict_id = self._generate_conflict_id(id1, id2, ConflictType.CONTRADICTION)
        if conflict_id in self._ignore_list:
            return None
        
        # 检查数字矛盾（价格等）
        number_conflict = self._check_number_contradiction(answer1, answer2)
        if number_conflict:
            return ConflictResult(
                conflict_id=conflict_id,
                conflict_type=ConflictType.CONTRADICTION,
                severity=ConflictSeverity.HIGH,
                knowledge_ids=[id1, id2],
                knowledge_questions=[question1, question2],
                knowledge_answers=[answer1, answer2],
                description=f"检测到数字矛盾: {number_conflict}",
                suggestion="请核实价格或数值信息，确保一致性"
            )
        
        # 检查语义矛盾
        semantic_conflict = self._check_semantic_contradiction(answer1, answer2)
        if semantic_conflict:
            return ConflictResult(
                conflict_id=conflict_id,
                conflict_type=ConflictType.CONTRADICTION,
                severity=ConflictSeverity.MEDIUM,
                knowledge_ids=[id1, id2],
                knowledge_questions=[question1, question2],
                knowledge_answers=[answer1, answer2],
                description=f"检测到语义矛盾: {semantic_conflict}",
                suggestion="请核实答案内容，确保表述一致"
            )
        
        return None
    
    def _check_number_contradiction(
        self,
        answer1: str,
        answer2: str
    ) -> Optional[str]:
        """
        检查数字矛盾
        
        Args:
            answer1: 答案1
            answer2: 答案2
            
        Returns:
            矛盾描述或None
        """
        # 检查是否包含价格关键词
        has_price_keyword = (
            any(kw in answer1 + answer2 for kw in self.PRICE_KEYWORDS)
        )
        
        if not has_price_keyword:
            return None
        
        # 提取数字
        numbers1 = re.findall(self.NUMBER_PATTERN, answer1)
        numbers2 = re.findall(self.NUMBER_PATTERN, answer2)
        
        if not numbers1 or not numbers2:
            return None
        
        # 比较主要数字
        try:
            main_num1 = float(numbers1[0])
            main_num2 = float(numbers2[0])
            
            # 如果差异超过20%，认为是矛盾
            if main_num1 > 0 and main_num2 > 0:
                diff_ratio = abs(main_num1 - main_num2) / max(main_num1, main_num2)
                if diff_ratio > 0.2:
                    return f"数值差异较大: {main_num1} vs {main_num2}"
        except ValueError:
            pass
        
        return None
    
    def _check_semantic_contradiction(
        self,
        answer1: str,
        answer2: str
    ) -> Optional[str]:
        """
        检查语义矛盾
        
        Args:
            answer1: 答案1
            answer2: 答案2
            
        Returns:
            矛盾描述或None
        """
        for positive, negative in self.CONTRADICTION_INDICATORS:
            if positive in answer1 and negative in answer2:
                return f"'{positive}' vs '{negative}'"
            if negative in answer1 and positive in answer2:
                return f"'{negative}' vs '{positive}'"
        
        return None
    
    def detect_duplicates(
        self,
        knowledge_items: List[Any]
    ) -> List[ConflictResult]:
        """
        检测重复内容
        
        Args:
            knowledge_items: 知识条目列表
            
        Returns:
            重复冲突列表
        """
        conflicts = []
        checked_pairs: Set[Tuple[str, str]] = set()
        
        for i, item1 in enumerate(knowledge_items):
            for j, item2 in enumerate(knowledge_items[i + 1:], i + 1):
                id1 = getattr(item1, 'id', str(id(item1)))
                id2 = getattr(item2, 'id', str(id(item2)))
                
                pair_key = tuple(sorted([id1, id2]))
                if pair_key in checked_pairs:
                    continue
                checked_pairs.add(pair_key)
                
                # 检查问题相似度
                question1 = getattr(item1, 'question', '')
                question2 = getattr(item2, 'question', '')
                
                question_sim = self._calculate_similarity(
                    self._simplify_question(question1),
                    self._simplify_question(question2)
                )
                
                # 检查答案相似度
                answer1 = getattr(item1, 'answer', '')
                answer2 = getattr(item2, 'answer', '')
                
                answer_sim = self._calculate_similarity(answer1, answer2)
                
                # 判断是否重复
                if question_sim > 0.9 and answer_sim > 0.9:
                    conflict_id = self._generate_conflict_id(id1, id2, ConflictType.DUPLICATE)
                    
                    if conflict_id not in self._ignore_list:
                        conflicts.append(ConflictResult(
                            conflict_id=conflict_id,
                            conflict_type=ConflictType.DUPLICATE,
                            severity=ConflictSeverity.LOW,
                            knowledge_ids=[id1, id2],
                            knowledge_questions=[question1, question2],
                            knowledge_answers=[answer1, answer2],
                            description=f"检测到重复内容 (问题相似度: {question_sim:.2%}, 答案相似度: {answer_sim:.2%})",
                            suggestion="建议合并或删除重复条目"
                        ))
        
        return conflicts
    
    def detect_outdated(
        self,
        knowledge_items: List[Any]
    ) -> List[ConflictResult]:
        """
        检测过时信息
        
        Args:
            knowledge_items: 知识条目列表
            
        Returns:
            过时信息冲突列表
        """
        conflicts = []
        
        # 时效性关键词
        time_keywords = ['最新', '当前', '现在', '目前', '今年', '本月', '限时', '优惠']
        
        for item in knowledge_items:
            question = getattr(item, 'question', '')
            answer = getattr(item, 'answer', '')
            item_id = getattr(item, 'id', str(id(item)))
            created_at = getattr(item, 'created_at', None)
            
            # 检查是否包含时效性关键词
            has_time_keyword = any(kw in question + answer for kw in time_keywords)
            
            if has_time_keyword and created_at:
                # 检查创建时间
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.fromisoformat(created_at)
                    except (ValueError, TypeError) as e:
                        logger.debug(f"日期解析失败: {created_at}, 错误: {e}")
                        continue
                
                days_old = (datetime.now() - created_at).days
                
                if days_old > 30:
                    conflict_id = f"outdated_{item_id}"
                    
                    if conflict_id not in self._ignore_list:
                        conflicts.append(ConflictResult(
                            conflict_id=conflict_id,
                            conflict_type=ConflictType.OUTDATED,
                            severity=ConflictSeverity.MEDIUM,
                            knowledge_ids=[item_id],
                            knowledge_questions=[question],
                            knowledge_answers=[answer],
                            description=f"检测到可能过时的信息 (创建于 {days_old} 天前)",
                            suggestion="建议更新或确认信息是否仍然有效"
                        ))
        
        return conflicts
    
    def detect_inconsistencies(
        self,
        knowledge_items: List[Any]
    ) -> List[ConflictResult]:
        """
        检测不一致
        
        Args:
            knowledge_items: 知识条目列表
            
        Returns:
            不一致冲突列表
        """
        conflicts = []
        
        # 按分类检查格式一致性
        category_items: Dict[str, List[Any]] = {}
        
        for item in knowledge_items:
            category = getattr(item, 'category', '其他')
            if category not in category_items:
                category_items[category] = []
            category_items[category].append(item)
        
        # 检查每个分类内的格式一致性
        for category, items in category_items.items():
            if len(items) < 2:
                continue
            
            # 检查答案长度差异
            lengths = [len(getattr(item, 'answer', '')) for item in items]
            avg_length = sum(lengths) / len(lengths)
            
            for item in items:
                answer = getattr(item, 'answer', '')
                item_id = getattr(item, 'id', str(id(item)))
                
                # 答案过短或过长
                if len(answer) < avg_length * 0.3 or len(answer) > avg_length * 3:
                    conflict_id = f"inconsistent_{item_id}"
                    
                    if conflict_id not in self._ignore_list:
                        conflicts.append(ConflictResult(
                            conflict_id=conflict_id,
                            conflict_type=ConflictType.INCONSISTENT,
                            severity=ConflictSeverity.LOW,
                            knowledge_ids=[item_id],
                            knowledge_questions=[getattr(item, 'question', '')],
                            knowledge_answers=[answer],
                            description=f"答案长度与同类知识差异较大 (平均: {avg_length:.0f}, 当前: {len(answer)})",
                            suggestion="建议调整答案长度，保持同类知识格式一致"
                        ))
        
        return conflicts
    
    def _generate_conflict_id(
        self,
        id1: str,
        id2: str,
        conflict_type: ConflictType
    ) -> str:
        """
        生成冲突ID
        
        Args:
            id1: 知识ID1
            id2: 知识ID2
            conflict_type: 冲突类型
            
        Returns:
            冲突ID
        """
        import hashlib
        combined = f"{id1}_{id2}_{conflict_type.value}"
        return hashlib.md5(combined.encode()).hexdigest()[:12]
    
    def _update_stats(self, conflicts: List[ConflictResult]):
        """
        更新统计信息
        
        Args:
            conflicts: 冲突列表
        """
        self._stats.total_conflicts = len(conflicts)
        
        for conflict in conflicts:
            # 按类型统计
            type_key = conflict.conflict_type.value
            self._stats.by_type[type_key] = self._stats.by_type.get(type_key, 0) + 1
            
            # 按严重程度统计
            severity_key = conflict.severity.value
            self._stats.by_severity[severity_key] = self._stats.by_severity.get(severity_key, 0) + 1
    
    def resolve_conflict(self, conflict_id: str, resolution: str):
        """
        解决冲突
        
        Args:
            conflict_id: 冲突ID
            resolution: 解决方案
        """
        conflict = self._conflicts.get(conflict_id)
        if conflict:
            conflict.resolved = True
            conflict.resolution = resolution
            self._stats.resolved_count += 1
            logger.info(f"冲突已解决: {conflict_id}")
    
    def ignore_conflict(self, conflict_id: str):
        """
        忽略冲突
        
        Args:
            conflict_id: 冲突ID
        """
        self._ignore_list.add(conflict_id)
        if conflict_id in self._conflicts:
            del self._conflicts[conflict_id]
        logger.info(f"忽略冲突: {conflict_id}")
    
    def get_conflict(self, conflict_id: str) -> Optional[ConflictResult]:
        """
        获取冲突详情
        
        Args:
            conflict_id: 冲突ID
            
        Returns:
            冲突详情或None
        """
        return self._conflicts.get(conflict_id)
    
    def get_all_conflicts(self) -> List[ConflictResult]:
        """
        获取所有冲突
        
        Returns:
            冲突列表
        """
        return list(self._conflicts.values())
    
    def get_unresolved_conflicts(self) -> List[ConflictResult]:
        """
        获取未解决的冲突
        
        Returns:
            未解决的冲突列表
        """
        return [c for c in self._conflicts.values() if not c.resolved]
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计信息字典
        """
        return {
            'total_conflicts': self._stats.total_conflicts,
            'by_type': self._stats.by_type,
            'by_severity': self._stats.by_severity,
            'resolved_count': self._stats.resolved_count,
            'ignored_count': len(self._ignore_list)
        }


# 全局实例
_conflict_detector = None


def get_conflict_detector(knowledge_base=None) -> ConflictDetector:
    """
    获取冲突检测器单例
    
    Args:
        knowledge_base: 知识库实例
        
    Returns:
        冲突检测器实例
    """
    global _conflict_detector
    if _conflict_detector is None:
        _conflict_detector = ConflictDetector(knowledge_base)
    return _conflict_detector
