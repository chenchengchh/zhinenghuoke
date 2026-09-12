"""
动态权重调优器

支持基于行业/企业/上下文的权重动态调整，
并提供简单的反馈学习接口。
"""
import logging
import time
import threading
from typing import Dict, Any, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


class WeightTuner:
    """动态权重调优器。

    原理：
    - 维护历史检索-反馈对（query_hash, used_weights, user_satisfaction）
    - 定期根据反馈调整各路权重
    - 限制权重总和 = 1.0
    """

    def __init__(self, learning_rate: float = 0.05, min_weight: float = 0.1):
        self.learning_rate = learning_rate
        self.min_weight = min_weight
        self._lock = threading.Lock()
        # 行业/企业的当前权重
        self._current_weights: Dict[str, Dict[str, float]] = {}
        # 反馈数据
        self._feedback: Dict[str, list] = defaultdict(list)

    def get_weights(self, key: str, default: Dict[str, float]) -> Dict[str, float]:
        """获取指定 key（行业/企业）的当前权重。"""
        with self._lock:
            return dict(self._current_weights.get(key, default))

    def set_weights(self, key: str, weights: Dict[str, float]) -> None:
        """显式设置权重。"""
        # 归一化
        total = sum(weights.values())
        if total <= 0:
            return
        normalized = {k: v / total for k, v in weights.items()}
        with self._lock:
            self._current_weights[key] = normalized

    def record_feedback(
        self,
        key: str,
        weights: Dict[str, float],
        satisfaction: float,  # 0.0 ~ 1.0
    ) -> None:
        """记录一次反馈，用于后续调优。

        Args:
            key: 行业/企业标识
            weights: 当时使用的权重
            satisfaction: 用户满意度（0~1）
        """
        with self._lock:
            self._feedback[key].append({
                "weights": dict(weights),
                "satisfaction": satisfaction,
                "timestamp": time.time(),
            })
            # 限制反馈历史
            if len(self._feedback[key]) > 1000:
                self._feedback[key] = self._feedback[key][-1000:]

    def auto_tune(self, key: str, default: Dict[str, float]) -> Dict[str, float]:
        """基于历史反馈自动调优权重。

        简单策略：
        - 计算每个权重维度的平均满意度
        - 满意度高的维度权重上调，反之下调
        - 限制最小权重不低于 min_weight
        - 保证总和为 1.0
        """
        with self._lock:
            feedbacks = list(self._feedback.get(key, []))
            current = dict(self._current_weights.get(key, default))

        if not feedbacks or len(feedbacks) < 5:
            # 数据不足，返回当前权重
            return current

        # 计算每个权重维度的平均满意度
        weighted_satisfaction: Dict[str, float] = {}
        for fb in feedbacks:
            w = fb["weights"]
            s = fb["satisfaction"]
            for dim, weight in w.items():
                if dim not in weighted_satisfaction:
                    weighted_satisfaction[dim] = 0.0
                weighted_satisfaction[dim] += s * weight

        # 平均
        total_satisfaction = sum(
            fb["satisfaction"] for fb in feedbacks
        ) / len(feedbacks)
        if total_satisfaction <= 0:
            return current

        # 调整权重
        new_weights = {}
        for dim, w in current.items():
            dim_sat = weighted_satisfaction.get(dim, total_satisfaction) / total_satisfaction
            # 满意度偏离平均值的程度影响权重
            adjustment = (dim_sat - 0.5) * 2  # [-1, 1]
            new_w = w * (1 + self.learning_rate * adjustment)
            new_weights[dim] = max(self.min_weight, new_w)

        # 归一化
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: v / total for k, v in new_weights.items()}

        with self._lock:
            self._current_weights[key] = new_weights
        logger.info(
            f"[WeightTuner] {key} 权重已调整: {current} -> {new_weights}"
        )
        return new_weights

    def get_feedback_count(self, key: str) -> int:
        """获取指定 key 的反馈数量。"""
        with self._lock:
            return len(self._feedback.get(key, []))

    def clear_feedback(self, key: str) -> None:
        """清空指定 key 的反馈数据。"""
        with self._lock:
            self._feedback.pop(key, None)
            self._current_weights.pop(key, None)


# 模块级单例
_TUNER_INSTANCE: Optional[WeightTuner] = None
_TUNER_LOCK = threading.Lock()


def get_weight_tuner() -> WeightTuner:
    """获取权重调优器单例。"""
    global _TUNER_INSTANCE
    if _TUNER_INSTANCE is None:
        with _TUNER_LOCK:
            if _TUNER_INSTANCE is None:
                _TUNER_INSTANCE = WeightTuner()
    return _TUNER_INSTANCE
