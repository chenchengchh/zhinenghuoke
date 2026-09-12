"""
企业级文件上传服务
Enterprise File Upload Service

整合摄取管道，提供完整的文件上传、处理、向量化流程
"""

import os
import uuid
import json
import copy
import tempfile
import aiofiles
import asyncio
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from loguru import logger

from src.infrastructure.runtime_paths import get_knowledge_files_dir

from .ocr_service import build_document_ocr_extractor_from_env
from .enterprise_ingestion_pipeline import (
    IngestionPipeline,
    ProcessingStatus,
    ProcessingProgress,
    ChunkStrategy,
    get_ingestion_pipeline
)


@dataclass
class UploadedFile:
    """上传文件信息"""
    id: str
    enterpriseId: str
    originalName: str
    storedName: str
    filePath: str
    fileSize: int
    fileType: str
    category: str
    tags: List[str]
    status: str
    contentHash: str = ""
    createdAt: Optional[datetime] = None
    processedAt: Optional[datetime] = None
    errorMessage: Optional[str] = None
    chunkCount: int = 0
    vectorCount: int = 0
    taskId: str = ""
    documentId: str = ""
    indexLedger: Dict = field(default_factory=dict)
    
    def toDict(self) -> Dict:
        """转换为字典"""
        return {
            "id": self.id,
            "enterpriseId": self.enterpriseId,
            "originalName": self.originalName,
            "storedName": self.storedName,
            "filePath": self.filePath,
            "fileSize": self.fileSize,
            "fileType": self.fileType,
            "category": self.category,
            "tags": self.tags,
            "status": self.status,
            "contentHash": self.contentHash,
            "createdAt": self.createdAt.isoformat() if self.createdAt else None,
            "processedAt": self.processedAt.isoformat() if self.processedAt else None,
            "errorMessage": self.errorMessage,
            "chunkCount": self.chunkCount,
            "vectorCount": self.vectorCount,
            "taskId": self.taskId,
            "documentId": self.documentId,
            "indexLedger": self.indexLedger,
        }


@dataclass
class FileUploadConfig:
    """文件上传配置"""
    maxFileSize: int = 50 * 1024 * 1024
    allowedExtensions: set = field(default_factory=lambda: {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".txt", ".md", ".csv", ".json"
    })
    storageRoot: str = field(default_factory=lambda: str(get_knowledge_files_dir()))
    metadataFile: str = "file_metadata.json"
    maxConcurrentUploads: int = 4
    chunkStrategy: ChunkStrategy = ChunkStrategy.RECURSIVE
    autoProcess: bool = True
    ocrExtractor: Optional[Callable[[str], str]] = None


