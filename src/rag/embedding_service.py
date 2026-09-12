"""
Embedding模型封装
支持本地BGE-M3和DashScope API两种方式
"""
import json
import os
import asyncio
import atexit
import threading
import concurrent.futures
from typing import List, Optional, Any, Dict
import numpy as np
from dataclasses import dataclass, field
import httpx
from loguru import logger

from src.infrastructure.model_loading import apply_local_model_only_env, is_local_model_only_enabled


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class EmbeddingConfig:
    """Embedding配置"""
    provider: str = "local"
    modelName: str = "BAAI/bge-small-zh-v1.5"
    dimension: int = 512
    batchSize: int = 32
    normalize: bool = True
    cacheDir: str = "models/embedding"
    localFilesOnly: bool = field(
        default_factory=lambda: _env_flag("EMBEDDING_LOCAL_FILES_ONLY", is_local_model_only_enabled())
    )
    allowRemoteDownload: bool = field(
        default_factory=lambda: _env_flag("EMBEDDING_ALLOW_REMOTE_DOWNLOAD", not is_local_model_only_enabled())
    )
    
    dashscopeApiKey: str = field(default_factory=lambda: os.getenv("DASHSCOPE_API_KEY", ""))
    dashscopeModel: str = "text-embedding-v2"
    
    fallbackModels: List[str] = field(default_factory=lambda: [
        "BAAI/bge-small-zh-v1.5",
        "BAAI/bge-large-zh-v1.5",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "sentence-transformers/all-MiniLM-L6-v2"
    ])


MODEL_DIMENSIONS: Dict[str, int] = {
    "BAAI/bge-small-zh-v1.5": 512,
    "BAAI/bge-large-zh-v1.5": 1024,
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "text-embedding-v2": 1536,
}


def build_embedding_config(
    provider: str = "local",
    model_name: str = "BAAI/bge-small-zh-v1.5",
    dimension: int = 512,
    normalize: bool = True,
    batch_size: int = 32,
    cache_dir: Optional[str] = None,
    dashscope_api_key: Optional[str] = None,
    dashscope_model: str = "text-embedding-v2",
    local_files_only: Optional[bool] = None,
    allow_remote_download: Optional[bool] = None,
) -> EmbeddingConfig:
    return EmbeddingConfig(
        provider=provider,
        modelName=model_name,
        dimension=dimension,
        normalize=normalize,
        batchSize=batch_size,
        cacheDir=cache_dir or _resolve_default_cache_dir(),
        localFilesOnly=is_local_model_only_enabled() if local_files_only is None else bool(local_files_only),
        allowRemoteDownload=(
            (not is_local_model_only_enabled()) if allow_remote_download is None else bool(allow_remote_download)
        ),
        dashscopeApiKey=dashscope_api_key or os.getenv("DASHSCOPE_API_KEY", ""),
        dashscopeModel=dashscope_model,
    )


def _resolve_default_cache_dir() -> str:
    from src.infrastructure.runtime_paths import get_models_embedding_dir

    return str(get_models_embedding_dir())


def build_embedding_config_from_app_config(app_config=None, cache_dir: Optional[str] = None) -> EmbeddingConfig:
    if app_config is None:
        from src.infrastructure.config import get_config

        app_config = get_config()
    return build_embedding_config(
        provider=app_config.embedding.provider,
        model_name=app_config.embedding.model_name,
        dimension=app_config.embedding.dimension,
        normalize=app_config.embedding.normalize,
        batch_size=app_config.embedding.batch_size,
        cache_dir=cache_dir or _resolve_default_cache_dir(),
        local_files_only=_env_flag("EMBEDDING_LOCAL_FILES_ONLY", is_local_model_only_enabled()),
        allow_remote_download=_env_flag("EMBEDDING_ALLOW_REMOTE_DOWNLOAD", not is_local_model_only_enabled()),
        dashscope_api_key=os.getenv("DASHSCOPE_API_KEY", ""),
        dashscope_model=os.getenv("DASHSCOPE_MODEL", "text-embedding-v2"),
    )


