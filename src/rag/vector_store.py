"""
Chroma向量数据库封装
支持多企业数据隔离、批量操作、高级检索
"""

import os
import time
import threading
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from loguru import logger

from src.common.industry_schema_service import get_industry_schema_service, get_active_schema_with_compat
from src.infrastructure.runtime_paths import get_chroma_dir

from functools import lru_cache

_chroma_client_path = None


@lru_cache(maxsize=1)
def get_chroma_client(persist_directory: Optional[str] = None):
    """
    获取ChromaDB客户端单例

    Args:
        persist_directory: 持久化目录

    Returns:
        ChromaDB客户端实例
    """
    global _chroma_client_path

    resolved_directory = persist_directory or str(get_chroma_dir())

    try:
        import chromadb
        from chromadb.config import Settings

        client = chromadb.PersistentClient(
            path=resolved_directory,
            settings=Settings(
                anonymized_telemetry=False,
                allow_reset=True
            )
        )
        _chroma_client_path = resolved_directory
        logger.info(f"ChromaDB客户端单例初始化成功，数据目录: {resolved_directory}")
        return client
    except Exception as e:
        if "already exists" in str(e).lower():
            import chromadb
            from chromadb.config import Settings
            client = chromadb.PersistentClient(
                path=resolved_directory,
                settings=Settings(anonymized_telemetry=False)
            )
            _chroma_client_path = resolved_directory
            logger.info(f"ChromaDB客户端复用成功，数据目录: {resolved_directory}")
            return client
        logger.error(f"ChromaDB客户端初始化失败: {e}")
        raise


@dataclass
class ChromaConfig:
    """
    Chroma配置
    
    Attributes:
        persistDirectory: 持久化目录
        collectionPrefix: 集合名称前缀
        distanceMetric: 距离度量 (cosine, l2, ip)
        hnswConfig: HNSW索引配置
    """
    persistDirectory: str = field(default_factory=lambda: str(get_chroma_dir()))
    collectionPrefix: str = "enterprise_"
    distanceMetric: str = "cosine"
    hnswConfig: Dict = field(default_factory=lambda: {
        "hnsw:space": "cosine",
        "hnsw:construction_ef": 200,
        "hnsw:M": 16
    })


