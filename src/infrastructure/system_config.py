"""
统一配置管理模块

集中管理系统的所有配置，支持动态更新和热加载

配置类别:
1. RPA 配置
2. LLM 配置
3. RAG 配置
4. 缓存配置
5. 数据库配置
6. 日志配置

支持:
- 环境变量覆盖
- 配置文件加载 (YAML/JSON)
- 动态更新
- 配置验证
"""

import os
import json
import yaml
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from loguru import logger

from src.infrastructure.env_loader import load_project_env
from src.infrastructure.runtime_paths import get_base_dir, get_chroma_dir, get_models_dir

load_project_env(base_dir=get_base_dir(), override=False)


def _get_env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


@dataclass
class RPAConfig:
    """RPA 配置"""
    poll_interval: float = 0.5  # 轮询间隔 (秒)
    max_retries: int = 3  # 最大重试次数
    retry_delay: float = 1.0  # 重试延迟 (秒)
    headless: bool = False  # 无头模式
    timeout: int = 30000  # 超时时间 (毫秒)
    mutation_observer: bool = True  # 使用 MutationObserver


@dataclass
class LLMConfig:
    """LLM 配置"""
    provider: str = "ollama"  # ollama/aliyun/openai
    model_name: str = "qwen3.5:4b"
    base_url: str = "http://localhost:11434"
    api_key: str = ""
    timeout: int = 120  # 超时 (秒)
    temperature: float = 0.7
    max_tokens: int = 1000
    cache_ttl: int = 30  # 缓存 TTL(秒)
    keep_alive: str = "24h"


@dataclass
class EmbeddingConfig:
    """Embedding 配置"""
    provider: str = "local"
    model_name: str = "BAAI/bge-small-zh-v1.5"
    dimension: int = 512
    batch_size: int = 32
    cache_dir: str = str(get_models_dir() / "embedding")


@dataclass
class VectorStoreConfig:
    """向量存储配置"""
    type: str = "chroma"
    persist_directory: str = str(get_chroma_dir())
    collection_prefix: str = "enterprise_"
    distance_metric: str = "cosine"


@dataclass
class RAGConfig:
    """RAG 配置"""
    top_k: int = 5
    enable_reranking: bool = True
    use_llm_rerank: bool = True
    enable_semantic_dedup: bool = True
    dedup_threshold: float = 0.85
    relevance_threshold: float = 0.3
    score_threshold: float = 0.1


@dataclass
class CacheConfig:
    """缓存配置"""
    enabled: bool = True
    lru_max_size: int = 1000
    ttl: int = 300  # 秒
    message_dedup_ttl: int = 1800  # 消息去重 TTL(秒)
    intent_cache_ttl: int = 180  # 意图缓存 TTL(秒)


@dataclass
class DatabaseConfig:
    """数据库配置"""
    provider: str = "sqlite"
    host: str = "localhost"
    port: int = 5432
    database: str = "huoketest"
    username: str = "postgres"
    password: str = ""
    path: str = "data/huoketest.db"
    echo: bool = False
    pool_size: int = 5


@dataclass
class RedisConfig:
    """Redis 配置"""
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str = ""


@dataclass
class ServerConfig:
    """服务配置"""
    host: str = "127.0.0.1"
    port: int = 8023
    debug: bool = True


@dataclass
class LogConfig:
    """日志配置"""
    level: str = "INFO"
    format: str = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} - {message}"
    rotation: str = "10 MB"
    retention: str = "30 days"
    compression: str = "zip"
    file_path: str = "logs/huoketest_{time}.log"