class LocalEmbedding:
    """
    本地Embedding模型
    
    使用BGE-M3进行文本向量化
    支持备用模型自动切换
    """
    
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._model = None
        self._actual_model_name = None
        self._actual_dimension = config.dimension
        self._init_failed = False
        self._encode_lock = threading.Lock()
        self._initModel()
    
    def _getLocalModelPaths(self, model_name: str) -> List[str]:
        """
        获取本地模型可能的路径列表
        
        Args:
            model_name: 模型名称，如 'sentence-transformers/all-MiniLM-L6-v2'
            
        Returns:
            可能的本地路径列表
        """
        paths = []
        
        if "/" in model_name:
            model_short_name = model_name.split("/")[-1]
            paths.append(os.path.join(self.config.cacheDir, model_short_name))
        
        paths.append(os.path.join(self.config.cacheDir, model_name.replace("/", "_")))
        
        paths.append(os.path.join(self.config.cacheDir, model_name))
        
        return paths
    
    def _initModel(self):
        """
        初始化模型
        支持备用模型自动切换
        """
        apply_local_model_only_env()

        local_paths = self._getLocalModelPaths(self.config.modelName)
        for local_model_path in local_paths:
            if os.path.exists(local_model_path):
                try:
                    logger.info(f"从本地目录加载嵌入模型: {local_model_path}")
                    self._model = self._load_sentence_transformer_backend(
                        local_model_path,
                        local_files_only=True,
                    )
                    self._actual_model_name = self.config.modelName
                    self._actual_dimension = self._resolve_model_dimension(self._model, self.config.modelName)
                    logger.info(f"嵌入模型加载成功: {self._actual_model_name}")
                    return
                except Exception as e:
                    logger.warning(f"本地目录模型加载失败: {e}")
                    try:
                        self._model = self._load_transformers_backend(
                            local_model_path,
                            local_files_only=True,
                        )
                        self._actual_model_name = self.config.modelName
                        self._actual_dimension = self._resolve_model_dimension(self._model, self.config.modelName)
                        logger.info(f"已切换到 transformers 本地后备加载: {self._actual_model_name}")
                        return
                    except Exception as fallback_error:
                        logger.warning(f"transformers 本地后备加载失败: {fallback_error}")
        
        models_to_try = [self.config.modelName] + self._get_compatible_fallback_models()
        
        last_error = None
        for model_name in models_to_try:
            try:
                logger.info(f"尝试加载嵌入模型: {model_name}")
                
                local_paths = self._getLocalModelPaths(model_name)
                for local_path in local_paths:
                    if os.path.exists(local_path):
                        try:
                            self._model = self._load_sentence_transformer_backend(
                                local_path,
                                local_files_only=True,
                            )
                            logger.info(f"从本地缓存加载嵌入模型成功: {local_path}")
                            self._actual_model_name = model_name
                            self._actual_dimension = self._resolve_model_dimension(self._model, model_name)
                            return
                        except Exception as e:
                            logger.warning(f"本地缓存模型损坏: {e}")
                            try:
                                self._model = self._load_transformers_backend(
                                    local_path,
                                    local_files_only=True,
                                )
                                logger.info(f"transformers 本地缓存后备加载成功: {local_path}")
                                self._actual_model_name = model_name
                                self._actual_dimension = self._resolve_model_dimension(self._model, model_name)
                                return
                            except Exception as fallback_error:
                                logger.warning(f"transformers 本地缓存后备加载失败: {fallback_error}")
                
                if self.config.localFilesOnly or not self.config.allowRemoteDownload:
                    logger.info(f"尝试离线加载嵌入模型缓存: {model_name}")
                    try:
                        self._model = self._load_sentence_transformer_backend(
                            model_name,
                            cache_folder=self.config.cacheDir,
                            local_files_only=True,
                        )
                    except Exception as st_error:
                        logger.warning(f"SentenceTransformer 离线加载失败，尝试 transformers 后备模式: {st_error}")
                        self._model = self._load_transformers_backend(
                            model_name,
                            cache_folder=self.config.cacheDir,
                            local_files_only=True,
                        )
                else:
                    logger.info(f"从HuggingFace下载模型: {model_name}")
                    try:
                        self._model = self._load_sentence_transformer_backend(
                            model_name,
                            cache_folder=self.config.cacheDir,
                        )
                    except Exception as st_error:
                        logger.warning(f"SentenceTransformer 在线加载失败，尝试 transformers 后备模式: {st_error}")
                        self._model = self._load_transformers_backend(
                            model_name,
                            cache_folder=self.config.cacheDir,
                        )
                
                local_save_path = self._getLocalModelPaths(model_name)[0]
                os.makedirs(os.path.dirname(local_save_path) if os.path.dirname(local_save_path) else local_save_path, exist_ok=True)
                if hasattr(self._model, "save"):
                    self._model.save(local_save_path)
                    logger.info(f"嵌入模型下载并保存到: {local_save_path}")
                self._actual_model_name = model_name
                self._actual_dimension = self._resolve_model_dimension(self._model, model_name)
                return
                
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_name} 加载失败: {e}")
                continue
        
        offline_hint = "（当前为本地/离线模式，请先将模型放入本地缓存目录）" if (self.config.localFilesOnly or not self.config.allowRemoteDownload) else ""
        logger.error(f"所有嵌入模型加载失败，最后错误: {last_error}{offline_hint}，将使用随机向量降级模式")
        self._init_failed = True
        self._actual_model_name = "fallback_random"
        self._actual_dimension = self.config.dimension

    def _load_sentence_transformer_backend(
        self,
        model_ref: str,
        cache_folder: Optional[str] = None,
        local_files_only: bool = False,
    ):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError("请安装sentence-transformers: pip install sentence-transformers")
        return SentenceTransformer(
            model_ref,
            cache_folder=cache_folder,
            local_files_only=local_files_only,
        )

    def _load_transformers_backend(
        self,
        model_ref: str,
        cache_folder: Optional[str] = None,
        local_files_only: bool = False,
    ):
        return _TransformersMeanPoolingBackend(
            model_ref=model_ref,
            cache_folder=cache_folder,
            local_files_only=local_files_only,
            normalize=self.config.normalize,
            dimension=self.config.dimension,
        )

    def _get_compatible_fallback_models(self) -> List[str]:
        compatible_models: List[str] = []
        skipped_models: List[str] = []
        expected_dimension = int(self.config.dimension)

        for model_name in self.config.fallbackModels:
            if model_name == self.config.modelName:
                continue
            model_dimension = MODEL_DIMENSIONS.get(model_name)
            if model_dimension is not None and int(model_dimension) != expected_dimension:
                skipped_models.append(f"{model_name}({model_dimension})")
                continue
            compatible_models.append(model_name)

        if skipped_models:
            logger.warning(
                "已跳过维度不兼容的备用嵌入模型: "
                f"expected={expected_dimension}, skipped={', '.join(skipped_models)}"
            )
        return compatible_models

    def _resolve_model_dimension(self, model, model_name: str) -> int:
        dimension = None
        if hasattr(model, "get_embedding_dimension"):
            try:
                dimension = int(model.get_embedding_dimension())
            except Exception:
                dimension = None
        if dimension is None and hasattr(model, "get_sentence_embedding_dimension"):
            try:
                dimension = int(model.get_sentence_embedding_dimension())
            except Exception:
                dimension = None
        if dimension is None:
            dimension = MODEL_DIMENSIONS.get(model_name)
        if dimension is None:
            raise RuntimeError(f"无法识别嵌入模型维度: {model_name}")
        return int(dimension)
    
    def get_model_name(self) -> str:
        """获取实际使用的模型名称"""
        return self._actual_model_name or self.config.modelName

    def get_output_dimension(self) -> int:
        """获取实际输出维度"""
        return int(self._actual_dimension or self.config.dimension)
    
    def embed(self, texts: List[str]) -> np.ndarray:
        """
        文本向量化（线程安全）
        
        Args:
            texts: 文本列表
        
        Returns:
            向量矩阵 (n, dimension)
        """
        if self._init_failed:
            raise RuntimeError(
                f"嵌入模型未加载(model={self.config.modelName})，无法生成向量，请检查模型文件"
            )
        
        if not self._model:
            raise RuntimeError("模型未初始化")
        
        with self._encode_lock:
            embeddings = self._model.encode(
                texts,
                batch_size=self.config.batchSize,
                normalize_embeddings=self.config.normalize,
                show_progress_bar=False
            )
        array = np.array(embeddings)
        return self._validate_embeddings(array)

    def _validate_embeddings(self, embeddings: np.ndarray) -> np.ndarray:
        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)
        actual_dimension = embeddings.shape[1] if embeddings.size else self.get_output_dimension()
        if actual_dimension != self.get_output_dimension():
            raise RuntimeError(
                f"嵌入模型输出维度异常: expected={self.get_output_dimension()}, actual={actual_dimension}, model={self.get_model_name()}"
            )
        return embeddings
    
    def embedSingle(self, text: str) -> np.ndarray:
        if self._init_failed:
            raise RuntimeError(
                f"嵌入模型未加载(model={self.config.modelName})，无法生成向量，请检查模型文件"
            )
        return self.embed([text])[0]
    
    def close(self):
        """释放资源"""
        self._model = None


