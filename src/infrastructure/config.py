# -*- coding: utf-8 -*-
"""
配置管理模块
提供企业级应用的配置管理功能
"""
import os
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse
from pydantic import BaseModel, Field, field_validator
from functools import lru_cache

from src.infrastructure.env_loader import load_project_env
from src.infrastructure.runtime_paths import get_base_dir, get_chroma_dir

load_project_env(base_dir=get_base_dir(), override=False)


def _get_env(*keys: str, default: str) -> str:
    for key in keys:
        value = os.getenv(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _default_local_origins() -> List[str]:
    port = _get_env("SERVER_PORT", "PORT", default="8023")
    return [
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
    ]


def _parse_database_url(url: str) -> Dict[str, Any]:
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("DATABASE_URL格式无效")
    database_name = parsed.path.lstrip("/")
    if not database_name:
        raise ValueError("DATABASE_URL缺少数据库名")
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": unquote(database_name),
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


def _parse_redis_url(url: str) -> Dict[str, Any]:
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("REDIS_URL格式无效")
    db_index = 0
    raw_db = parsed.path.lstrip("/")
    if raw_db:
        db_index = int(raw_db)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 6379,
        "password": unquote(parsed.password) if parsed.password is not None else None,
        "db": db_index,
    }


class DatabaseConfig(BaseModel):
    """数据库配置"""
    
    host: str = Field(default="localhost", description="数据库主机")
    port: int = Field(default=5432, description="数据库端口")
    database: str = Field(default="huoketest", description="数据库名称")
    username: str = Field(default="postgres", description="用户名")
    password: str = Field(default="", description="密码")
    pool_size: int = Field(default=10, description="连接池大小")
    max_overflow: int = Field(default=20, description="最大溢出连接数")
    pool_timeout: int = Field(default=30, description="连接池超时时间(秒)")
    echo: bool = Field(default=False, description="是否打印SQL")
    
    @property
    def url(self) -> str:
        """获取数据库连接URL"""
        from urllib.parse import quote_plus
        return f"postgresql://{self.username}:{quote_plus(self.password)}@{self.host}:{self.port}/{self.database}"
    
    @property
    def async_url(self) -> str:
        """获取异步数据库连接URL"""
        from urllib.parse import quote_plus
        return f"postgresql+asyncpg://{self.username}:{quote_plus(self.password)}@{self.host}:{self.port}/{self.database}"


class RedisConfig(BaseModel):
    """Redis配置"""
    
    host: str = Field(default="localhost", description="Redis主机")
    port: int = Field(default=6379, description="Redis端口")
    password: Optional[str] = Field(default=None, description="Redis密码")
    db: int = Field(default=0, description="Redis数据库索引")
    pool_size: int = Field(default=10, description="连接池大小")
    socket_timeout: int = Field(default=5, description="Socket超时时间(秒)")
    socket_connect_timeout: int = Field(default=5, description="连接超时时间(秒)")
    
    @property
    def url(self) -> str:
        """获取Redis连接URL"""
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class SecurityConfig(BaseModel):
    """安全配置"""
    
    secret_key: str = Field(
        default_factory=lambda: os.getenv("SECRET_KEY", ""),
        description="应用密钥（生产环境必须设置SECRET_KEY环境变量）"
    )
    algorithm: str = Field(default="HS256", description="JWT算法")
    access_token_expire_minutes: int = Field(default=30, description="访问令牌过期时间(分钟)")
    refresh_token_expire_days: int = Field(default=7, description="刷新令牌过期时间(天)")
    password_min_length: int = Field(default=8, description="密码最小长度")
    password_require_uppercase: bool = Field(default=True, description="密码是否需要大写字母")
    password_require_lowercase: bool = Field(default=True, description="密码是否需要小写字母")
    password_require_digit: bool = Field(default=True, description="密码是否需要数字")
    password_require_special: bool = Field(default=False, description="密码是否需要特殊字符")
    max_login_attempts: int = Field(default=5, description="最大登录尝试次数")
    lockout_duration_minutes: int = Field(default=30, description="锁定持续时间(分钟)")

    def __init__(self, **data):
        super().__init__(**data)
        if not self.secret_key:
            import logging
            env = os.getenv("ENVIRONMENT", "development")
            if env == "production":
                raise ValueError(
                    "生产环境必须设置SECRET_KEY环境变量！"
                    "请设置 SECRET_KEY 为一个强随机字符串。"
                )
            logging.getLogger(__name__).warning(
                "SECRET_KEY未设置！JWT令牌安全性无法保证。"
                "请设置环境变量 SECRET_KEY 为一个强随机字符串。"
            )


class EmbeddingConfig(BaseModel):
    """Embedding模型配置"""
    
    provider: str = Field(default="local", description="Embedding服务提供商")
    model_name: str = Field(default="BAAI/bge-small-zh-v1.5", description="模型名称")
    dimension: int = Field(default=512, description="向量维度")
    normalize: bool = Field(default=True, description="是否归一化")
    batch_size: int = Field(default=32, description="批处理大小")
    max_length: int = Field(default=512, description="最大序列长度")
    cache_enabled: bool = Field(default=True, description="是否启用缓存")
    cache_ttl: int = Field(default=3600, description="缓存过期时间(秒)")


class VectorStoreConfig(BaseModel):
    """向量存储配置"""
    
    type: str = Field(default="chroma", description="向量存储类型")
    persist_directory: str = Field(default_factory=lambda: str(get_chroma_dir()), description="持久化目录")
    collection_prefix: str = Field(default="enterprise", description="集合前缀")
    distance_metric: str = Field(default="cosine", description="距离度量方式")
    hnsw_m: int = Field(default=16, description="HNSW M参数")
    hnsw_ef_construction: int = Field(default=200, description="HNSW构建参数")


