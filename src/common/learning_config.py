"""
RAG 自学习配置加载器

从 YAML 加载自学习相关配置（关键词、阈值、模式等），
替代 self_learning_rag.py 中散落的硬编码。
"""
import os
import logging
import threading
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = "config/learning_keywords.yaml"


class LearningConfigLoader:
    """自学习配置加载器。"""

    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._data: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, Any]:
        if self._data is not None:
            return self._data
        if not os.path.exists(self.path):
            logger.debug(f"自学习配置不存在: {self.path}，使用空配置")
            self._data = {}
            return self._data
        try:
            import yaml
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
            return self._data
        except Exception as e:
            logger.error(f"加载自学习配置失败 {self.path}: {e}")
            self._data = {}
            return self._data

    def reload(self) -> None:
        """强制重新加载（用于配置更新后）。"""
        with self._lock:
            self._data = None

    @property
    def low_confidence_keywords(self) -> List[str]:
        return list(self._load().get("unknown_detection", {}).get("low_confidence_keywords", []) or [])

    @property
    def question_patterns(self) -> List[str]:
        return list(self._load().get("unknown_detection", {}).get("question_patterns", []) or [])

    @property
    def confidence_threshold(self) -> float:
        return float(self._load().get("unknown_detection", {}).get("confidence_threshold", 0.5))

    @property
    def injection_keywords(self) -> List[str]:
        """合并所有注入检测关键词。"""
        data = self._load().get("injection_detection", {})
        result = []
        for key in ("contact_keywords", "payment_keywords", "violation_keywords", "price_injection_keywords"):
            result.extend(data.get(key, []) or [])
        return result

    @property
    def intent_patterns(self) -> Dict[str, List[str]]:
        return dict(self._load().get("intent_patterns", {}) or {})

    @property
    def learning_thresholds(self) -> Dict[str, Any]:
        return dict(self._load().get("learning_thresholds", {}) or {})

    def detect_intent(self, text: str) -> Optional[str]:
        """根据意图模式匹配返回首个命中的意图名称。

        Returns:
            命中的意图名称，未命中返回 None
        """
        if not text:
            return None
        text_str = str(text)
        for intent_name, patterns in self.intent_patterns.items():
            for pattern in patterns or []:
                if pattern and pattern in text_str:
                    return intent_name
        return None


# 模块级单例
_LOADER_INSTANCE: Optional[LearningConfigLoader] = None
_LOADER_LOCK = threading.Lock()


def get_learning_config_loader() -> LearningConfigLoader:
    """获取自学习配置加载器单例。"""
    global _LOADER_INSTANCE
    if _LOADER_INSTANCE is None:
        with _LOADER_LOCK:
            if _LOADER_INSTANCE is None:
                _LOADER_INSTANCE = LearningConfigLoader()
    return _LOADER_INSTANCE


# 便捷函数
def get_low_confidence_keywords() -> List[str]:
    return get_learning_config_loader().low_confidence_keywords


def get_question_patterns() -> List[str]:
    return get_learning_config_loader().question_patterns


def get_injection_keywords() -> List[str]:
    return get_learning_config_loader().injection_keywords


def detect_intent(text: str) -> Optional[str]:
    return get_learning_config_loader().detect_intent(text)