class _TransformersMeanPoolingBackend:
    """使用 transformers + torch 的轻量后备嵌入后端。"""

    def __init__(
        self,
        model_ref: str,
        cache_folder: Optional[str],
        local_files_only: bool,
        normalize: bool,
        dimension: int,
    ):
        import torch

        self._torch = torch
        self._normalize = bool(normalize)
        self._dimension = int(dimension)
        self._tokenizer, self._model = self._load_backend(
            model_ref=model_ref,
            cache_folder=cache_folder,
            local_files_only=local_files_only,
        )
        self._model.eval()
        hidden_size = int(getattr(getattr(self._model, "config", None), "hidden_size", self._dimension))
        self._dimension = hidden_size
        self._model_ref = model_ref

    def _load_backend(
        self,
        model_ref: str,
        cache_folder: Optional[str],
        local_files_only: bool,
    ):
        try:
            return self._load_auto_backend(
                model_ref=model_ref,
                cache_folder=cache_folder,
                local_files_only=local_files_only,
            )
        except Exception as auto_error:
            logger.warning(f"AutoModel 后备加载失败，尝试显式模型类加载: {auto_error}")
            return self._load_explicit_backend(
                model_ref=model_ref,
                cache_folder=cache_folder,
                local_files_only=local_files_only,
                original_error=auto_error,
            )

    def _load_auto_backend(
        self,
        model_ref: str,
        cache_folder: Optional[str],
        local_files_only: bool,
    ):
        from transformers import AutoModel, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            model_ref,
            cache_dir=cache_folder,
            local_files_only=local_files_only,
        )
        model = AutoModel.from_pretrained(
            model_ref,
            cache_dir=cache_folder,
            local_files_only=local_files_only,
        )
        return tokenizer, model

    def _load_explicit_backend(
        self,
        model_ref: str,
        cache_folder: Optional[str],
        local_files_only: bool,
        original_error: Exception,
    ):
        model_type = self._detect_model_type(model_ref)
        if model_type == "bert":
            tokenizer_cls, model_cls = self._get_explicit_bert_classes()
            tokenizer = tokenizer_cls.from_pretrained(
                model_ref,
                cache_dir=cache_folder,
                local_files_only=local_files_only,
            )
            model = model_cls.from_pretrained(
                model_ref,
                cache_dir=cache_folder,
                local_files_only=local_files_only,
            )
            return tokenizer, model
        raise original_error

    def _detect_model_type(self, model_ref: str) -> str:
        if not os.path.isdir(model_ref):
            return ""
        config_path = os.path.join(model_ref, "config.json")
        if not os.path.exists(config_path):
            return ""
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                config = json.load(fh)
        except Exception:
            return ""
        return str(config.get("model_type", "") or "").strip().lower()

    def _get_explicit_bert_classes(self):
        try:
            from transformers.models.bert.tokenization_bert_fast import BertTokenizerFast

            tokenizer_cls = BertTokenizerFast
        except Exception:
            from transformers.models.bert.tokenization_bert import BertTokenizer

            tokenizer_cls = BertTokenizer
        from transformers.models.bert.modeling_bert import BertModel

        return tokenizer_cls, BertModel

    def _mean_pool(self, token_embeddings, attention_mask):
        mask = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        masked_embeddings = token_embeddings * mask
        sum_embeddings = masked_embeddings.sum(dim=1)
        sum_mask = mask.sum(dim=1).clamp(min=1e-9)
        return sum_embeddings / sum_mask

    def encode(
        self,
        texts: List[str],
        batch_size: int = 32,
        normalize_embeddings: bool = True,
        show_progress_bar: bool = False,
    ):
        del show_progress_bar
        all_embeddings = []
        for start in range(0, len(texts), max(int(batch_size), 1)):
            batch = texts[start:start + max(int(batch_size), 1)]
            encoded = self._tokenizer(
                batch,
                padding=True,
                truncation=True,
                return_tensors="pt",
            )
            with self._torch.no_grad():
                outputs = self._model(**encoded)
            embeddings = self._mean_pool(outputs.last_hidden_state, encoded["attention_mask"])
            if self._normalize and normalize_embeddings:
                embeddings = self._torch.nn.functional.normalize(embeddings, p=2, dim=1)
            all_embeddings.append(embeddings.cpu().numpy())

        if not all_embeddings:
            return np.empty((0, self.get_sentence_embedding_dimension()), dtype=np.float32)
        return np.vstack(all_embeddings)

    def get_sentence_embedding_dimension(self) -> int:
        return int(self._dimension)

    def get_model_name(self) -> str:
        return self._model_ref

    def close(self):
        self._model = None
        self._tokenizer = None


