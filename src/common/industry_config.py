"""
行业配置加载器

提供运行时按需加载行业相关关键词、模式、信号等配置，
替代代码中散落的硬编码常量。
"""
import os
import logging
import threading
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


# 默认配置目录
DEFAULT_CONFIG_DIR = "config/industry_keywords"


class IndustryConfigLoader:
    """行业配置加载器（线程安全 + 缓存）。"""

    def __init__(self, config_dir: str = DEFAULT_CONFIG_DIR):
        self.config_dir = config_dir
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load(self, industry: str) -> Dict[str, Any]:
        """加载指定行业的配置，无配置文件时返回空字典。"""
        with self._lock:
            if industry in self._cache:
                return self._cache[industry]
            path = os.path.join(self.config_dir, f"{industry}.yaml")
            if not os.path.exists(path):
                logger.debug(f"行业配置不存在: {path}，使用空配置")
                self._cache[industry] = {}
                return self._cache[industry]
            try:
                import yaml
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                self._cache[industry] = data
                return data
            except Exception as e:
                logger.error(f"加载行业配置失败 {path}: {e}")
                self._cache[industry] = {}
                return self._cache[industry]

    def get_patterns(self, industry: str) -> List[Dict[str, Any]]:
        """获取行业 patterns 配置（如旅游模式：跟团/自由行/景点等）。

        Returns:
            List[Dict]，如 [{"name": "跟团游", "keywords": ["跟团", "团队游"]}, ...]
        """
        cfg = self.load(industry)
        return list(cfg.get("patterns", []) or [])

    def get_intent_signals(self, industry: str) -> Dict[str, List[str]]:
        """获取行业意图信号配置。

        Returns:
            Dict[str, List[str]]，如 {"conversion": ["报名", "预订"], "info": ["价格"]}
        """
        cfg = self.load(industry)
        return dict(cfg.get("intent_signals", {}) or {})

    def get_category_keywords(self, industry: str) -> Dict[str, List[str]]:
        """获取行业类目关键词。

        Returns:
            Dict[str, List[str]]，如 {"price": ["价格", "费用"], "attractions": ["景点"]}
        """
        cfg = self.load(industry)
        return dict(cfg.get("category_keywords", {}) or {})

    def match_pattern(self, industry: str, text: str) -> Optional[str]:
        """根据行业 patterns 匹配文本，返回首个命中的 pattern 名称。

        Returns:
            Optional[str]，命中的 pattern 名称，未命中返回 None
        """
        if not text:
            return None
        text_str = str(text)
        for pattern in self.get_patterns(industry):
            name = pattern.get("name", "")
            keywords = pattern.get("keywords", []) or []
            for kw in keywords:
                if kw and kw in text_str:
                    return name
        return None

    def clear_cache(self) -> None:
        """清空缓存（用于配置更新后强制重新加载）。"""
        with self._lock:
            self._cache.clear()


# 模块级单例
_LOADER_INSTANCE: Optional[IndustryConfigLoader] = None
_LOADER_LOCK = threading.Lock()


def get_industry_config_loader() -> IndustryConfigLoader:
    """获取行业配置加载器单例。"""
    global _LOADER_INSTANCE
    if _LOADER_INSTANCE is None:
        with _LOADER_LOCK:
            if _LOADER_INSTANCE is None:
                _LOADER_INSTANCE = IndustryConfigLoader()
    return _LOADER_INSTANCE


# 便捷函数
def load_industry_keywords(industry: str = "tourism") -> Dict[str, Any]:
    """便捷函数：加载行业配置。"""
    return get_industry_config_loader().load(industry)


def match_industry_pattern(industry: str, text: str) -> Optional[str]:
    """便捷函数：匹配行业模式。"""
    return get_industry_config_loader().match_pattern(industry, text)
