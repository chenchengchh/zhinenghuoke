"""
RAG 领域异常类

将宽泛的 `except Exception` 拆分为领域特定的异常类型，
便于调用方对症下药（重试/降级/熔断/告警）。
"""


class RAGError(Exception):
    """RAG 基础异常。"""
    pass


class RetrievalError(RAGError):
    """检索阶段异常（向量检索、关键词检索、KG 检索失败）。"""
    pass


class GenerationError(RAGError):
    """生成阶段异常（LLM 调用失败、模板渲染失败）。"""
    pass


class KnowledgeBaseError(RAGError):
    """知识库操作异常（加载、查询、写入失败）。"""
    pass


class EmbeddingError(RAGError):
    """嵌入计算异常。"""
    pass


class LLMTimeoutError(RAGError):
    """LLM 调用超时。"""
    pass


class GuardrailBlockedError(RAGError):
    """守卫阶段拦截（如敏感词、内容审核）。"""
    pass


class CacheError(RAGError):
    """缓存读写异常。"""
    pass