class DashScopeEmbedding:
    """
    DashScope Embedding API
    使用阿里云DashScope进行文本向量化
    """
    
    _instances: List[Any] = []
    _instances_lock = threading.Lock()

    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._baseUrl = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
        self._client: Optional[httpx.AsyncClient] = None
        self._closed = False
        self._client_lock = threading.Lock()
        self._actual_dimension = MODEL_DIMENSIONS.get(self.config.dashscopeModel, self.config.dimension)
        with DashScopeEmbedding._instances_lock:
            DashScopeEmbedding._instances.append(self)
    
    def _ensure_client(self):
        """确保客户端已初始化（线程安全）"""
        if self._client is None and not self._closed:
            with self._client_lock:
                if self._client is None and not self._closed:
                    self._client = httpx.AsyncClient(timeout=60.0)
    
    async def embed(self, texts: List[str]) -> np.ndarray:
        """
        文本向量化
        
        Args:
            texts: 文本列表
        
        Returns:
            向量矩阵 (n, dimension)
        """
        if self._closed:
            raise RuntimeError("客户端已关闭")
        
        self._ensure_client()
        
        allEmbeddings = []
        
        for i in range(0, len(texts), self.config.batchSize):
            batch = texts[i:i + self.config.batchSize]
            
            headers = {
                "Authorization": f"Bearer {self.config.dashscopeApiKey}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": self.config.dashscopeModel,
                "input": {
                    "texts": batch
                },
                "parameters": {
                    "text_type": "document"
                }
            }
            
            response = await self._client.post(
                self._baseUrl,
                headers=headers,
                json=payload
            )
            
            if response.status_code == 200:
                result = response.json()
                if not isinstance(result, dict):
                    raise Exception(f"Embedding API返回非字典类型: {type(result)}")
                output = result.get("output")
                if not output or not isinstance(output, dict):
                    raise Exception(f"Embedding API返回缺少output字段: {list(result.keys())}")
                embeddings_data = output.get("embeddings")
                if not embeddings_data or not isinstance(embeddings_data, list):
                    raise Exception(f"Embedding API返回缺少embeddings列表: {list(output.keys())}")
                batchEmbeddings = []
                for item in embeddings_data:
                    if not isinstance(item, dict) or "embedding" not in item:
                        raise Exception(f"Embedding API返回的embedding项格式错误: {type(item)}")
                    embedding = item["embedding"]
                    if self._actual_dimension is None:
                        self._actual_dimension = len(embedding)
                    if len(embedding) != self.get_output_dimension():
                        raise RuntimeError(
                            f"DashScope返回维度异常: expected={self.get_output_dimension()}, actual={len(embedding)}, model={self.get_model_name()}"
                        )
                    batchEmbeddings.append(embedding)
                allEmbeddings.extend(batchEmbeddings)
            else:
                raise Exception(f"Embedding API调用失败: {response.text}")
        
        return np.array(allEmbeddings)
    
    async def embedSingle(self, text: str) -> np.ndarray:
        """单个文本向量化"""
        result = await self.embed([text])
        return result[0]
    
    async def close(self):
        """关闭客户端释放资源"""
        if self._client and not self._closed:
            await self._client.aclose()
            self._client = None
            self._closed = True
        with DashScopeEmbedding._instances_lock:
            if self in DashScopeEmbedding._instances:
                DashScopeEmbedding._instances.remove(self)

    def __del__(self):
        """析构时确保资源释放"""
        if self._client and not self._closed:
            try:
                try:
                    loop = asyncio.get_running_loop()
                    if loop.is_running():
                        logger.debug("DashScope客户端在运行中的事件循环内析构，资源可能延迟释放")
                    else:
                        loop.run_until_complete(self.close())
                except RuntimeError:
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        executor.submit(asyncio.run, self.close()).result(timeout=5)
            except Exception as e:
                logger.debug(f"关闭DashScope客户端失败: {e}")

    def get_model_name(self) -> str:
        return self.config.dashscopeModel

    def get_output_dimension(self) -> int:
        if self._actual_dimension is not None:
            return int(self._actual_dimension)
        inferred = MODEL_DIMENSIONS.get(self.config.dashscopeModel)
        if inferred is not None:
            return int(inferred)
        return int(self.config.dimension)