@dataclass
class SystemConfig:
    """系统配置"""
    rpa: RPAConfig = field(default_factory=RPAConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    vector_store: VectorStoreConfig = field(default_factory=VectorStoreConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    log: LogConfig = field(default_factory=LogConfig)
    
    # 元数据
    version: str = "2.0.0"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


class ConfigManager:
    """
    配置管理器
    
    单例模式，提供统一的配置访问和更新接口
    """
    
    _instance: Optional['ConfigManager'] = None
    _config: SystemConfig = None
    
    def __new__(cls, *args, **kwargs) -> 'ConfigManager':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, config_path: str = "config/system_config.yaml"):
        resolved_path = Path(config_path)
        if getattr(self, "_config_path", None) != resolved_path:
            self._config_path = resolved_path
            self._config = None
        if self._config is None:
            self._config = SystemConfig()
            self._load_from_env()
            self.load_from_file(str(self._config_path))
            logger.info("配置管理器初始化成功")

    @staticmethod
    def _normalize_section_keys(data: Dict[str, Any], aliases: Dict[str, str]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        for key, value in dict(data or {}).items():
            normalized[aliases.get(key, key)] = value
        return normalized

    def _normalize_loaded_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        source = dict(data or {})

        llm_source = source.get("llm") or source.get("ollama") or source.get("qwen")
        if isinstance(llm_source, dict):
            llm_aliases = {
                "model": "model_name",
                "baseUrl": "base_url",
                "apiKey": "api_key",
                "maxTokens": "max_tokens",
                "keepAlive": "keep_alive",
                "cacheTtl": "cache_ttl",
            }
            normalized["llm"] = self._normalize_section_keys(llm_source, llm_aliases)
            if "ollama" in source and "provider" not in normalized["llm"]:
                normalized["llm"]["provider"] = "ollama"
            if "qwen" in source and "provider" not in normalized["llm"]:
                normalized["llm"]["provider"] = "qwen"

        if isinstance(source.get("embedding"), dict):
            normalized["embedding"] = self._normalize_section_keys(
                source["embedding"],
                {
                    "modelName": "model_name",
                    "batchSize": "batch_size",
                    "cacheDir": "cache_dir",
                },
            )

        vector_store_source = source.get("vector_store") or source.get("vectorStore")
        if isinstance(vector_store_source, dict):
            normalized["vector_store"] = self._normalize_section_keys(
                vector_store_source,
                {
                    "persistDirectory": "persist_directory",
                    "collectionPrefix": "collection_prefix",
                    "distanceMetric": "distance_metric",
                },
            )

        if isinstance(source.get("rpa"), dict):
            normalized["rpa"] = self._normalize_section_keys(
                source["rpa"],
                {
                    "pollInterval": "poll_interval",
                    "maxRetries": "max_retries",
                    "retryDelay": "retry_delay",
                    "headlessMode": "headless",
                },
            )

        if isinstance(source.get("rag"), dict):
            normalized["rag"] = dict(source["rag"])

        if isinstance(source.get("cache"), dict):
            normalized["cache"] = dict(source["cache"])

        if isinstance(source.get("database"), dict):
            normalized["database"] = self._normalize_section_keys(
                source["database"],
                {
                    "type": "provider",
                    "name": "database",
                    "user": "username",
                },
            )

        if isinstance(source.get("redis"), dict):
            normalized["redis"] = dict(source["redis"])

        if isinstance(source.get("server"), dict):
            normalized["server"] = dict(source["server"])

        logging_source = source.get("log") or source.get("logging")
        if isinstance(logging_source, dict):
            normalized["log"] = self._normalize_section_keys(
                logging_source,
                {
                    "file": "file_path",
                },
            )

        return normalized
    
    def _load_from_env(self):
        """从环境变量加载配置"""
        # LLM 配置
        if _get_env("LLM_PROVIDER"):
            self._config.llm.provider = _get_env("LLM_PROVIDER")
        if _get_env("LLM_MODEL_NAME"):
            self._config.llm.model_name = _get_env("LLM_MODEL_NAME")
        if _get_env("LLM_BASE_URL", "OLLAMA_HOST"):
            self._config.llm.base_url = _get_env("LLM_BASE_URL", "OLLAMA_HOST")
        if _get_env("LLM_API_KEY"):
            self._config.llm.api_key = _get_env("LLM_API_KEY")
        if _get_env("OLLAMA_KEEP_ALIVE"):
            self._config.llm.keep_alive = _get_env("OLLAMA_KEEP_ALIVE")
        
        if _get_env("EMBEDDING_PROVIDER"):
            self._config.embedding.provider = _get_env("EMBEDDING_PROVIDER")
        if _get_env("EMBEDDING_MODEL_NAME"):
            self._config.embedding.model_name = _get_env("EMBEDDING_MODEL_NAME")
        
        # 数据库配置
        if _get_env("DATABASE_PATH"):
            self._config.database.path = _get_env("DATABASE_PATH")
        if _get_env("SERVER_HOST", "HOST"):
            self._config.server.host = _get_env("SERVER_HOST", "HOST")
        if _get_env("SERVER_PORT", "PORT"):
            self._config.server.port = int(_get_env("SERVER_PORT", "PORT"))
        
        # 日志配置
        if _get_env("LOG_LEVEL"):
            self._config.log.level = _get_env("LOG_LEVEL")
        
        logger.debug("已从环境变量加载配置")
    
    def load_from_file(self, file_path: str):
        """从文件加载配置"""
        path = Path(file_path)
        
        if not path.exists():
            logger.warning(f"配置文件不存在：{file_path}，使用默认配置")
            return
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                if path.suffix in ['.yaml', '.yml']:
                    data = yaml.safe_load(f)
                elif path.suffix == '.json':
                    data = json.load(f)
                else:
                    logger.error(f"不支持的配置文件格式：{path.suffix}")
                    return
            
            self._update_config(self._normalize_loaded_config(data))
            logger.info(f"已从文件加载配置：{file_path}")
            
        except Exception as e:
            logger.error(f"加载配置文件失败：{e}")
    
    def _update_config(self, data: Dict[str, Any]):
        """更新配置"""
        for section_name in (
            "rpa",
            "llm",
            "embedding",
            "vector_store",
            "rag",
            "cache",
            "database",
            "redis",
            "server",
            "log",
        ):
            if section_name not in data:
                continue
            section = getattr(self._config, section_name, None)
            if section is None:
                continue
            for key, value in data[section_name].items():
                if hasattr(section, key):
                    setattr(section, key, value)
        
        self._config.updated_at = datetime.now().isoformat()
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        keys = key.split('.')
        
        config = self._config
        for k in keys:
            if hasattr(config, k):
                config = getattr(config, k)
            else:
                return default
        
        return config
    
    def set(self, key: str, value: Any):
        """设置配置值"""
        keys = key.split('.')
        
        if len(keys) == 1:
            logger.error(f"无效的配置键：{key}，格式应为：section.key")
            return
        
        section_name = keys[0]
        key_name = '.'.join(keys[1:])
        
        section = getattr(self._config, section_name, None)
        if section and hasattr(section, key_name):
            setattr(section, key_name, value)
            self._config.updated_at = datetime.now().isoformat()
            logger.info(f"配置已更新：{key} = {value}")
        else:
            logger.error(f"无效的配置键：{key}")
    
    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        return asdict(self._config)
    
    def save_to_file(self, file_path: str, format: str = 'yaml'):
        """保存配置到文件"""
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            data = asdict(self._config)
            
            with open(path, 'w', encoding='utf-8') as f:
                if format == 'yaml':
                    yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
                elif format == 'json':
                    json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"配置已保存到：{file_path}")
            
        except Exception as e:
            logger.error(f"保存配置文件失败：{e}")
    
    def validate(self) -> List[str]:
        """验证配置"""
        errors = []
        
        # LLM 配置验证
        if not self._config.llm.base_url:
            errors.append("LLM base_url 不能为空")
        
        if self._config.llm.timeout <= 0:
            errors.append("LLM timeout 必须大于 0")
        
        # RAG 配置验证
        if not (0 <= self._config.rag.dedup_threshold <= 1):
            errors.append("RAG ded重阈值必须在 0-1 之间")
        
        # 缓存配置验证
        if self._config.cache.ttl < 0:
            errors.append("缓存 TTL 不能为负数")
        
        if errors:
            logger.error(f"配置验证失败：{errors}")
        
        return errors
    
    def reload(self):
        """重新加载配置"""
        self._config = SystemConfig()
        self._load_from_env()
        if self._config_path.exists():
            self.load_from_file(str(self._config_path))
        logger.info("配置已重新加载")


def get_config_manager() -> ConfigManager:
    """获取配置管理器单例"""
    return ConfigManager()


def get_config(key: str, default: Any = None) -> Any:
    """便捷函数：获取配置值"""
    return ConfigManager().get(key, default)


def set_config(key: str, value: Any):
    """便捷函数：设置配置值"""
    ConfigManager().set(key, value)