class LLMConfig(BaseModel):
    """LLM配置"""
    
    provider: str = Field(default="ollama", description="LLM提供商")
    model_name: str = Field(default="qwen2.5:7b", description="模型名称")
    base_url: str = Field(default="http://localhost:11434", description="API基础URL")
    api_key: Optional[str] = Field(default=None, description="API密钥")
    temperature: float = Field(default=0.7, description="温度参数")
    max_tokens: int = Field(default=2048, description="最大生成令牌数")
    top_p: float = Field(default=0.9, description="Top-p采样参数")
    timeout: int = Field(default=60, description="请求超时时间(秒)")


class ChunkingConfig(BaseModel):
    """分块配置"""
    
    chunk_size: int = Field(default=800, description="分块大小")
    chunk_overlap: int = Field(default=120, description="分块重叠")
    min_chunk_size: int = Field(default=200, description="最小分块大小")
    max_chunk_size: int = Field(default=1200, description="最大分块大小")
    semantic_threshold: float = Field(default=0.6, description="语义分块阈值")
    use_semantic_chunking: bool = Field(default=True, description="是否使用语义分块")


class RetrievalConfig(BaseModel):
    """检索配置"""
    
    top_k: int = Field(default=10, description="检索返回数量")
    similarity_threshold: float = Field(default=0.3, description="相似度阈值")
    keyword_weight: float = Field(default=0.2, description="关键词权重")
    use_hybrid_search: bool = Field(default=True, description="是否使用混合检索")
    use_reranking: bool = Field(default=True, description="是否使用重排序")
    rerank_top_k: int = Field(default=5, description="重排序后返回数量")


class CacheConfig(BaseModel):
    """缓存配置"""
    
    enabled: bool = Field(default=True, description="是否启用缓存")
    type: str = Field(default="memory", description="缓存类型")
    default_ttl: int = Field(default=3600, description="默认过期时间(秒)")
    max_size: int = Field(default=1000, description="最大缓存数量")
    redis_url: Optional[str] = Field(default=None, description="Redis URL")


class LoggingConfig(BaseModel):
    """日志配置"""
    
    level: str = Field(default="INFO", description="日志级别")
    log_dir: str = Field(default="logs", description="日志目录")
    json_format: bool = Field(default=True, description="是否使用JSON格式")
    rotation: str = Field(default="10 MB", description="日志轮转大小")
    retention: str = Field(default="7 days", description="日志保留时间")
    include_request_body: bool = Field(default=False, description="是否包含请求体")
    include_response_body: bool = Field(default=False, description="是否包含响应体")


class RateLimitConfig(BaseModel):
    """限流配置"""
    
    enabled: bool = Field(default=True, description="是否启用限流")
    requests_per_minute: int = Field(default=60, description="每分钟请求数限制")
    requests_per_hour: int = Field(default=1000, description="每小时请求数限制")
    burst_size: int = Field(default=10, description="突发请求数")


class BaseConfig(BaseModel):
    """应用基础配置"""
    
    app_name: str = Field(default="智能知识库系统", description="应用名称")
    app_version: str = Field(default="1.0.0", description="应用版本")
    debug: bool = Field(default=False, description="调试模式")
    environment: str = Field(default="development", description="运行环境")
    
    host: str = Field(default="127.0.0.1", description="服务主机")
    port: int = Field(default=8023, description="服务端口")
    workers: int = Field(default=1, description="工作进程数")
    
    cors_origins: List[str] = Field(
        default_factory=lambda: os.getenv("CORS_ORIGINS", ",".join(_default_local_origins())).split(","),
        description="CORS允许的源",
    )
    
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    
    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        """验证环境配置"""
        allowed = ["development", "testing", "staging", "production"]
        if v not in allowed:
            raise ValueError(f"环境必须是: {allowed}")
        return v
    
    @property
    def is_production(self) -> bool:
        """是否为生产环境"""
        return self.environment == "production"
    
    @property
    def is_development(self) -> bool:
        """是否为开发环境"""
        return self.environment == "development"


@lru_cache(maxsize=1)
def get_config() -> BaseConfig:
    """
    获取应用配置实例
    
    使用缓存避免重复加载
    """
    config_dict = {}

    database_url = os.getenv("DATABASE_URL")
    if database_url:
        config_dict["database"] = _parse_database_url(database_url)

    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        config_dict["redis"] = _parse_redis_url(redis_url)

    env_mappings = {
        "APP_NAME": ("app_name", str),
        "APP_VERSION": ("app_version", str),
        "DEBUG": ("debug", lambda x: x.lower() == "true"),
        "ENVIRONMENT": ("environment", str),
        "HOST": ("host", str),
        "PORT": ("port", int),
        "SERVER_HOST": ("host", str),
        "SERVER_PORT": ("port", int),
        "SECRET_KEY": (("security", "secret_key"), str),
        "LOG_LEVEL": (("logging", "level"), str),
        "EMBEDDING_PROVIDER": (("embedding", "provider"), str),
        "EMBEDDING_MODEL_NAME": (("embedding", "model_name"), str),
        "LLM_PROVIDER": (("llm", "provider"), str),
        "LLM_MODEL_NAME": (("llm", "model_name"), str),
    }
    
    for env_key, (config_path, converter) in env_mappings.items():
        env_value = os.getenv(env_key)
        if env_value:
            if isinstance(config_path, tuple):
                section, key = config_path
                if section not in config_dict:
                    config_dict[section] = {}
                config_dict[section][key] = converter(env_value) if callable(converter) else env_value
            else:
                config_dict[config_path] = converter(env_value) if callable(converter) else env_value
    
    return BaseConfig(**config_dict)


def reload_config() -> BaseConfig:
    """重新加载配置"""
    get_config.cache_clear()
    return get_config()