@atexit.register
def _cleanup_dashscope_clients():
    """程序退出时清理所有DashScope客户端"""
    import concurrent.futures
    with DashScopeEmbedding._instances_lock:
        instances = list(DashScopeEmbedding._instances)
    for instance in instances:
        try:
            if instance._client and not instance._closed:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(asyncio.run, instance.close()).result(timeout=5)
        except Exception as e:
            logger.debug(f"清理DashScope客户端失败: {e}")


class EmbeddingService:
    """
    Embedding服务
    统一封装本地和API两种方式
    """

    def __init__(self, config: Optional[EmbeddingConfig] = None):
        self.config = config or build_embedding_config_from_app_config()
        from src.common.utils import get_thread_pool_manager
        self._sync_executor = get_thread_pool_manager().get_or_create("embed_sync", max_workers=2)

        if self.config.provider == "local":
            self._embedding = LocalEmbedding(self.config)
            self._isAsync = False
        else:
            self._embedding = DashScopeEmbedding(self.config)
            self._isAsync = True
        self._validate_runtime_contract()

    def _validate_runtime_contract(self):
        actual_dimension = self.get_output_dimension()
        if actual_dimension != int(self.config.dimension):
            raise RuntimeError(
                f"Embedding配置维度与实际模型不匹配: configured={self.config.dimension}, actual={actual_dimension}, "
                f"model={self.get_model_name()}, provider={self.config.provider}"
            )

    def embed(self, texts: List[str]) -> np.ndarray:
        """文本向量化(同步，复用线程池)"""
        if self._isAsync:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    future = self._sync_executor.submit(
                        asyncio.run,
                        self._embedding.embed(texts)
                    )
                    return future.result(timeout=60)
                else:
                    return loop.run_until_complete(self._embedding.embed(texts))
            except RuntimeError:
                return asyncio.run(self._embedding.embed(texts))
        return self._embedding.embed(texts)
    
    def embed_query(self, query: str) -> List[float]:
        """
        查询文本向量化
        
        Args:
            query: 查询文本
            
        Returns:
            向量列表
        """
        embedding = self.embedSingle(query)
        return embedding.tolist()
    
    def embed_documents(self, documents: List[str]) -> List[List[float]]:
        """
        文档批量向量化
        
        Args:
            documents: 文档列表
            
        Returns:
            向量列表
        """
        embeddings = self.embed(documents)
        return embeddings.tolist()
    
    async def embedAsync(self, texts: List[str]) -> np.ndarray:
        """文本向量化(异步)"""
        if self._isAsync:
            return await self._embedding.embed(texts)
        return self._embedding.embed(texts)
    
    def embedSingle(self, text: str) -> np.ndarray:
        """单个文本向量化(同步，复用线程池)"""
        if self._isAsync:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    future = self._sync_executor.submit(
                        asyncio.run,
                        self._embedding.embedSingle(text)
                    )
                    return future.result(timeout=30)
                else:
                    return loop.run_until_complete(self._embedding.embedSingle(text))
            except RuntimeError:
                return asyncio.run(self._embedding.embedSingle(text))
        return self._embedding.embedSingle(text)
    
    async def embedSingleAsync(self, text: str) -> np.ndarray:
        """单个文本向量化(异步)"""
        if self._isAsync:
            return await self._embedding.embedSingle(text)
        return self._embedding.embedSingle(text)
    
    @staticmethod
    def similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
        """计算余弦相似度"""
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return float(np.dot(vec1, vec2) / (norm1 * norm2))
    
    def get_model_name(self) -> str:
        """获取实际使用的模型名称"""
        if hasattr(self._embedding, 'get_model_name'):
            return self._embedding.get_model_name()
        return self.config.modelName

    def get_output_dimension(self) -> int:
        if hasattr(self._embedding, 'get_output_dimension'):
            return int(self._embedding.get_output_dimension())
        return int(self.config.dimension)

    def get_runtime_contract(self) -> Dict[str, Any]:
        return {
            "provider": self.config.provider,
            "configured_model": self.config.modelName,
            "actual_model": self.get_model_name(),
            "configured_dimension": int(self.config.dimension),
            "actual_dimension": self.get_output_dimension(),
        }
    
    async def close(self):
        """关闭服务释放资源"""
        if hasattr(self._embedding, 'close'):
            if asyncio.iscoroutinefunction(self._embedding.close):
                await self._embedding.close()
            else:
                self._embedding.close()


