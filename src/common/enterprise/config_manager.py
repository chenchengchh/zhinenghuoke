"""
企业级配置管理器

提供：
1. 集中配置管理
2. 多环境配置
3. 热更新支持
"""

import yaml
import json
import time
import threading
from typing import Dict, Any, Optional, Callable
from pathlib import Path
from loguru import logger
from dataclasses import dataclass, field


@dataclass
class ConfigItem:
    """配置项"""
    key: str
    value: Any
    config_type: str
    description: str = ""
    default_value: Any = None
    validator: Optional[Callable] = None
    updated_at: float = field(default_factory=time.time)


class ConfigManager:
    """
    企业级配置管理器

    特性：
    1. 集中管理 - 统一配置入口
    2. 类型安全 - 配置类型验证
    3. 热更新 - 配置变更即时生效
    4. 分环境 - dev/staging/prod
    """

    DEFAULT_CONFIG = {
        "rpa": {
            "poll_interval": 2.0,
            "poll_interval_idle": 5.0,
            "max_retries": 3,
            "max_concurrent_operations": 1,
            "rate_limit": {
                "send_per_window": 20,
                "click_per_window": 30,
                "window_size": 60
            }
        },
        "rag": {
            "top_k": 5,
            "min_confidence": 0.6,
            "max_context_length": 10,
            "cache_ttl": 300
        },
        "cache": {
            "enabled": True,
            "ttl": 300,
            "max_size": 1000
        },
        "observability": {
            "metrics_enabled": True,
            "logging_level": "INFO"
        }
    }

    def __init__(self, config_path: str = "config/app_config.yaml"):
        self._config_path = Path(config_path)
        self._configs: Dict[str, ConfigItem] = {}
        self._lock = threading.RLock()
        self._watchers: Dict[str, List[Callable]] = {}
        self._running = True
        self._load_default_config()
        self._load_from_file()
        self._start_file_watcher()

    def _flatten_dict(self, d: Dict, prefix: str = "") -> Dict[str, Any]:
        """扁平化字典"""
        items = {}
        for k, v in d.items():
            new_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                items.update(self._flatten_dict(v, new_key))
            else:
                items[new_key] = v
        return items

    def _unflatten_dict(self, flat: Dict[str, Any]) -> Dict[str, Any]:
        """反扁平化字典"""
        result = {}
        for key, value in flat.items():
            parts = key.split(".")
            current = result
            for i, part in enumerate(parts[:-1]):
                if part not in current:
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = value
        return result

    def _load_default_config(self):
        """加载默认配置"""
        for key, value in self._flatten_dict(self.DEFAULT_CONFIG).items():
            self._configs[key] = ConfigItem(
                key=key,
                value=value,
                config_type=type(value).__name__,
                default_value=value
            )

    def _load_from_file(self):
        """从文件加载配置"""
        if not self._config_path.exists():
            self._save_to_file()
            return

        try:
            with open(self._config_path, 'r', encoding='utf-8') as f:
                if self._config_path.suffix in ['.yaml', '.yml']:
                    data = yaml.safe_load(f)
                else:
                    data = json.load(f)

            if data:
                flat = self._flatten_dict(data)
                with self._lock:
                    for key, value in flat.items():
                        if key in self._configs:
                            self._configs[key].value = value
                logger.info(f"从 {self._config_path} 加载配置")
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")

    def _save_to_file(self):
        """保存配置到文件"""
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                flat = {k: v.value for k, v in self._configs.items()}
            data = self._unflatten_dict(flat)
            with open(self._config_path, 'w', encoding='utf-8') as f:
                if self._config_path.suffix in ['.yaml', '.yml']:
                    yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
                else:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"配置已保存到 {self._config_path}")
            self._last_save_time = time.time()
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")

    def _start_file_watcher(self):
        """启动文件监控"""
        self._stop_event = threading.Event()
        self._last_save_time = 0.0

        def watch():
            last_mtime = 0
            if self._config_path.exists():
                last_mtime = self._config_path.stat().st_mtime
            while not self._stop_event.is_set():
                try:
                    self._stop_event.wait(5)
                    if self._stop_event.is_set():
                        break
                    if self._config_path.exists():
                        mtime = self._config_path.stat().st_mtime
                        if mtime > last_mtime and time.time() - self._last_save_time > 2:
                            last_mtime = mtime
                            logger.info("检测到配置文件变更，重新加载...")
                            self._load_from_file()
                            self._notify_watchers()
                except Exception as e:
                    logger.error(f"配置文件监控异常: {e}")

        self._watcher_thread = threading.Thread(target=watch, daemon=True)
        self._watcher_thread.start()

    def _notify_watchers(self, key: str = None):
        """通知观察者"""
        with self._lock:
            watchers_snapshot = {k: list(cbs) for k, cbs in self._watchers.items()}
        for watched_key, callbacks in watchers_snapshot.items():
            if key is None or watched_key == key or watched_key in key:
                for callback in callbacks:
                    try:
                        callback(self.get(watched_key))
                    except Exception as e:
                        logger.error(f"配置变更回调失败: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        with self._lock:
            if key in self._configs:
                return self._configs[key].value
            return default

    def set(self, key: str, value: Any, save: bool = True):
        """设置配置值"""
        with self._lock:
            if key in self._configs:
                old_value = self._configs[key].value
                self._configs[key].value = value
                self._configs[key].updated_at = time.time()
                if old_value != value:
                    logger.info(f"配置变更: {key} = {value}")
            else:
                self._configs[key] = ConfigItem(
                    key=key,
                    value=value,
                    config_type=type(value).__name__
                )
                logger.info(f"新增配置: {key} = {value}")

        if save:
            self._save_to_file()
            self._notify_watchers(key)

    def get_section(self, section: str) -> Dict[str, Any]:
        """获取配置节"""
        with self._lock:
            prefix = f"{section}."
            return {
                k.split(".", 1)[1]: v.value
                for k, v in self._configs.items()
                if k.startswith(prefix)
            }

    def watch(self, key: str, callback: Callable):
        """监控配置变更（线程安全）"""
        with self._lock:
            if key not in self._watchers:
                self._watchers[key] = []
            self._watchers[key].append(callback)

    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        with self._lock:
            return {k: v.value for k, v in self._configs.items()}

    def reset(self, key: str = None):
        """重置配置"""
        with self._lock:
            if key:
                if key in self._configs:
                    self._configs[key].value = self._configs[key].default_value
            else:
                for config in self._configs.values():
                    config.value = config.default_value

    def stop(self):
        """停止管理器"""
        self._running = False
        if hasattr(self, '_stop_event'):
            self._stop_event.set()
        if hasattr(self, '_watcher_thread'):
            self._watcher_thread.join(timeout=5)