class EnterpriseFileUploadService:
    """
    企业级文件上传服务
    
    特性：
    - 异步文件上传
    - 自动向量化处理
    - 进度跟踪
    - 文件去重
    - 批量上传
    - 错误重试
    """
    
    def __init__(
        self,
        config: Optional[FileUploadConfig] = None,
        embeddingService=None,
        vectorStore=None,
        knowledgeService=None
    ):
        self.config = config or FileUploadConfig()
        self.embeddingService = embeddingService
        self.vectorStore = vectorStore
        self.knowledgeService = knowledgeService
        
        self._metadata: Dict[str, Dict[str, UploadedFile]] = {}
        self._progressCallbacks: List[Callable] = []
        
        self._pipeline = get_ingestion_pipeline(
            embeddingService=embeddingService,
            vectorStore=vectorStore,
            knowledgeService=knowledgeService,
            ocrExtractor=self.config.ocrExtractor,
        )
        
        os.makedirs(self.config.storageRoot, exist_ok=True)
        self._loadMetadata()

    def setOcrExtractor(self, ocrExtractor: Optional[Callable[[str], str]]) -> None:
        """运行时更新 OCR 提取器并同步到摄取管道。"""
        self.config.ocrExtractor = ocrExtractor
        if hasattr(self._pipeline, "set_ocr_extractor"):
            self._pipeline.set_ocr_extractor(ocrExtractor)
        else:
            self._pipeline.ocrExtractor = ocrExtractor
            if hasattr(self._pipeline, "parser"):
                self._pipeline.parser.ocrExtractor = ocrExtractor
    
    def _loadMetadata(self):
        """加载文件元数据"""
        metadataPath = os.path.join(self.config.storageRoot, self.config.metadataFile)
        if os.path.exists(metadataPath):
            try:
                with open(metadataPath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for entId, files in data.items():
                        self._metadata[entId] = {}
                        for fileId, fileData in files.items():
                            self._metadata[entId][fileId] = UploadedFile(
                                id=fileData["id"],
                                enterpriseId=fileData["enterpriseId"],
                                originalName=fileData["originalName"],
                                storedName=fileData["storedName"],
                                filePath=fileData["filePath"],
                                fileSize=fileData["fileSize"],
                                fileType=fileData["fileType"],
                                category=fileData.get("category", "general"),
                                tags=fileData.get("tags", []),
                                status=fileData.get("status", "completed"),
                                contentHash=fileData.get("contentHash", ""),
                                createdAt=datetime.fromisoformat(fileData["createdAt"]) if fileData.get("createdAt") else datetime.now(),
                                processedAt=datetime.fromisoformat(fileData["processedAt"]) if fileData.get("processedAt") else None,
                                errorMessage=fileData.get("errorMessage"),
                                chunkCount=fileData.get("chunkCount", 0),
                                vectorCount=fileData.get("vectorCount", 0),
                                taskId=fileData.get("taskId", ""),
                                documentId=fileData.get("documentId", ""),
                                indexLedger=fileData.get("indexLedger", {}) or {},
                            )
                logger.info(f"加载文件元数据: {sum(len(v) for v in self._metadata.values())} 个文件")
            except Exception as e:
                logger.warning(f"加载文件元数据失败: {e}")
    
    def _saveMetadata(self):
        """保存文件元数据"""
        metadataPath = os.path.join(self.config.storageRoot, self.config.metadataFile)
        data = {}
        for entId, files in self._metadata.items():
            data[entId] = {}
            for fileId, uploadedFile in files.items():
                data[entId][fileId] = uploadedFile.toDict()

        os.makedirs(self.config.storageRoot, exist_ok=True)
        fd, tempPath = tempfile.mkstemp(
            prefix="file_metadata_",
            suffix=".tmp",
            dir=self.config.storageRoot,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tempPath, metadataPath)
        finally:
            if os.path.exists(tempPath):
                os.remove(tempPath)
    
    def addProgressCallback(self, callback: Callable):
        """添加进度回调"""
        self._progressCallbacks.append(callback)

    def setChunkStrategy(self, strategy: ChunkStrategy):
        """兼容旧调用，设置默认分块策略。"""
        if strategy:
            self.config.chunkStrategy = strategy
            self._pipeline.chunkStrategy = strategy
    
    def removeProgressCallback(self, callback: Callable):
        """移除进度回调"""
        if callback in self._progressCallbacks:
            self._progressCallbacks.remove(callback)
    
    async def _notifyProgress(self, progress: ProcessingProgress):
        """通知进度更新"""
        for callback in self._progressCallbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(progress)
                else:
                    callback(progress)
            except Exception as e:
                logger.warning(f"进度回调失败: {e}")
    
    def _calculateContentHash(self, content: bytes) -> str:
        """计算内容哈希"""
        import hashlib
        return hashlib.sha256(content).hexdigest()
    
    def _isDuplicate(self, enterpriseId: str, contentHash: str) -> Optional[UploadedFile]:
        """检查是否重复"""
        if enterpriseId not in self._metadata:
            return None
        
        for file in self._metadata[enterpriseId].values():
            if file.contentHash == contentHash:
                return file
        return None
    
    async def uploadFile(
        self,
        enterpriseId: str,
        fileContent: bytes,
        fileName: str,
        category: str = "general",
        tags: Optional[List[str]] = None,
        autoProcess: Optional[bool] = None,
        chunkStrategy: Optional[ChunkStrategy] = None,
        chunkSize: Optional[int] = None,
    ) -> UploadedFile:
        """
        上传文件
        
        Args:
            enterpriseId: 企业ID
            fileContent: 文件内容
            fileName: 文件名
            category: 文档分类
            tags: 标签列表
            autoProcess: 是否自动处理
            
        Returns:
            上传文件信息
        """
        fileExt = os.path.splitext(fileName)[1].lower()
        
        if fileExt not in self.config.allowedExtensions:
            raise ValueError(f"不支持的文件类型: {fileExt}")
        
        if len(fileContent) > self.config.maxFileSize:
            raise ValueError(f"文件大小超过限制: {self.config.maxFileSize / 1024 / 1024}MB")
        
        contentHash = self._calculateContentHash(fileContent)
        
        existingFile = self._isDuplicate(enterpriseId, contentHash)
        if existingFile:
            logger.info(f"文件已存在，跳过上传: {fileName}")
            shouldProcess = autoProcess if autoProcess is not None else self.config.autoProcess
            needsReprocess = (
                shouldProcess
                and existingFile.status in {"uploaded", "failed", "timeout", "cancelled"}
            )
            if shouldProcess and existingFile.status == "completed" and not self._checkExistingKnowledge(existingFile.id):
                needsReprocess = True
            if needsReprocess and existingFile.status != "processing":
                logger.info(f"检测到重复文件需补处理，重新处理: {existingFile.originalName}")
                return await self.processFile(
                    existingFile.enterpriseId,
                    existingFile.id,
                    chunkStrategy=chunkStrategy,
                    chunkSize=chunkSize,
                )
            return existingFile
        
        fileId = str(uuid.uuid4())
        storedName = f"{fileId}{fileExt}"
        
        enterpriseDir = os.path.join(self.config.storageRoot, enterpriseId)
        os.makedirs(enterpriseDir, exist_ok=True)
        
        filePath = os.path.join(enterpriseDir, storedName)
        
        async with aiofiles.open(filePath, "wb") as f:
            await f.write(fileContent)
        
        uploadedFile = UploadedFile(
            id=fileId,
            enterpriseId=enterpriseId,
            originalName=fileName,
            storedName=storedName,
            filePath=filePath,
            fileSize=len(fileContent),
            fileType=fileExt,
            category=category,
            tags=tags or [],
            status="uploaded",
            contentHash=contentHash,
            createdAt=datetime.now()
        )
        
        if enterpriseId not in self._metadata:
            self._metadata[enterpriseId] = {}
        self._metadata[enterpriseId][fileId] = uploadedFile
        self._saveMetadata()
        
        logger.info(f"文件上传成功: {fileName} ({len(fileContent)} bytes)")
        
        shouldProcess = autoProcess if autoProcess is not None else self.config.autoProcess
        if shouldProcess:
            processedFile = await self.processFile(
                enterpriseId,
                fileId,
                chunkStrategy=chunkStrategy,
                chunkSize=chunkSize,
            )
            if processedFile:
                uploadedFile.status = processedFile.status
                uploadedFile.chunkCount = processedFile.chunkCount
                uploadedFile.vectorCount = processedFile.vectorCount
                uploadedFile.taskId = processedFile.taskId
                uploadedFile.documentId = processedFile.documentId
                uploadedFile.errorMessage = processedFile.errorMessage
                self._saveMetadata()
        
        return uploadedFile
    
    async def uploadBatch(
        self,
        enterpriseId: str,
        files: List[Dict]
    ) -> List[UploadedFile]:
        """
        批量上传文件
        
        Args:
            enterpriseId: 企业ID
            files: 文件列表 [{"content": bytes, "name": str, "category": str, "tags": list}, ...]
            
        Returns:
            上传文件信息列表
        """
        results = []
        
        semaphore = asyncio.Semaphore(self.config.maxConcurrentUploads)
        
        async def uploadWithLimit(fileData: Dict) -> UploadedFile:
            async with semaphore:
                return await self.uploadFile(
                    enterpriseId=enterpriseId,
                    fileContent=fileData["content"],
                    fileName=fileData["name"],
                    category=fileData.get("category", "general"),
                    tags=fileData.get("tags")
                )
        
        tasks = [uploadWithLimit(f) for f in files]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        uploadedFiles = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"上传失败: {files[i]['name']}, 错误: {result}")
            else:
                uploadedFiles.append(result)
        
        return uploadedFiles
    
    async def processFile(
        self,
        enterpriseId: str,
        fileId: str,
        chunkStrategy: Optional[ChunkStrategy] = None,
        chunkSize: Optional[int] = None,
    ) -> UploadedFile:
        """
        处理上传的文件（同步等待完成）
        
        兼容现有API，同步处理并返回处理后的文件信息
        
        Args:
            enterpriseId: 企业ID
            fileId: 文件ID
            
        Returns:
            处理后的文件信息
        """
        if enterpriseId not in self._metadata or fileId not in self._metadata[enterpriseId]:
            raise ValueError(f"文件不存在: {fileId}")
        
        uploadedFile = self._metadata[enterpriseId][fileId]
        if uploadedFile.documentId or uploadedFile.indexLedger:
            self._clear_indexed_artifacts(uploadedFile)
        uploadedFile.status = "processing"
        self._saveMetadata()
        
        try:
            taskId = await self._pipeline.submitTask(
                filePath=uploadedFile.filePath,
                enterpriseId=enterpriseId,
                category=uploadedFile.category,
                tags=uploadedFile.tags,
                chunkStrategy=chunkStrategy or self.config.chunkStrategy,
                chunkSize=chunkSize,
                metadata={
                    "fileId": fileId,
                    "originalName": uploadedFile.originalName
                }
            )
            
            if taskId:
                uploadedFile.taskId = taskId
                self._saveMetadata()
                
                await self._waitForCompletion(enterpriseId, fileId, taskId)
            else:
                # 文件内容已存在，但仍需确保知识点已提取
                logger.info(f"文件内容已存在，检查知识点: {uploadedFile.originalName}")
                
                # 检查知识库中是否有来自此文件的知识点
                existing_knowledge = self._checkExistingKnowledge(fileId)
                
                if not existing_knowledge:
                    # 如果没有知识点，强制重新处理
                    logger.info(f"知识点不存在，强制重新处理: {uploadedFile.originalName}")
                    await self._forceProcessFile(uploadedFile)
                else:
                    uploadedFile.status = "completed"
                    uploadedFile.errorMessage = "文件内容已存在，知识点已同步"
                    uploadedFile.processedAt = datetime.now()
                    self._sync_file_index_ledger(uploadedFile)
                    self._saveMetadata()
                    logger.info(f"文件已存在且知识点已同步: {uploadedFile.originalName}")
                
        except Exception as e:
            uploadedFile.status = "failed"
            uploadedFile.errorMessage = str(e)
            uploadedFile.processedAt = datetime.now()
            self._saveMetadata()
            logger.error(f"文件处理失败: {e}")
        
        return uploadedFile
    
    async def processFileAsync(
        self,
        enterpriseId: str,
        fileId: str,
        chunkStrategy: Optional[ChunkStrategy] = None,
        chunkSize: Optional[int] = None,
    ) -> str:
        """
        异步处理上传的文件（不等待完成）
        
        Args:
            enterpriseId: 企业ID
            fileId: 文件ID
            
        Returns:
            任务ID
        """
        if enterpriseId not in self._metadata or fileId not in self._metadata[enterpriseId]:
            raise ValueError(f"文件不存在: {fileId}")
        
        uploadedFile = self._metadata[enterpriseId][fileId]
        if uploadedFile.documentId or uploadedFile.indexLedger:
            self._clear_indexed_artifacts(uploadedFile)
        uploadedFile.status = "processing"
        self._saveMetadata()
        
        taskId = await self._pipeline.submitTask(
            filePath=uploadedFile.filePath,
            enterpriseId=enterpriseId,
            category=uploadedFile.category,
            tags=uploadedFile.tags,
            chunkStrategy=chunkStrategy or self.config.chunkStrategy,
            chunkSize=chunkSize,
            metadata={
                "fileId": fileId,
                "originalName": uploadedFile.originalName
            }
        )
        
        if taskId:
            uploadedFile.taskId = taskId
            self._saveMetadata()
            
            asyncio.create_task(self._monitorProgress(enterpriseId, fileId, taskId))
        
        return taskId
    
    async def _waitForCompletion(self, enterpriseId: str, fileId: str, taskId: str, timeout: float = 300):
        """等待处理完成"""
        uploadedFile = self._metadata[enterpriseId][fileId]
        startTime = asyncio.get_event_loop().time()
        
        while True:
            progress = self._pipeline.getProgress(taskId)
            
            if progress:
                await self._notifyProgress(progress)
                
                if progress.status == ProcessingStatus.COMPLETED:
                    uploadedFile.status = "completed"
                    uploadedFile.processedAt = datetime.now()
                    uploadedFile.documentId = progress.documentId
                    uploadedFile.chunkCount = progress.chunkCount
                    uploadedFile.vectorCount = progress.vectorCount
                    self._sync_file_index_ledger(uploadedFile, progress.indexLedger)
                    self._saveMetadata()
                    return
                
                elif progress.status == ProcessingStatus.FAILED:
                    uploadedFile.status = "failed"
                    uploadedFile.errorMessage = progress.errorMessage
                    uploadedFile.processedAt = datetime.now()
                    self._saveMetadata()
                    return

                elif progress.status == ProcessingStatus.OCR_REQUIRED:
                    uploadedFile.status = "pending_ocr"
                    uploadedFile.errorMessage = progress.errorMessage
                    uploadedFile.processedAt = datetime.now()
                    self._saveMetadata()
                    return
                
                elif progress.status == ProcessingStatus.CANCELLED:
                    uploadedFile.status = "cancelled"
                    uploadedFile.processedAt = datetime.now()
                    self._saveMetadata()
                    return
            
            if asyncio.get_event_loop().time() - startTime > timeout:
                uploadedFile.status = "timeout"
                uploadedFile.errorMessage = f"处理超时（{timeout}秒）"
                uploadedFile.processedAt = datetime.now()
                self._saveMetadata()
                return
            
            await asyncio.sleep(0.5)
    
    async def _monitorProgress(self, enterpriseId: str, fileId: str, taskId: str):
        """监控处理进度"""
        uploadedFile = self._metadata[enterpriseId][fileId]
        
        while True:
            progress = self._pipeline.getProgress(taskId)
            
            if progress:
                await self._notifyProgress(progress)
                
                if progress.status == ProcessingStatus.COMPLETED:
                    uploadedFile.status = "completed"
                    uploadedFile.processedAt = datetime.now()
                    uploadedFile.documentId = progress.documentId
                    uploadedFile.chunkCount = progress.chunkCount
                    uploadedFile.vectorCount = progress.vectorCount
                    self._sync_file_index_ledger(uploadedFile, progress.indexLedger)
                    self._saveMetadata()
                    break
                
                elif progress.status == ProcessingStatus.FAILED:
                    uploadedFile.status = "failed"
                    uploadedFile.errorMessage = progress.errorMessage
                    uploadedFile.processedAt = datetime.now()
                    self._saveMetadata()
                    break

                elif progress.status == ProcessingStatus.OCR_REQUIRED:
                    uploadedFile.status = "pending_ocr"
                    uploadedFile.errorMessage = progress.errorMessage
                    uploadedFile.processedAt = datetime.now()
                    self._saveMetadata()
                    break
                
                elif progress.status == ProcessingStatus.CANCELLED:
                    uploadedFile.status = "cancelled"
                    uploadedFile.processedAt = datetime.now()
                    self._saveMetadata()
                    break
            
            await asyncio.sleep(0.5)
    
    def getProcessingProgress(self, taskId: str) -> Optional[ProcessingProgress]:
        """获取处理进度"""
        return self._pipeline.getProgress(taskId)
    
    def getFile(self, enterpriseId: str, fileId: str) -> Optional[UploadedFile]:
        """获取文件信息"""
        if enterpriseId in self._metadata and fileId in self._metadata[enterpriseId]:
            return self._metadata[enterpriseId][fileId]
        for entId, entFiles in self._metadata.items():
            if fileId in entFiles:
                return entFiles[fileId]
        return None
    
    def getFileByTaskId(self, taskId: str) -> Optional[UploadedFile]:
        """根据任务ID获取文件"""
        for entId, files in self._metadata.items():
            for file in files.values():
                if file.taskId == taskId:
                    return file
        return None
    
    def listFiles(
        self,
        enterpriseId: Optional[str] = None,
        category: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[UploadedFile]:
        """列出文件"""
        if enterpriseId:
            files = list(self._metadata.get(enterpriseId, {}).values())
        else:
            files = []
            for entId, entFiles in self._metadata.items():
                files.extend(entFiles.values())
        
        if category:
            files = [f for f in files if f.category == category]
        
        if status and status != "all":
            files = [f for f in files if f.status == status]
        
        return sorted(files, key=lambda x: x.createdAt, reverse=True)
    
    def deleteFile(self, enterpriseId: str, fileId: str) -> bool:
        """删除文件"""
        uploadedFile = None
        actualEnterpriseId = None
        
        if enterpriseId and enterpriseId in self._metadata and fileId in self._metadata[enterpriseId]:
            uploadedFile = self._metadata[enterpriseId][fileId]
            actualEnterpriseId = enterpriseId
        elif not enterpriseId:
            for entId, entFiles in self._metadata.items():
                if fileId in entFiles:
                    uploadedFile = entFiles[fileId]
                    actualEnterpriseId = entId
                    break
        
        if not uploadedFile:
            return False

        self._clear_indexed_artifacts(uploadedFile)
        
        if os.path.exists(uploadedFile.filePath):
            os.remove(uploadedFile.filePath)
        
        del self._metadata[actualEnterpriseId][fileId]
        self._saveMetadata()
        
        return True
    
    def getStorageStats(self, enterpriseId: str) -> Dict:
        """获取存储统计"""
        files = self.listFiles(enterpriseId)
        
        totalSize = sum(f.fileSize for f in files)
        totalChunks = sum(f.chunkCount for f in files)
        totalVectors = sum(f.vectorCount for f in files)
        
        categoryStats = {}
        statusStats = {}
        
        for f in files:
            if f.category not in categoryStats:
                categoryStats[f.category] = {"count": 0, "size": 0}
            categoryStats[f.category]["count"] += 1
            categoryStats[f.category]["size"] += f.fileSize
            
            if f.status not in statusStats:
                statusStats[f.status] = 0
            statusStats[f.status] += 1
        
        return {
            "totalFiles": len(files),
            "totalSize": totalSize,
            "totalSizeMB": round(totalSize / 1024 / 1024, 2),
            "totalChunks": totalChunks,
            "totalVectors": totalVectors,
            "categoryStats": categoryStats,
            "statusStats": statusStats
        }
    
    def isSupported(self, fileName: str) -> bool:
        """检查文件是否支持"""
        ext = os.path.splitext(fileName)[1].lower()
        return ext in self.config.allowedExtensions
    
    def getSupportedExtensions(self) -> List[str]:
        """获取支持的文件扩展名"""
        return list(self.config.allowedExtensions)
    
    def getFileContent(self, enterpriseId: str, fileId: str) -> Optional[str]:
        """
        获取文件原始文本内容
        
        Args:
            enterpriseId: 企业ID
            fileId: 文件ID
            
        Returns:
            文件文本内容，如果文件不存在或无法读取则返回 None
        """
        file_info = self.getFile(enterpriseId, fileId)
        if not file_info:
            return None
        
        try:
            file_path = file_info.filePath
            if not os.path.exists(file_path):
                return None
            
            file_ext = os.path.splitext(file_info.originalName)[1].lower()
            
            if file_ext in ['.txt', '.md', '.json', '.csv']:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return f.read()
            elif file_ext == '.pdf':
                try:
                    import PyPDF2
                    text_content = []
                    with open(file_path, 'rb') as f:
                        reader = PyPDF2.PdfReader(f)
                        for page in reader.pages:
                            text = page.extract_text()
                            if text:
                                text_content.append(text)
                    return '\n\n'.join(text_content)
                except ImportError:
                    return "[PDF内容无法提取：缺少PyPDF2库]"
                except Exception as e:
                    return f"[PDF内容提取失败：{str(e)}]"
            elif file_ext in ['.doc', '.docx']:
                try:
                    from docx import Document
                    doc = Document(file_path)
                    return '\n'.join([para.text for para in doc.paragraphs])
                except ImportError:
                    return "[Word文档无法提取：缺少python-docx库]"
                except Exception as e:
                    return f"[Word文档提取失败：{str(e)}]"
            else:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        return f.read()
                except Exception:
                    return f"[无法读取此格式文件：{file_ext}]"
                    
        except Exception as e:
            logger.error(f"读取文件内容失败: {e}")
            return None
    
    def _checkExistingKnowledge(self, fileId: str) -> bool:
        """
        检查知识库中是否有来自指定文件的知识点
        
        Args:
            fileId: 文件ID
            
        Returns:
            是否存在知识点
        """
        if not self.knowledgeService:
            return False
        
        try:
            for item in self.knowledgeService._items:
                metadata = item.metadata or {}
                if metadata.get("fileId") == fileId:
                    return True
            return False
        except Exception as e:
            logger.warning(f"检查知识点失败: {e}")
            return False

    def _resolve_index_ledger(self, uploadedFile: UploadedFile) -> Dict:
        ledger = dict(uploadedFile.indexLedger or {})
        pipeline_ledger = None
        if hasattr(self._pipeline, "getDocumentLedger"):
            pipeline_ledger = self._pipeline.getDocumentLedger(
                uploadedFile.enterpriseId,
                fileId=uploadedFile.id,
                documentId=uploadedFile.documentId,
                contentHash=uploadedFile.contentHash,
            )
        if pipeline_ledger:
            ledger.update(pipeline_ledger)
        return ledger

    def _sync_file_index_ledger(self, uploadedFile: UploadedFile, ledger: Optional[Dict] = None):
        resolved = dict(ledger or self._resolve_index_ledger(uploadedFile) or {})
        uploadedFile.indexLedger = copy.deepcopy(resolved)
        if resolved.get("documentId"):
            uploadedFile.documentId = str(resolved.get("documentId") or "")
        if resolved.get("chunkCount") is not None:
            uploadedFile.chunkCount = int(resolved.get("chunkCount") or 0)
        if resolved.get("vectorCount") is not None:
            uploadedFile.vectorCount = int(resolved.get("vectorCount") or 0)

    def _clear_indexed_artifacts(self, uploadedFile: UploadedFile):
        ledger = self._resolve_index_ledger(uploadedFile)
        enterpriseId = uploadedFile.enterpriseId

        if self.vectorStore:
            try:
                deleted_chunk_ids = set()
                for chunk_id in ledger.get("chunkIds", []) or []:
                    chunk_id = str(chunk_id or "").strip()
                    if not chunk_id or chunk_id in deleted_chunk_ids:
                        continue
                    self.vectorStore.deleteDocument(enterpriseId, chunk_id)
                    deleted_chunk_ids.add(chunk_id)

                delete_filters = []
                if uploadedFile.id:
                    delete_filters.append({"fileId": uploadedFile.id})
                if uploadedFile.documentId:
                    delete_filters.append({"documentId": uploadedFile.documentId})
                if ledger.get("documentId"):
                    delete_filters.append({"documentId": ledger.get("documentId")})

                seen_filters = set()
                for filter_conditions in delete_filters:
                    normalized = tuple(sorted((filter_conditions or {}).items()))
                    if not normalized or normalized in seen_filters:
                        continue
                    seen_filters.add(normalized)
                    result = self.vectorStore.deleteDocumentsByFilter(enterpriseId, filter_conditions)
                    if not result.get("success"):
                        raise RuntimeError(result.get("error") or "向量过滤删除未成功")
            except Exception as e:
                raise RuntimeError(f"删除向量数据失败: {e}") from e

        if self.knowledgeService:
            try:
                item_ids = []
                item_ids.extend(str(item_id or "").strip() for item_id in (ledger.get("knowledgeItemIds", []) or []))
                for item in list(self.knowledgeService._items):
                    metadata = item.metadata or {}
                    if metadata.get("fileId") == uploadedFile.id:
                        item_ids.append(item.id)
                        continue
                    if uploadedFile.documentId and metadata.get("documentId") == uploadedFile.documentId:
                        item_ids.append(item.id)
                        continue
                    if ledger.get("documentId") and metadata.get("documentId") == ledger.get("documentId"):
                        item_ids.append(item.id)
                for itemId in dict.fromkeys(item_ids):
                    if itemId:
                        self.knowledgeService.delete_item(itemId)
            except Exception as e:
                raise RuntimeError(f"删除知识点失败: {e}") from e

        deleted_ledger = None
        if hasattr(self._pipeline, "removeDocumentLedger"):
            deleted_ledger = self._pipeline.removeDocumentLedger(
                enterpriseId,
                fileId=uploadedFile.id,
                documentId=uploadedFile.documentId or str(ledger.get("documentId") or ""),
                contentHash=uploadedFile.contentHash or str(ledger.get("contentHash") or ""),
                deleteStage="artifacts_cleared",
            )

        uploadedFile.indexLedger = copy.deepcopy(deleted_ledger or ledger or {})
        uploadedFile.documentId = ""
        uploadedFile.chunkCount = 0
        uploadedFile.vectorCount = 0
    
    async def _forceProcessFile(self, uploadedFile: UploadedFile):
        """
        强制处理文件（跳过缓存检查）
        
        当文件内容已存在但知识点不存在时使用
        
        Args:
            uploadedFile: 上传文件信息
        """
        try:
            # 读取文件内容
            content = await self._readFileContent(uploadedFile.filePath)
            
            if not content or len(content.strip()) < 50:
                uploadedFile.status = "completed"
                uploadedFile.errorMessage = "文件内容过短，无需处理"
                uploadedFile.processedAt = datetime.now()
                self._saveMetadata()
                return
            
            # 提取知识点
            if self.knowledgeService:
                extracted_count = await self._extractKnowledgeFromFile(
                    uploadedFile, content
                )
                uploadedFile.chunkCount = extracted_count
                logger.info(f"从文件 {uploadedFile.originalName} 提取了 {extracted_count} 个知识点")
            
            uploadedFile.status = "completed"
            uploadedFile.processedAt = datetime.now()
            self._sync_file_index_ledger(uploadedFile)
            self._saveMetadata()
            
        except Exception as e:
            uploadedFile.status = "failed"
            uploadedFile.errorMessage = str(e)
            uploadedFile.processedAt = datetime.now()
            self._saveMetadata()
            logger.error(f"强制处理文件失败: {e}")
    
    async def _readFileContent(self, filePath: str) -> str:
        """
        读取文件内容
        
        Args:
            filePath: 文件路径
            
        Returns:
            文件内容
        """
        file_ext = os.path.splitext(filePath)[1].lower()
        
        try:
            if file_ext in ['.txt', '.md', '.json', '.csv']:
                async with aiofiles.open(filePath, 'r', encoding='utf-8') as f:
                    return await f.read()
            
            elif file_ext == '.pdf':
                import PyPDF2
                content = []
                with open(filePath, 'rb') as f:
                    reader = PyPDF2.PdfReader(f)
                    for page in reader.pages:
                        text = page.extract_text()
                        if text:
                            content.append(text)
                return '\n\n'.join(content)
            
            elif file_ext in ['.doc', '.docx']:
                from docx import Document
                doc = Document(filePath)
                return '\n'.join([para.text for para in doc.paragraphs])
            
            else:
                # 尝试文本方式读取
                async with aiofiles.open(filePath, 'r', encoding='utf-8') as f:
                    return await f.read()
                    
        except Exception as e:
            logger.error(f"读取文件失败: {e}")
            return ""
    
    async def _extractKnowledgeFromFile(self, uploadedFile: UploadedFile, content: str) -> int:
        """
        从文件内容提取知识点
        
        Args:
            uploadedFile: 上传文件信息
            content: 文件内容
            
        Returns:
            提取的知识点数量
        """
        if not self.knowledgeService:
            return 0
        
        extracted_count = 0
        
        # 分块处理
        chunks = self._splitContent(content, chunk_size=500)
        
        for chunk in chunks:
            if len(chunk.strip()) < 50:
                continue
            
            # 提取问答对
            qa_pairs = self._extractQAFromContent(chunk)
            
            for question, answer in qa_pairs:
                if len(question) > 5 and len(answer) > 10:
                    item_data = {
                        "question": question,
                        "answer": answer,
                        "category": uploadedFile.category,
                        "enterprise_id": str(uploadedFile.enterpriseId or "").strip(),
                        "keywords": self._extractKeywords(question + " " + answer),
                        "source": "document_upload",
                        "enabled": True,
                        "priority": 10,
                        "metadata": {
                            "documentId": uploadedFile.documentId or uploadedFile.id,
                            "originalName": uploadedFile.originalName,
                            "fileId": uploadedFile.id
                        }
                    }
                    
                    try:
                        self.knowledgeService.add_item(item_data)
                        extracted_count += 1
                    except Exception as e:
                        logger.warning(f"添加知识点失败: {e}")
        
        return extracted_count
    
    def _splitContent(self, content: str, chunk_size: int = 500) -> List[str]:
        """
        分割内容为块
        
        Args:
            content: 内容
            chunk_size: 块大小
            
        Returns:
            内容块列表
        """
        chunks = []
        
        # 按段落分割
        paragraphs = content.split('\n\n')
        
        current_chunk = ""
        for para in paragraphs:
            if len(current_chunk) + len(para) < chunk_size:
                current_chunk += para + "\n\n"
            else:
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                current_chunk = para + "\n\n"
        
        if current_chunk.strip():
            chunks.append(current_chunk.strip())
        
        return chunks
    
    def _extractQAFromContent(self, content: str) -> List[tuple]:
        """
        从内容中提取问答对
        
        Args:
            content: 内容
            
        Returns:
            问答对列表
        """
        import re
        qa_pairs = []
        
        # 模式1: Q: xxx A: xxx 格式
        qa_pattern1 = r'Q[：:]\s*(.+?)\s*A[：:]\s*(.+?)(?=Q[：:]|$)'
        matches1 = re.findall(qa_pattern1, content, re.DOTALL)
        for q, a in matches1:
            qa_pairs.append((q.strip(), a.strip()))
        
        # 模式2: 问题：xxx 答案：xxx 格式
        qa_pattern2 = r'问题[：:]\s*(.+?)\s*答案[：:]\s*(.+?)(?=问题[：:]|$)'
        matches2 = re.findall(qa_pattern2, content, re.DOTALL)
        for q, a in matches2:
            qa_pairs.append((q.strip(), a.strip()))
        
        # 模式3: 问句格式
        if not qa_pairs:
            lines = content.split('\n')
            current_question = None
            current_answer = []
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # 检测问句
                if len(line) < 50 and (line.endswith('？') or line.endswith('?') or
                    line.startswith(('如何', '怎么', '什么是', '为什么', '能不能', '是否'))):
                    if current_question and current_answer:
                        qa_pairs.append((current_question, '\n'.join(current_answer)))
                    current_question = line
                    current_answer = []
                else:
                    if current_question:
                        current_answer.append(line)
                    elif len(line) > 50:
                        # 没有问句时，使用前30字符作为问题
                        qa_pairs.append((line[:30] + "...", line))
            
            if current_question and current_answer:
                qa_pairs.append((current_question, '\n'.join(current_answer)))
        
        return qa_pairs[:10]  # 限制每个块最多10个知识点
    
    def _extractKeywords(self, text: str) -> List[str]:
        """
        从文本中提取关键词
        
        Args:
            text: 文本
            
        Returns:
            关键词列表
        """
        import re
        keywords = []
        
        # 提取2-4字的中文词组
        chinese_pattern = r'[\u4e00-\u9fa5]{2,4}'
        matches = re.findall(chinese_pattern, text)
        
        # 过滤停用词
        stop_words = {'的', '是', '在', '有', '和', '了', '对', '这', '那', '我', '你', '他', '她', '它'}
        
        for word in matches:
            if word not in stop_words and word not in keywords:
                keywords.append(word)
        
        return keywords[:5]


_service_instance = None
_OCR_EXTRACTOR_UNSET = object()


def get_enterprise_file_upload_service(
    embeddingService=None,
    vectorStore=None,
    knowledgeService=None,
    ocrExtractor: Optional[Callable[[str], str]] = _OCR_EXTRACTOR_UNSET,
    reset: bool = False
) -> EnterpriseFileUploadService:
    """
    获取企业级文件上传服务单例
    
    Args:
        embeddingService: 嵌入服务
        vectorStore: 向量存储
        knowledgeService: 知识服务
        reset: 是否重置
        
    Returns:
        EnterpriseFileUploadService实例
    """
    global _service_instance
    resolved_ocr_extractor = (
        build_document_ocr_extractor_from_env()
        if ocrExtractor is _OCR_EXTRACTOR_UNSET
        else ocrExtractor
    )
    
    if _service_instance is None or reset:
        _service_instance = EnterpriseFileUploadService(
            embeddingService=embeddingService,
            vectorStore=vectorStore,
            knowledgeService=knowledgeService,
            config=FileUploadConfig(ocrExtractor=resolved_ocr_extractor) if resolved_ocr_extractor is not None else None,
        )
        logger.info("创建新的企业级文件上传服务实例")
    else:
        # 更新现有实例的服务引用
        if knowledgeService is not None and _service_instance.knowledgeService is None:
            _service_instance.knowledgeService = knowledgeService
            _service_instance._pipeline.knowledgeService = knowledgeService
            logger.info("更新文件上传服务的知识服务引用")
        if embeddingService is not None and _service_instance.embeddingService is None:
            _service_instance.embeddingService = embeddingService
            _service_instance._pipeline.embeddingService = embeddingService
        if vectorStore is not None and _service_instance.vectorStore is None:
            _service_instance.vectorStore = vectorStore
            _service_instance._pipeline.vectorStore = vectorStore
        current_ocr_extractor = getattr(_service_instance.config, "ocrExtractor", None)
        if ocrExtractor is not _OCR_EXTRACTOR_UNSET:
            if current_ocr_extractor is not resolved_ocr_extractor:
                _service_instance.setOcrExtractor(resolved_ocr_extractor)
        elif current_ocr_extractor is None and resolved_ocr_extractor is not None:
            _service_instance.setOcrExtractor(resolved_ocr_extractor)
    
    return _service_instance