_embedding_service_instance = None
_embedding_service_lock = __import__('threading').Lock()


def _embedding_config_signature(config: Optional[EmbeddingConfig]) -> Optional[tuple]:
    if config is None:
        return None
    return (
        config.provider,
        config.modelName,
        int(config.dimension),
        bool(config.normalize),
        int(config.batchSize),
        config.cacheDir,
        config.dashscopeModel,
    )


def get_embedding_service(config: Optional[EmbeddingConfig] = None, reset: bool = False) -> EmbeddingService:
    """
    获取嵌入服务单例

    Args:
        config: Embedding配置
        reset: 是否重置单例

    Returns:
        EmbeddingService实例
    """
    global _embedding_service_instance

    requested_config = config or build_embedding_config_from_app_config()

    if _embedding_service_instance is None or reset:
        with _embedding_service_lock:
            if _embedding_service_instance is None or reset:
                _embedding_service_instance = EmbeddingService(requested_config)
                logger.info(f"创建新的嵌入服务实例，模型: {_embedding_service_instance.get_model_name()}")
    else:
        existing_signature = _embedding_config_signature(_embedding_service_instance.config)
        requested_signature = _embedding_config_signature(requested_config)
        if existing_signature != requested_signature:
            raise RuntimeError(
                "已存在的Embedding服务配置与请求配置不一致，需先 reset_embedding_service() 后再切换模型"
            )

    return _embedding_service_instance


def reset_embedding_service():
    """
    重置嵌入服务单例（线程安全）
    用于模型加载失败后重新初始化
    """
    global _embedding_service_instance
    with _embedding_service_lock:
        _embedding_service_instance = None
    logger.info("嵌入服务单例已重置")