class ChromaVectorStore:
    """
    Chroma向量存储
    
    功能特性：
    - 多企业数据隔离
    - 批量操作优化
    - 高级检索（过滤、分页）
    - 健康检查
    """
    
    GENERIC_QUERY_EXPANSIONS = {
        "价格": ["费用", "多少钱", "报价"],
        "费用": ["价格", "收费", "报价"],
        "功能": ["作用", "能力", "特点"],
        "服务": ["支持", "售后", "帮助"],
        "联系": ["联系方式", "电话", "微信"],
        "购买": ["下单", "订购", "报名"],
        "退款": ["退费", "退钱", "退订"],
        "发货": ["配送", "快递", "物流"],
    }

    TOURISM_QUERY_EXPANSIONS = {}

    TOURISM_SIGNAL_TERMS = ()

    def __init__(self, config: Optional[ChromaConfig] = None, embeddingService = None):
        """
        初始化向量存储
        
        Args:
            config: Chroma配置
            embeddingService: Embedding服务实例
        """
        self.config = config or ChromaConfig()
        self.embedding = embeddingService
        
        os.makedirs(self.config.persistDirectory, exist_ok=True)
        
        self._collections: Dict[str, Any] = {}
        self._collections_lock = threading.RLock()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._last_health_check = 0
        self._health_check_interval = 60

    def _get_active_schema(self, enterprise_id: str = ""):
        try:
            return get_active_schema_with_compat(
                get_industry_schema_service(),
                enterprise_id=enterprise_id,
            )
        except Exception:
            return {}

    def _get_scenic_aliases(self, enterprise_id: str = "") -> Dict:
        schema = self._get_active_schema(enterprise_id)
        try:
            return schema.get("metadata", {}).get("domain_scenic_aliases") or {}
        except Exception:
            return {}

    def _get_domain_keywords(self, enterprise_id: str = "") -> Tuple:
        schema = self._get_active_schema(enterprise_id)
        try:
            keywords = schema.get("metadata", {}).get("query_understanding", {}).get("domain_keywords") or []
            return tuple(keywords) if keywords else ()
        except Exception:
            return ()

    def _get_query_expansion_config(self, enterprise_id: str = "") -> Dict[str, Any]:
        schema = self._get_active_schema(enterprise_id)
        try:
            metadata = schema.get("metadata") or {}
            config = metadata.get("query_expansion") or {}
            return config if isinstance(config, dict) else {}
        except Exception:
            return {}

    def _get_runtime_flags(self, enterprise_id: str = "") -> Dict[str, Any]:
        schema = self._get_active_schema(enterprise_id)
        try:
            metadata = schema.get("metadata") or {}
            runtime_flags = metadata.get("runtime_flags") or {}
            return runtime_flags if isinstance(runtime_flags, dict) else {}
        except Exception:
            return {}

    def _sanitize_metadata_value(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:
                pass
        if isinstance(value, (dict, list, tuple, set)):
            try:
                import json
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return str(value)
        return str(value)

    def _sanitize_metadatas(self, metadatas: List[Dict]) -> List[Dict]:
        sanitized = []
        for metadata in metadatas:
            safe_metadata = {}
            for key, value in (metadata or {}).items():
                safe_metadata[str(key)] = self._sanitize_metadata_value(value)
            sanitized.append(safe_metadata)
        return sanitized
    
    def _initClient(self):
        """延迟初始化客户端（线程安全）"""
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            try:
                self._client = get_chroma_client(self.config.persistDirectory)
                self._initialized = True
            except ImportError:
                raise ImportError("请安装chromadb: pip install chromadb")
            except Exception as e:
                logger.error(f"Chroma客户端初始化失败: {e}")
                raise

    def _get_embedding_contract(self) -> Dict[str, Any]:
        if not self.embedding:
            return {}
        model_name = self.embedding.get_model_name() if hasattr(self.embedding, "get_model_name") else None
        dimension = self.embedding.get_output_dimension() if hasattr(self.embedding, "get_output_dimension") else None
        contract = {}
        if model_name:
            contract["embedding_model"] = model_name
        if dimension is not None:
            contract["embedding_dimension"] = int(dimension)
        return contract

    def _infer_collection_dimension(self, collection) -> Optional[int]:
        try:
            sample = collection.get(limit=1, include=["embeddings"])
            embeddings = sample.get("embeddings")
            if embeddings is not None and len(embeddings) > 0:
                return int(len(embeddings[0]))
        except Exception as e:
            logger.debug(f"推断集合维度失败: {e}")
        return None

    def _update_collection_metadata(self, collection, metadata: Dict[str, Any]):
        safe_existing_metadata = {
            "enterprise_id": metadata.get("enterprise_id"),
            "embedding_model": metadata.get("embedding_model"),
            "embedding_dimension": metadata.get("embedding_dimension"),
        }
        safe_existing_metadata = {
            key: value
            for key, value in safe_existing_metadata.items()
            if value is not None
        }
        try:
            collection.modify(metadata=safe_existing_metadata)
        except Exception as e:
            logger.warning(f"更新集合元数据失败: {e}")

    def _ensure_collection_contract(self, collection, enterpriseId: str):
        metadata = dict(collection.metadata) if collection.metadata else {}
        expected_contract = self._get_embedding_contract()
        if not expected_contract:
            return

        collection_dimension = metadata.get("embedding_dimension")
        if collection_dimension is None:
            collection_dimension = self._infer_collection_dimension(collection)
            if collection_dimension is None:
                collection_dimension = expected_contract.get("embedding_dimension")
        if collection_dimension is not None:
            collection_dimension = int(collection_dimension)

        expected_dimension = expected_contract.get("embedding_dimension")
        if collection_dimension is not None and expected_dimension is not None and collection_dimension != expected_dimension:
            raise RuntimeError(
                f"向量集合维度与当前Embedding模型不匹配: enterprise={enterpriseId}, "
                f"collection_dimension={collection_dimension}, expected_dimension={expected_dimension}, "
                f"model={expected_contract.get('embedding_model')}"
            )

        needs_update = False
        for key, value in expected_contract.items():
            if metadata.get(key) != value:
                metadata[key] = value
                needs_update = True
        if metadata.get("embedding_dimension") != collection_dimension and collection_dimension is not None:
            metadata["embedding_dimension"] = collection_dimension
            needs_update = True
        if needs_update:
            self._update_collection_metadata(collection, metadata)

    def _ensure_embeddings_dimension(self, embeddings: List[List[float]], enterpriseId: str):
        expected_dimension = self._get_embedding_contract().get("embedding_dimension")
        if expected_dimension is None:
            return
        for index, embedding in enumerate(embeddings):
            actual_dimension = len(embedding)
            if actual_dimension != expected_dimension:
                raise RuntimeError(
                    f"向量维度异常: enterprise={enterpriseId}, expected={expected_dimension}, actual={actual_dimension}, index={index}"
                )
    
    def getCollection(self, enterpriseId: str, createIfNotExists: bool = True):
        """
        获取企业专属集合
        
        Args:
            enterpriseId: 企业ID
            createIfNotExists: 如果不存在是否创建
            
        Returns:
            Chroma集合对象
        """
        self._initClient()
        
        collectionName = f"{self.config.collectionPrefix}{enterpriseId}"
        
        with self._collections_lock:
            if collectionName not in self._collections:
                metadata = {
                    "enterprise_id": enterpriseId,
                    **self.config.hnswConfig,
                    "hnsw:space": self.config.distanceMetric,
                    **self._get_embedding_contract(),
                }
                
                if createIfNotExists:
                    self._collections[collectionName] = self._client.get_or_create_collection(
                        name=collectionName,
                        metadata=metadata
                    )
                else:
                    self._collections[collectionName] = self._client.get_collection(
                        name=collectionName
                    )
            collection = self._collections[collectionName]
            self._ensure_collection_contract(collection, enterpriseId)
            return collection
    
    def listCollections(self) -> List[Dict]:
        """
        列出所有集合
        
        Returns:
            集合信息列表
        """
        self._initClient()
        
        collections = []
        for collection in self._client.list_collections():
            collections.append({
                "name": collection.name,
                "count": collection.count(),
                "metadata": dict(collection.metadata) if collection.metadata else {}
            })
        
        return collections
    
    def addDocuments(
        self,
        enterpriseId: str,
        documents: List[Dict],
        batchSize: int = 100
    ) -> Dict[str, Any]:
        """
        批量添加文档到向量库（线程安全）
        
        Args:
            enterpriseId: 企业ID
            documents: 文档列表，每个文档包含 id, content, metadata
            batchSize: 批量处理大小
            
        Returns:
            操作结果统计
        """
        if not documents:
            return {"success": True, "added": 0}
        
        collection = self.getCollection(enterpriseId)
        added_count = 0
        errors = []
        
        for i in range(0, len(documents), batchSize):
            batch = documents[i:i + batchSize]
            
            try:
                ids = [doc["id"] for doc in batch]
                contents = [doc["content"] for doc in batch]
                metadatas = self._sanitize_metadatas([doc.get("metadata", {}) for doc in batch])
                explicit_embeddings = [doc.get("embedding") for doc in batch]
                has_explicit_embeddings = all(embedding is not None for embedding in explicit_embeddings)
                
                if has_explicit_embeddings:
                    embeddings = [
                        embedding.tolist() if hasattr(embedding, "tolist") else embedding
                        for embedding in explicit_embeddings
                    ]
                    self._ensure_embeddings_dimension(embeddings, enterpriseId)
                    with self._collections_lock:
                        collection.add(
                            ids=ids,
                            embeddings=embeddings,
                            documents=contents,
                            metadatas=metadatas
                        )
                elif self.embedding:
                    embeddings = self.embedding.embed(contents).tolist()
                    self._ensure_embeddings_dimension(embeddings, enterpriseId)
                    
                    with self._collections_lock:
                        collection.add(
                            ids=ids,
                            embeddings=embeddings,
                            documents=contents,
                            metadatas=metadatas
                        )
                else:
                    with self._collections_lock:
                        collection.add(
                            ids=ids,
                            documents=contents,
                            metadatas=metadatas
                        )
                
                added_count += len(batch)
                
            except Exception as e:
                logger.error(f"批量添加文档失败 (批次 {i//batchSize + 1}): {e}")
                errors.append({
                    "batch": i // batchSize + 1,
                    "error": str(e)
                })
        
        return {
            "success": len(errors) == 0,
            "added": added_count,
            "total": len(documents),
            "errors": errors if errors else None
        }
    
    def search(
        self,
        enterpriseId: str,
        query: str,
        topK: int = 5,
        filterConditions: Optional[Dict] = None,
        whereDocument: Optional[Dict] = None
    ) -> List[Tuple[Dict, float]]:
        """
        向量检索
        
        Args:
            enterpriseId: 企业ID
            query: 查询文本
            topK: 返回结果数量
            filterConditions: 元数据过滤条件
            whereDocument: 文档内容过滤条件
            
        Returns:
            检索结果列表 [(document, score), ...]
        """
        collection = self.getCollection(enterpriseId)
        
        try:
            if self.embedding:
                queryEmbedding = self.embedding.embedSingle(query).tolist()
                self._ensure_embeddings_dimension([queryEmbedding], enterpriseId)
                
                with self._collections_lock:
                    results = collection.query(
                        query_embeddings=[queryEmbedding],
                        n_results=topK,
                        where=filterConditions,
                        where_document=whereDocument,
                        include=["documents", "metadatas", "distances"]
                    )
            else:
                with self._collections_lock:
                    results = collection.query(
                        query_texts=[query],
                        n_results=topK,
                        where=filterConditions,
                        where_document=whereDocument,
                        include=["documents", "metadatas", "distances"]
                    )
            
            formattedResults = []
            if results["ids"] and results["ids"][0]:
                for i in range(len(results["ids"][0])):
                    doc = {
                        "id": results["ids"][0][i],
                        "content": results["documents"][0][i] if results["documents"] else "",
                        "metadata": results["metadatas"][0][i] if results["metadatas"] else {}
                    }
                    distance = results["distances"][0][i] if results["distances"] else 0
                    if self.config.distanceMetric == "cosine":
                        score = 1 - distance
                    elif self.config.distanceMetric == "ip":
                        score = distance
                    else:
                        score = 1 / (1 + distance)
                    formattedResults.append((doc, score))
            
            return formattedResults
            
        except Exception as e:
            logger.error(f"向量检索失败: {e}")
            return []
    
    def searchByIds(
        self,
        enterpriseId: str,
        ids: List[str]
    ) -> List[Dict]:
        """
        根据ID获取文档
        
        Args:
            enterpriseId: 企业ID
            ids: 文档ID列表
            
        Returns:
            文档列表
        """
        if not ids:
            return []
        
        collection = self.getCollection(enterpriseId)
        
        try:
            results = collection.get(
                ids=ids,
                include=["documents", "metadatas"]
            )
            
            documents = []
            if results["ids"]:
                for i in range(len(results["ids"])):
                    documents.append({
                        "id": results["ids"][i],
                        "content": results["documents"][i] if results["documents"] else "",
                        "metadata": results["metadatas"][i] if results["metadatas"] else {}
                    })
            
            return documents
            
        except Exception as e:
            logger.error(f"根据ID获取文档失败: {e}")
            return []
    
    def deleteDocument(self, enterpriseId: str, documentId: str) -> bool:
        """
        删除单个文档
        
        Args:
            enterpriseId: 企业ID
            documentId: 文档ID
            
        Returns:
            是否成功
        """
        try:
            collection = self.getCollection(enterpriseId)
            collection.delete(ids=[documentId])
            return True
        except Exception as e:
            logger.error(f"删除文档失败: {e}")
            return False
    
    def deleteDocuments(self, enterpriseId: str, documentIds: List[str]) -> Dict:
        """
        批量删除文档
        
        Args:
            enterpriseId: 企业ID
            documentIds: 文档ID列表
            
        Returns:
            操作结果
        """
        if not documentIds:
            return {"success": True, "deleted": 0}
        
        try:
            collection = self.getCollection(enterpriseId)
            collection.delete(ids=documentIds)
            return {"success": True, "deleted": len(documentIds)}
        except Exception as e:
            logger.error(f"批量删除文档失败: {e}")
            return {"success": False, "error": str(e)}
    
    def deleteDocumentsByFilter(self, enterpriseId: str, filterConditions: Dict) -> Dict:
        """
        根据条件删除文档
        
        Args:
            enterpriseId: 企业ID
            filterConditions: 过滤条件
            
        Returns:
            操作结果
        """
        try:
            collection = self.getCollection(enterpriseId)
            collection.delete(where=filterConditions)
            return {"success": True}
        except Exception as e:
            logger.error(f"根据条件删除文档失败: {e}")
            return {"success": False, "error": str(e)}
    
    def deleteEnterpriseData(self, enterpriseId: str) -> bool:
        """
        删除企业所有数据

        Args:
            enterpriseId: 企业ID

        Returns:
            是否成功
        """
        collectionName = f"{self.config.collectionPrefix}{enterpriseId}"
        try:
            with self._collections_lock:
                if collectionName in self._collections:
                    del self._collections[collectionName]
                if self._initialized:
                    self._client.delete_collection(collectionName)
            logger.info(f"已删除企业 {enterpriseId} 的所有向量数据")
            return True
        except Exception as e:
            logger.warning(f"删除企业数据失败: {e}")
            return False
    
    def getCollectionStats(self, enterpriseId: str) -> Dict:
        """
        获取集合统计信息
        
        Args:
            enterpriseId: 企业ID
            
        Returns:
            统计信息字典
        """
        try:
            collection = self.getCollection(enterpriseId, createIfNotExists=False)
            return {
                "count": collection.count(),
                "name": collection.name,
                "metadata": dict(collection.metadata) if collection.metadata else {},
                "exists": True
            }
        except Exception:
            return {
                "count": 0,
                "name": f"{self.config.collectionPrefix}{enterpriseId}",
                "metadata": {},
                "exists": False
            }
    
    def updateDocument(
        self,
        enterpriseId: str,
        documentId: str,
        content: str,
        metadata: Optional[Dict] = None
    ) -> bool:
        """
        更新文档
        
        Args:
            enterpriseId: 企业ID
            documentId: 文档ID
            content: 新内容
            metadata: 新元数据
            
        Returns:
            是否成功
        """
        try:
            collection = self.getCollection(enterpriseId)
            
            if self.embedding:
                embedding = self.embedding.embedSingle(content).tolist()
                self._ensure_embeddings_dimension([embedding], enterpriseId)
                collection.update(
                    ids=[documentId],
                    embeddings=[embedding],
                    documents=[content],
                    metadatas=[metadata] if metadata else None
                )
            else:
                collection.update(
                    ids=[documentId],
                    documents=[content],
                    metadatas=[metadata] if metadata else None
                )
            
            return True
            
        except Exception as e:
            logger.error(f"更新文档失败: {e}")
            return False
    
    def upsertDocuments(
        self,
        enterpriseId: str,
        documents: List[Dict],
        batchSize: int = 100
    ) -> Dict:
        """
        批量更新或插入文档
        
        如果文档ID已存在则更新，否则插入
        
        Args:
            enterpriseId: 企业ID
            documents: 文档列表
            batchSize: 批量处理大小
            
        Returns:
            操作结果
        """
        if not documents:
            return {"success": True, "upserted": 0}
        
        collection = self.getCollection(enterpriseId)
        upserted_count = 0
        errors = []
        
        for i in range(0, len(documents), batchSize):
            batch = documents[i:i + batchSize]
            
            try:
                ids = [doc["id"] for doc in batch]
                contents = [doc["content"] for doc in batch]
                metadatas = [doc.get("metadata", {}) for doc in batch]
                
                if self.embedding:
                    embeddings = self.embedding.embed(contents).tolist()
                    self._ensure_embeddings_dimension(embeddings, enterpriseId)
                    
                    with self._collections_lock:
                        collection.upsert(
                            ids=ids,
                            embeddings=embeddings,
                            documents=contents,
                            metadatas=metadatas
                        )
                else:
                    with self._collections_lock:
                        collection.upsert(
                            ids=ids,
                            documents=contents,
                            metadatas=metadatas
                        )
                
                upserted_count += len(batch)
                
            except Exception as e:
                logger.error(f"批量upsert失败 (批次 {i//batchSize + 1}): {e}")
                errors.append({"batch": i // batchSize + 1, "error": str(e)})
        
        return {
            "success": len(errors) == 0,
            "upserted": upserted_count,
            "total": len(documents),
            "errors": errors if errors else None
        }
    
    def healthCheck(self) -> Dict[str, Any]:
        """
        健康检查

        Returns:
            健康状态信息
        """
        now = time.time()

        if now - self._last_health_check < self._health_check_interval:
            return {
                "status": "healthy",
                "cached": True
            }

        try:
            self._initClient()

            test_collection_name = "health_check_probe"
            metadata = {"test": True, **self._get_embedding_contract()}
            test_collection = self._client.get_or_create_collection(
                name=test_collection_name,
                metadata=metadata
            )
            self._ensure_collection_contract(test_collection, "_health_check_")

            if self.embedding:
                test_embedding = self.embedding.embed(["test"])[0].tolist()
                self._ensure_embeddings_dimension([test_embedding], "_health_check_")
                test_collection.add(
                    ids=["test"],
                    embeddings=[test_embedding],
                    documents=["test"],
                    metadatas=[{"test": True}]
                )
            else:
                test_collection.add(
                    ids=["test"],
                    documents=["test"],
                    metadatas=[{"test": True}]
                )

            test_collection.get(ids=["test"])

            test_collection.delete(ids=["test"])

            try:
                self._client.delete_collection(test_collection_name)
                with self._collections_lock:
                    if test_collection_name in self._collections:
                        del self._collections[test_collection_name]
            except Exception:
                pass

            self._last_health_check = now

            return {
                "status": "healthy",
                "persist_directory": self.config.persistDirectory,
                "collections_count": len(self._client.list_collections()),
                "embedding_enabled": self.embedding is not None,
                "embedding_contract": self._get_embedding_contract(),
            }

        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e)
            }
    
    def getDatabaseInfo(self) -> Dict[str, Any]:
        """
        获取数据库详细信息
        
        Returns:
            数据库信息
        """
        try:
            self._initClient()
            
            collections = self.listCollections()
            total_vectors = sum(c["count"] for c in collections)
            
            return {
                "persist_directory": self.config.persistDirectory,
                "distance_metric": self.config.distanceMetric,
                "collections_count": len(collections),
                "total_vectors": total_vectors,
                "collections": collections,
                "embedding_service": type(self.embedding).__name__ if self.embedding else None
            }
            
        except Exception as e:
            return {
                "error": str(e)
            }
    
    def addDocumentsWithEnhancedMetadata(
        self,
        enterpriseId: str,
        documents: List[Dict],
        batchSize: int = 100
    ) -> Dict[str, Any]:
        """
        添加带有增强元数据的文档
        
        Args:
            enterpriseId: 企业ID
            documents: 文档列表
            batchSize: 批量处理大小
            
        Returns:
            操作结果统计
        """
        enhancedDocs = []
        
        for doc in documents:
            metadata = doc.get("metadata", {})
            
            enhancedMetadata = {
                "documentId": metadata.get("documentId", doc.get("id", "")),
                "documentName": metadata.get("documentName", ""),
                "category": metadata.get("category", "general"),
                "chunkType": metadata.get("chunkType", "semantic"),
                "sectionTitle": metadata.get("sectionTitle", ""),
                "sectionLevel": metadata.get("sectionLevel", 0),
                "charCount": len(doc.get("content", "")),
                "createdAt": metadata.get("createdAt", time.time()),
                "source": metadata.get("source", "document_upload"),
                "tags": ",".join(metadata.get("tags", [])) if isinstance(metadata.get("tags"), list) else metadata.get("tags", ""),
                "keywords": ",".join(metadata.get("keywords", [])) if isinstance(metadata.get("keywords"), list) else metadata.get("keywords", ""),
                "priority": metadata.get("priority", 10),
                "enabled": 1 if metadata.get("enabled", True) else 0
            }
            
            enhancedDocs.append({
                "id": doc["id"],
                "content": doc["content"],
                "metadata": enhancedMetadata
            })
        
        return self.addDocuments(enterpriseId, enhancedDocs, batchSize)
    
    def hybridSearch(
        self,
        enterpriseId: str,
        query: str,
        topK: int = 10,
        filterConditions: Optional[Dict] = None,
        useKeywordSearch: bool = True,
        keywordWeight: float = 0.2
    ) -> List[Tuple[Dict, float]]:
        """
        混合检索（向量 + 关键词）
        
        Args:
            enterpriseId: 企业ID
            query: 查询文本
            topK: 返回结果数量
            filterConditions: 元数据过滤条件
            useKeywordSearch: 是否使用关键词检索
            keywordWeight: 关键词检索权重（优化：降低到0.2，向量检索更重要）
            
        Returns:
            检索结果列表
        """
        expandedQueries = self._expandQuery(query, enterprise_id=enterpriseId)
        allVectorResults = []
        
        with self._collections_lock:
            for expandedQuery in expandedQueries:
                vectorResults = self.search(
                    enterpriseId,
                    expandedQuery,
                    topK=topK * 2,
                    filterConditions=filterConditions
                )
                allVectorResults.extend(vectorResults)
        
            mergedByDoc = {}
            for doc, score in allVectorResults:
                docId = doc["id"]
                if docId not in mergedByDoc or score > mergedByDoc[docId][1]:
                    mergedByDoc[docId] = (doc, score)
        
            vectorResults = list(mergedByDoc.values())
            vectorResults.sort(key=lambda x: x[1], reverse=True)
            vectorResults = vectorResults[:topK * 2]
        
            if not useKeywordSearch:
                return vectorResults[:topK]
        
            keywordResults = self._keywordSearch(
                enterpriseId,
                query,
                topK=topK * 2,
                filterConditions=filterConditions
            )
        
        mergedResults = {}
        
        for doc, score in vectorResults:
            docId = doc["id"]
            mergedResults[docId] = {
                "doc": doc,
                "vectorScore": score,
                "keywordScore": 0
            }
        
        for doc, score in keywordResults:
            docId = doc["id"]
            if docId in mergedResults:
                mergedResults[docId]["keywordScore"] = score
            else:
                mergedResults[docId] = {
                    "doc": doc,
                    "vectorScore": 0,
                    "keywordScore": score
                }
        
        vectorScores = [d["vectorScore"] for d in mergedResults.values() if d["vectorScore"] > 0]
        keywordScores = [d["keywordScore"] for d in mergedResults.values() if d["keywordScore"] > 0]
        
        v_min = min(vectorScores) if vectorScores else 0
        v_max = max(vectorScores) if vectorScores else 1
        v_range = v_max - v_min if v_max > v_min else 1.0
        
        k_min = min(keywordScores) if keywordScores else 0
        k_max = max(keywordScores) if keywordScores else 1
        k_range = k_max - k_min if k_max > k_min else 1.0
        
        finalResults = []
        for docId, data in mergedResults.items():
            vectorScore = data["vectorScore"]
            keywordScore = data["keywordScore"]
            
            normVector = (vectorScore - v_min) / v_range if vectorScore > 0 else 0.0
            normKeyword = (keywordScore - k_min) / k_range if keywordScore > 0 else 0.0
            
            combinedScore = (1 - keywordWeight) * normVector + keywordWeight * normKeyword
            
            finalResults.append((data["doc"], combinedScore))
        
        finalResults.sort(key=lambda x: x[1], reverse=True)
        
        return finalResults[:topK]
    
    def _expandQuery(self, query: str, enterprise_id: str = "") -> List[str]:
        """
        查询扩展（通用基座 + schema 配置增量）
        
        为查询添加同义词和相关词，提升检索召回率
        
        Args:
            query: 原始查询
            
        Returns:
            扩展后的查询列表
        """
        queries = [query]

        expansions = dict(self.GENERIC_QUERY_EXPANSIONS)
        query_expansion_config = self._get_query_expansion_config(enterprise_id)
        context_detection_terms = tuple(
            str(item or "").strip()
            for item in list(query_expansion_config.get("context_detection_terms") or [])
            if str(item or "").strip()
        )
        schema_context = bool(context_detection_terms) and any(term in query for term in context_detection_terms)
        if not schema_context:
            try:
                schema_context = self._is_route_domain_context(query, enterprise_id=enterprise_id)
            except TypeError:
                schema_context = self._is_route_domain_context(query)

        if schema_context:
            configured_aliases = query_expansion_config.get("term_alias_groups") or {}
            if isinstance(configured_aliases, dict) and configured_aliases:
                expansions.update(configured_aliases)
            else:
                scenic_aliases = self._get_scenic_aliases(enterprise_id)
                if scenic_aliases:
                    expansions.update(scenic_aliases)

        for key, synonyms in expansions.items():
            if key in query:
                for syn in synonyms:
                    expanded = query.replace(key, syn)
                    if expanded not in queries:
                        queries.append(expanded)

        expansion_limit = query_expansion_config.get("expansion_limit") or 5
        try:
            expansion_limit = max(int(expansion_limit), 1)
        except (TypeError, ValueError):
            expansion_limit = 5
        return queries[:expansion_limit]

    def _is_route_domain_context(self, query: str, enterprise_id: str = "") -> bool:
        text = str(query or "").strip()
        active_schema = self._get_active_schema(enterprise_id)
        domain_keywords = self._get_domain_keywords(enterprise_id)
        if domain_keywords and any(term in text for term in domain_keywords):
            return True
        runtime_flags = self._get_runtime_flags(enterprise_id)
        return bool(
            runtime_flags.get("route_domain_enabled")
            or runtime_flags.get("domain_query_expansion_enabled")
            or active_schema.get("is_domain_specific", False)
        )
    
    def _keywordSearch(
        self,
        enterpriseId: str,
        query: str,
        topK: int = 10,
        filterConditions: Optional[Dict] = None
    ) -> List[Tuple[Dict, float]]:
        """
        关键词检索
        
        Args:
            enterpriseId: 企业ID
            query: 查询文本
            topK: 返回结果数量
            filterConditions: 过滤条件
            
        Returns:
            检索结果列表
        """
        collection = self.getCollection(enterpriseId)
        
        try:
            keywords = self._extractKeywords(query)
            
            if not keywords:
                return []
            
            all_results = {}
            for kw in keywords[:3]:
                try:
                    whereDocument = {"$contains": kw}
                    with self._collections_lock:
                        results = collection.get(
                            where_document=whereDocument,
                            include=["documents", "metadatas"],
                            limit=topK * 2
                        )
                    if results["ids"]:
                        for i in range(len(results["ids"])):
                            doc_id = results["ids"][i]
                            if doc_id not in all_results:
                                all_results[doc_id] = {
                                    "id": doc_id,
                                    "content": results["documents"][i] if results["documents"] else "",
                                    "metadata": results["metadatas"][i] if results["metadatas"] else {}
                                }
                except Exception as e:
                    logger.warning(f"关键词'{kw}'检索失败: {e}")
            
            formattedResults = []
            for doc in all_results.values():
                content = doc["content"].lower()
                matched_count = 0
                for kw in keywords:
                    kw_lower = kw.lower()
                    if kw_lower in content:
                        idx = content.find(kw_lower)
                        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in kw_lower)
                        if has_cjk or idx == 0 or not content[idx - 1].isalnum():
                            matched_count += 1
                if matched_count == 0:
                    continue
                score = matched_count / len(keywords)
                formattedResults.append((doc, score))
            
            formattedResults.sort(key=lambda x: x[1], reverse=True)
            
            return formattedResults[:topK]
            
        except Exception as e:
            logger.error(f"关键词检索失败: {e}")
            return []
    
    def _extractKeywords(self, text: str) -> List[str]:
        """
        提取关键词
        
        Args:
            text: 文本
            
        Returns:
            关键词列表
        """
        import re
        
        stopwords = {"的", "是", "在", "了", "和", "有", "我", "不", "这", "个",
                    "也", "就", "都", "而", "及", "与", "着", "或", "一个", "没有"}
        
        # 修复 P12：正则增加数字匹配，避免"3天2晚"类查询丢失关键信息
        words = re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]+|\d+', text.lower())
        
        keywords = [w for w in words if w not in stopwords and len(w) > 1]
        
        return keywords[:10]
    
    def rerankResults(
        self,
        query: str,
        results: List[Tuple[Dict, float]],
        topK: int = 5
    ) -> List[Tuple[Dict, float]]:
        """
        重排序检索结果
        
        基于关键词匹配和内容相关性进行重排序
        
        Args:
            query: 查询文本
            results: 原始检索结果
            topK: 返回结果数量
            
        Returns:
            重排序后的结果列表
        """
        if not results:
            return []
        
        queryKeywords = self._extractKeywords(query)
        queryLower = query.lower()
        
        reranked = []
        for doc, originalScore in results:
            content = doc.get("content", "").lower()
            
            keywordBonus = 0
            for kw in queryKeywords:
                if kw in content:
                    keywordBonus += 0.05
            
            exactMatchBonus = 0
            if queryLower in content:
                exactMatchBonus = 0.1
            
            titleBonus = 0
            question = doc.get("metadata", {}).get("question", "")
            if question and any(kw in question.lower() for kw in queryKeywords):
                titleBonus = 0.05
            
            finalScore = originalScore + keywordBonus + exactMatchBonus + titleBonus
            finalScore = min(finalScore, 1.0)
            
            reranked.append((doc, finalScore))
        
        reranked.sort(key=lambda x: x[1], reverse=True)
        
        return reranked[:topK]
    
    def searchByCategory(
        self,
        enterpriseId: str,
        query: str,
        category: str,
        topK: int = 5
    ) -> List[Tuple[Dict, float]]:
        """
        按分类检索
        
        Args:
            enterpriseId: 企业ID
            query: 查询文本
            category: 分类
            topK: 返回结果数量
            
        Returns:
            检索结果列表
        """
        filterConditions = {"category": category}
        
        return self.hybridSearch(
            enterpriseId,
            query,
            topK=topK,
            filterConditions=filterConditions
        )
    
    def searchByDocument(
        self,
        enterpriseId: str,
        query: str,
        documentId: str,
        topK: int = 5
    ) -> List[Tuple[Dict, float]]:
        """
        按文档检索
        
        Args:
            enterpriseId: 企业ID
            query: 查询文本
            documentId: 文档ID
            topK: 返回结果数量
            
        Returns:
            检索结果列表
        """
        filterConditions = {"documentId": documentId}
        
        return self.hybridSearch(
            enterpriseId,
            query,
            topK=topK,
            filterConditions=filterConditions
        )
    
    def getDocumentChunks(
        self,
        enterpriseId: str,
        documentId: str
    ) -> List[Dict]:
        """
        获取文档的所有分块
        
        Args:
            enterpriseId: 企业ID
            documentId: 文档ID
            
        Returns:
            分块列表
        """
        collection = self.getCollection(enterpriseId)
        
        try:
            results = collection.get(
                where={"documentId": documentId},
                include=["documents", "metadatas"]
            )
            
            chunks = []
            if results["ids"]:
                for i in range(len(results["ids"])):
                    chunks.append({
                        "id": results["ids"][i],
                        "content": results["documents"][i] if results["documents"] else "",
                        "metadata": results["metadatas"][i] if results["metadatas"] else {}
                    })
            
            chunks.sort(key=lambda x: x["metadata"].get("chunkIndex", 0))
            
            return chunks
            
        except Exception as e:
            logger.error(f"获取文档分块失败: {e}")
            return []
    
    def getStatistics(self, enterpriseId: str) -> Dict[str, Any]:
        """
        获取详细统计信息
        
        Args:
            enterpriseId: 企业ID
            
        Returns:
            统计信息
        """
        collection = self.getCollection(enterpriseId, createIfNotExists=False)
        
        try:
            results = collection.get(include=["metadatas"])
            
            stats = {
                "total": len(results["ids"]) if results["ids"] else 0,
                "categories": {},
                "chunkTypes": {},
                "documents": set()
            }
            
            if results["metadatas"]:
                for meta in results["metadatas"]:
                    category = meta.get("category", "unknown")
                    stats["categories"][category] = stats["categories"].get(category, 0) + 1
                    
                    chunkType = meta.get("chunkType", "unknown")
                    stats["chunkTypes"][chunkType] = stats["chunkTypes"].get(chunkType, 0) + 1
                    
                    docId = meta.get("documentId", "")
                    if docId:
                        stats["documents"].add(docId)
            
            stats["documents"] = len(stats["documents"])
            
            return stats
            
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {"total": 0, "error": str(e)}
