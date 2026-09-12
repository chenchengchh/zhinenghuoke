"""
LLM智能回复模块

基于LangChain架构设计，支持多种LLM providers
"""
import os
import json
import time
import threading
import re
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from loguru import logger

from src.config.settings import (
    REPLY_LLM_TIMEOUT_SECONDS,
    OLLAMA_NUM_CTX,
    OLLAMA_NUM_PREDICT,
    OLLAMA_TEMPERATURE,
    OLLAMA_TOP_K,
    OLLAMA_TOP_P,
    OLLAMA_ENABLE_THINKING,
    OLLAMA_THINKING_DEPTH,
)
from src.infrastructure.env_loader import load_project_env

def _load_env_file():
    """加载环境变量，避免在模块级日志中出现 <module> 格式化标签。"""
    try:
        env_path = load_project_env(override=False)
        if env_path:
            logger.info(f"环境变量已从指定 .env 文件加载: {env_path}")
        else:
            logger.info("未发现可加载的 .env 文件，继续使用当前进程环境变量")
    except ImportError:
        logger.warning("python-dotenv未安装，使用系统环境变量")
    except Exception as e:
        logger.warning(f"加载.env文件失败: {e}")


_load_env_file()


# 使用统一的意图类型定义
from .types.intent import IntentType, SentimentType, UrgencyLevel, IntentResult


from .types.knowledge import KnowledgeItem as UnifiedKnowledgeItem


def _build_provider_fallback_response(reason: str, prompt: str = "") -> str:
    """provider 不可用时统一返回空串，由上游决定是否回复。"""
    del reason, prompt
    return ""


def _looks_like_hidden_reasoning_output(text: str) -> bool:
    content = str(text or "").strip()
    if not content:
        return False
    lowered = content.lower()
    strong_markers = [
        r"\bthinking process\b",
        r"\banalyze the request\b",
        r"\*\*role:\*\*",
        r"\*\*task:\*\*",
        r"\*\*industry:\*\*",
        r"\*\*constraints:\*\*",
        r"\bprofessional sales consultant\b",
        r"\bgeneral service sales\b",
    ]
    matched = sum(1 for pattern in strong_markers if re.search(pattern, lowered, re.IGNORECASE))
    if matched >= 2:
        return True
    return lowered.startswith("thinking process") or lowered.startswith("1. **analyze the request:**")


def _sanitize_provider_reply(text: str, *, source: str, done_reason: str = "") -> str:
    """过滤 provider 返回中的隐藏推理泄漏，避免把思维链当正式回复。"""
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    if _looks_like_hidden_reasoning_output(normalized):
        logger.warning(
            f"[LLM] 丢弃隐藏推理泄漏 source={source} "
            f"done_reason={str(done_reason or '').strip() or '-'} "
            f"text_len={len(normalized)}"
        )
        return ""
    return normalized


class LLMProvider(ABC):
    """LLM Provider抽象基类"""
    
    @abstractmethod
    def chat(self, prompt: str, system_prompt: str = "", **kwargs) -> str:
        """发送对话请求"""
        pass
    
    def generate(self, prompt: str, **kwargs) -> str:
        """生成文本（默认委托给chat方法）"""
        return self.chat(prompt, **kwargs)
    
    @abstractmethod
    def embeddings(self, texts: List[str]) -> List[List[float]]:
        """获取文本嵌入"""
        pass


class OpenAIProvider(LLMProvider):
    """OpenAI Provider"""
    
    def __init__(self, api_key: str = None, base_url: str = None, model: str = "gpt-4"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.model = model
        
        try:
            from openai import OpenAI
            import httpx
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=httpx.Timeout(30.0, connect=10.0)
            )
        except ImportError:
            logger.warning("openai库未安装，将使用模拟响应")
            self.client = None
    
    def chat(self, prompt: str, system_prompt: str = "", **kwargs) -> str:
        """发送对话请求"""
        if not self.client:
            return self._mock_response(prompt)
        
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            
            response = self.client.chat.completions.create(
                model=kwargs.get("model", self.model),
                messages=messages,
                temperature=kwargs.get("temperature", 0.7),
                max_tokens=kwargs.get("max_tokens", 1000)
            )
            
            content = response.choices[0].message.content
            sanitized = _sanitize_provider_reply(content, source="openai")
            return sanitized if sanitized else self._mock_response(prompt)
            
        except Exception as e:
            logger.error(f"OpenAI API调用失败: {e}")
            return self._mock_response(prompt)
    
    def embeddings(self, texts: List[str]) -> List[List[float]]:
        """获取文本嵌入"""
        if not self.client:
            return [[0.0] * 1536 for _ in texts]
        
        try:
            response = self.client.embeddings.create(
                model="text-embedding-ada-002",
                input=texts
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            logger.error(f"获取嵌入失败: {e}")
            return [[0.0] * 1536 for _ in texts]
    
    def _mock_response(self, prompt: str) -> str:
        """模拟响应（标记为降级响应）"""
        logger.warning("LLM服务不可用，返回降级响应")
        return _build_provider_fallback_response("llm_unavailable", prompt)


class LocalLLMProvider(LLMProvider):
    """本地LLM Provider (Ollama)"""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3.5:4b",
        keep_alive: str = "24h",
    ):
        self.base_url = base_url
        self.model = model
        self.keep_alive = keep_alive

    @staticmethod
    def _build_ollama_options(**kwargs) -> Dict[str, Any]:
        requested_enable_thinking = kwargs.get("enable_thinking")
        requested_thinking_depth = kwargs.get("thinking_depth")
        if requested_enable_thinking not in (None, OLLAMA_ENABLE_THINKING):
            logger.warning(
                f"[OLLAMA-RUNTIME] 忽略外部 enable_thinking={requested_enable_thinking}，"
                f"强制使用系统配置 {OLLAMA_ENABLE_THINKING}"
            )
        if requested_thinking_depth not in (None, OLLAMA_THINKING_DEPTH):
            logger.warning(
                f"[OLLAMA-RUNTIME] 忽略外部 thinking_depth={requested_thinking_depth}，"
                f"强制使用系统配置 {OLLAMA_THINKING_DEPTH}"
            )
        return {
            "num_ctx": int(kwargs.get("num_ctx", OLLAMA_NUM_CTX)),
            "num_predict": int(kwargs.get("num_predict", OLLAMA_NUM_PREDICT)),
            "temperature": float(kwargs.get("temperature", OLLAMA_TEMPERATURE)),
            "top_p": float(kwargs.get("top_p", OLLAMA_TOP_P)),
            "top_k": int(kwargs.get("top_k", OLLAMA_TOP_K)),
            "enable_thinking": bool(OLLAMA_ENABLE_THINKING),
            "thinking_depth": int(OLLAMA_THINKING_DEPTH),
        }

    @staticmethod
    def _build_ollama_think_flag(**kwargs):
        requested_enable_thinking = kwargs.get("enable_thinking")
        if requested_enable_thinking not in (None, OLLAMA_ENABLE_THINKING):
            logger.warning(
                f"[OLLAMA-RUNTIME] 忽略外部 think={requested_enable_thinking}，"
                f"强制使用系统配置 {OLLAMA_ENABLE_THINKING}"
            )
        return bool(OLLAMA_ENABLE_THINKING)

    def generate(self, prompt: str, **kwargs) -> str:
        """生成文本（兼容接口，委托给chat方法）"""
        return self.chat(prompt, **kwargs)

    def chat(self, prompt: str, system_prompt: str = "", **kwargs) -> str:
        """发送对话请求（带超时保护，防止长时间阻塞消息处理线程）"""
        started_at = time.perf_counter()
        timeout = float(kwargs.get("timeout", REPLY_LLM_TIMEOUT_SECONDS))
        resolved_model = kwargs.get("model", self.model)
        prompt_len = len(str(prompt or ""))
        try:
            import requests

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            keep_alive = kwargs.get("keep_alive", self.keep_alive)
            options = self._build_ollama_options(**kwargs)
            think_flag = self._build_ollama_think_flag(**kwargs)
            logger.info(
                f"[OLLAMA-RUNTIME] start model={resolved_model} timeout={float(timeout):.1f}s "
                f"prompt_len={prompt_len} keep_alive={keep_alive} num_ctx={options['num_ctx']} "
                f"num_predict={options['num_predict']} enable_thinking={options['enable_thinking']} "
                f"thinking_depth={options['thinking_depth']} think={think_flag}"
            )
            primary_error = None
            try:
                response = self._chat_once(
                    requests=requests,
                    model_name=resolved_model,
                    messages=messages,
                    keep_alive=keep_alive,
                    options=options,
                    think_flag=think_flag,
                    timeout=timeout,
                    started_at=started_at,
                )
            except Exception as exc:
                primary_error = exc
                elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
                logger.error(
                    f"[OLLAMA-RUNTIME] exception model={resolved_model} elapsed={elapsed_ms}ms "
                    f"error={str(exc)[:160]}"
                )
                logger.error(f"本地LLM调用失败: {exc}")
                response = ""
            if response:
                return response

            fallback_model = str(os.getenv("OLLAMA_FALLBACK_MODEL", "qwen2.5:3b") or "").strip()
            if fallback_model and fallback_model != resolved_model:
                logger.warning(
                    f"[OLLAMA-RUNTIME] 主模型不可用，尝试备用本地模型: {fallback_model} "
                    f"reason={str(primary_error)[:80] if primary_error else 'empty_or_non_200'}"
                )
                retry_started_at = time.perf_counter()
                try:
                    fallback_response = self._chat_once(
                        requests=requests,
                        model_name=fallback_model,
                        messages=messages,
                        keep_alive=keep_alive,
                        options=options,
                        think_flag=think_flag,
                        timeout=timeout,
                        started_at=retry_started_at,
                    )
                except Exception as exc:
                    elapsed_ms = round((time.perf_counter() - retry_started_at) * 1000, 1)
                    logger.error(
                        f"[OLLAMA-RUNTIME] exception model={fallback_model} elapsed={elapsed_ms}ms "
                        f"error={str(exc)[:160]}"
                    )
                    logger.error(f"备用本地LLM调用失败: {exc}")
                    fallback_response = ""
                if fallback_response:
                    return fallback_response

        except Exception as e:
            elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
            logger.error(
                f"[OLLAMA-RUNTIME] exception model={resolved_model} elapsed={elapsed_ms}ms "
                f"error={str(e)[:160]}"
            )
            logger.error(f"本地LLM调用失败: {e}")

        return _build_provider_fallback_response("llm_unavailable", prompt)

    def _chat_once(
        self,
        *,
        requests,
        model_name: str,
        messages: List[Dict[str, str]],
        keep_alive: str,
        options: Dict[str, Any],
        think_flag,
        timeout: float,
        started_at: float,
    ) -> str:
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": model_name,
                "messages": messages,
                "stream": False,
                "keep_alive": keep_alive,
                "think": think_flag,
                "options": options,
            },
            timeout=timeout
        )

        if response.status_code == 200:
            resp_json = response.json()
            msg = resp_json.get("message", {})
            content = msg.get("content", "")
            thinking = msg.get("thinking", "")
            done_reason = resp_json.get("done_reason", "")
            elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
            logger.info(
                f"[OLLAMA-RUNTIME] done model={model_name} elapsed={elapsed_ms}ms "
                f"status_code=200 done_reason={done_reason} "
                f"content_len={len(str(content or ''))} thinking_len={len(str(thinking or ''))}"
            )
            sanitized_content = _sanitize_provider_reply(
                content,
                source="ollama_content",
                done_reason=done_reason,
            )
            if sanitized_content:
                return sanitized_content
            if str(thinking or "").strip():
                logger.warning(
                    f"[LLM] 丢弃 thinking 字段输出 model={model_name} "
                    f"done_reason={str(done_reason or '').strip() or '-'} "
                    f"thinking_len={len(str(thinking or '').strip())} "
                    f"enable_thinking={options.get('enable_thinking')} "
                    f"thinking_depth={options.get('thinking_depth')} "
                    f"think={think_flag}"
                )
            return ""

        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        logger.warning(
            f"[OLLAMA-RUNTIME] http_non_200 model={model_name} elapsed={elapsed_ms}ms "
            f"status_code={response.status_code}"
        )
        logger.warning(f"本地LLM响应状态码: {response.status_code}")
        return ""
    
    def embeddings(self, texts: List[str]) -> List[List[float]]:
        """获取文本嵌入"""
        # 本地模型可能需要单独的嵌入服务
        return [[0.0] * 768 for _ in texts]


class QwenProvider(LLMProvider):
    """
    阿里云千问（百炼）Provider
    
    支持通过DashScope API调用千问系列模型
    """
    
    def __init__(self, api_key: str = None, model: str = "qwen-plus"):
        """
        初始化千问Provider
        
        Args:
            api_key: DashScope API密钥
            model: 模型名称 (qwen-turbo, qwen-plus, qwen-max等)
        """
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        self.model = model
        self.base_url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
        self.embedding_url = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
        
        if not self.api_key:
            logger.warning("DASHSCOPE_API_KEY未设置，千问服务将返回模拟响应")
    
    def chat(self, prompt: str, system_prompt: str = "", **kwargs) -> str:
        """
        发送对话请求
        
        Args:
            prompt: 用户输入
            system_prompt: 系统提示
            **kwargs: 额外参数
            
        Returns:
            模型响应文本
        """
        if not self.api_key:
            return self._mock_response(prompt)
        
        try:
            import requests
            
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": kwargs.get("model", self.model),
                "input": {
                    "messages": messages
                },
                "parameters": {
                    "max_tokens": kwargs.get("max_tokens", 2000),
                    "temperature": kwargs.get("temperature", 0.7),
                    "result_format": "text"
                }
            }
            
            timeout = kwargs.get("timeout", 30)
            response = requests.post(
                self.base_url,
                headers=headers,
                json=data,
                timeout=timeout
            )
            
            if response.status_code == 200:
                result = response.json()
                return _sanitize_provider_reply(
                    result.get("output", {}).get("text", ""),
                    source="qwen",
                )
            else:
                logger.error(f"千问API调用失败: {response.status_code} - {response.text}")
                return self._mock_response(prompt)
                
        except Exception as e:
            logger.error(f"千问API调用异常: {e}")
            return self._mock_response(prompt)
    
    def embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        获取文本嵌入向量
        
        Args:
            texts: 文本列表
            
        Returns:
            嵌入向量列表
        """
        if not self.api_key:
            return [[0.0] * 1536 for _ in texts]
        
        try:
            import requests
            
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": "text-embedding-v2",
                "input": {
                    "texts": texts
                },
                "parameters": {
                    "text_type": "query"
                }
            }
            
            response = requests.post(
                self.embedding_url,
                headers=headers,
                json=data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                embeddings = result.get("output", {}).get("embeddings", [])
                return [item.get("embedding", []) for item in embeddings]
            else:
                logger.error(f"千问Embedding API调用失败: {response.status_code}")
                return [[0.0] * 1536 for _ in texts]
                
        except Exception as e:
            logger.error(f"千问Embedding API调用异常: {e}")
            return [[0.0] * 1536 for _ in texts]
    
    def _mock_response(self, prompt: str) -> str:
        """模拟响应（标记为降级响应）"""
        logger.warning("LLM服务不可用，返回降级响应")
        return _build_provider_fallback_response("llm_unavailable", prompt)



class KnowledgeBase:
    """知识库"""
    
    def __init__(self, llm_provider: LLMProvider = None):
        self.llm = llm_provider
        self.knowledge_items: List[UnifiedKnowledgeItem] = []
        self._init_default_knowledge()
    
    def _init_default_knowledge(self):
        """初始化默认知识。

        不再在代码里内置行业知识点，避免知识库为空时回落到写死业务答案。
        """
        self.knowledge_items = []
    
    def add_knowledge(self, item: UnifiedKnowledgeItem):
        """添加知识条目"""
        self.knowledge_items.append(item)
    
    def search(self, query: str, top_k: int = 3) -> List[UnifiedKnowledgeItem]:
        """搜索知识"""
        results = []
        query_lower = query.lower()
        
        for item in self.knowledge_items:
            score = 0
            
            # 标题匹配
            if any(kw in item.title.lower() for kw in query_lower.split()):
                score += 0.5
            
            # 内容匹配
            if any(kw in item.content.lower() for kw in query_lower.split()):
                score += 0.3
            
            # 关键词匹配
            for keyword in item.keywords:
                if keyword in query_lower:
                    score += 0.2
            
            if score > 0:
                item.score = score
                results.append(item)
        
        # 返回Top-K
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]


class ReplyGenerator:
    """回复生成器（已废弃：请使用 EnhancedCustomerService 替代）"""
    
    def __init__(self, llm_provider: LLMProvider = None):
        self.llm = llm_provider
        self.knowledge_base = KnowledgeBase(llm_provider)
    
    def generate(self, message: str, customer_name: str = "客户", 
                conversation_history: List[Dict] = None) -> tuple:
        """生成回复，返回 (回复内容, 意图结果)"""
        try:
            from .intent_orchestrator import IntentOrchestrator
            orchestrator = IntentOrchestrator()
            intent_result = orchestrator.recognize(message)
        except Exception:
            intent_result = IntentResult(
                primaryIntent=IntentType.UNKNOWN,
                confidence=0.3,
                reasoning="IntentOrchestrator不可用"
            )
        
        knowledge_items = self.knowledge_base.search(message)
        
        if intent_result.confidence >= 0.8 and knowledge_items:
            reply = self._generate_from_knowledge(
                message, intent_result, knowledge_items, customer_name
            )
        elif self.llm:
            reply = self._generate_by_llm(
                message, intent_result, knowledge_items, customer_name, conversation_history
            )
        else:
            reply = self._generate_fallback(intent_result, customer_name)
        
        return reply, intent_result
    
    def _generate_from_knowledge(self, message: str, intent_result: IntentResult,
                                  knowledge_items: List[UnifiedKnowledgeItem],
                                  customer_name: str) -> str:
        """基于知识生成回复"""
        if knowledge_items:
            best_knowledge = knowledge_items[0]
            return str(best_knowledge.content or "").strip()
        
        return self._generate_fallback(intent_result, customer_name, message)
    
    def _generate_by_llm(self, message: str, intent_result: IntentResult,
                         knowledge_items: List[UnifiedKnowledgeItem],
                         customer_name: str,
                         conversation_history: List[Dict] = None) -> str:
        """基于LLM生成回复"""
        prompt = f"""你是一名通用业务客服助手。

客户昵称: {customer_name}
客户消息: {message}

识别到的意图: {intent_result.intent.value} (置信度: {intent_result.confidence})
"""
        
        if knowledge_items:
            prompt += "参考知识:\n"
            for item in knowledge_items[:2]:
                prompt += f"- {item.title}: {item.content}\n"
        
        if conversation_history:
            prompt += "\n对话历史:\n"
            for msg in conversation_history[-3:]:
                role = "客户" if msg.get("direction") == "inbound" else "客服"
                prompt += f"{role}: {msg.get('content')}\n"
        
        prompt += "\n请基于参考知识生成简洁、自然、以解决问题为导向的回复；如果知识不足，只做需求澄清，不要编造价格、优惠、政策或承诺(50-100字):"
        
        try:
            return self.llm.chat(prompt)
        except Exception as e:
            logger.error(f"LLM生成失败: {e}")
            if knowledge_items:
                return self._generate_from_knowledge(message, intent_result, knowledge_items, customer_name)
            return self._generate_fallback(intent_result, customer_name, message)
    
    def _generate_fallback(self, intent_result: IntentResult, customer_name: str, message: str = "") -> str:
        """无知识时委托统一 fallback 服务，避免重复维护硬编码模板。"""
        del intent_result
        try:
            from src.common.fallback_reply_service import get_fallback_reply_service

            fallback = get_fallback_reply_service().make_business_fallback_reply(
                customer_name,
                str(message or ""),
                "llm_service_fallback",
            )
            reply = str((fallback or {}).get("reply", "") or "").strip()
            if reply:
                return reply
        except Exception as exc:
            logger.debug(f"统一 fallback 服务不可用，退回通用兜底: {exc}")

        return "您好，您可以再具体说一下想了解的内容、使用场景或预算，我按您的情况继续帮您梳理。"


class ChatbotService:
    """聊天机器人服务"""
    
    def __init__(self, llm_provider: LLMProvider = None):
        self.llm = llm_provider
        self.reply_generator = ReplyGenerator(llm_provider)
    
    def chat(self, message: str, customer_name: str = "客户",
            conversation_history: List[Dict] = None) -> Dict[str, Any]:
        """
        处理对话
        
        Returns:
            Dict: {
                "reply": "回复内容",
                "intent": "意图类型",
                "confidence": 0.8,
                "need_human": False
            }
        """
        # 生成回复
        reply, intent_result = self.reply_generator.generate(
            message, customer_name, conversation_history
        )
        
        # 判断是否需要转人工
        need_human = (
            intent_result.intent == IntentType.COMPLAINT or
            intent_result.intent == IntentType.UNKNOWN and intent_result.confidence < 0.5
        )
        
        return {
            "reply": reply,
            "intent": intent_result.intent.value,
            "confidence": intent_result.confidence,
            "need_human": need_human
        }


def create_chatbot(llm_type: str = "openai", **kwargs) -> ChatbotService:
    """
    创建聊天机器人服务
    
    Args:
        llm_type: LLM类型 (openai, qwen, local, none)
        **kwargs: 额外参数
        
    Returns:
        ChatbotService实例
    """
    llm_provider = None
    
    if llm_type == "openai":
        llm_provider = OpenAIProvider(
            api_key=kwargs.get("api_key"),
            base_url=kwargs.get("base_url"),
            model=kwargs.get("model", "gpt-4")
        )
    elif llm_type == "qwen":
        llm_provider = QwenProvider(
            api_key=kwargs.get("api_key"),
            model=kwargs.get("model", "qwen-plus")
        )
    elif llm_type == "local":
        _default_ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        _default_ollama_model = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
        _default_keep_alive = os.getenv("OLLAMA_KEEP_ALIVE", "24h")
        llm_provider = LocalLLMProvider(
            base_url=kwargs.get("base_url", _default_ollama_host),
            model=kwargs.get("model", _default_ollama_model),
            keep_alive=kwargs.get("keep_alive", _default_keep_alive),
        )
    elif llm_type == "none":
        pass
    
    return ChatbotService(llm_provider)


_cached_llm_provider = None
_cached_llm_provider_time = 0
_LLM_PROVIDER_CACHE_TTL = 30
_llm_provider_lock = threading.Lock()

def get_default_llm_provider() -> LLMProvider:
    """
    获取默认的LLM Provider（带健康检查缓存，30秒内不重复检查）
    
    仅允许本地 Ollama。
    
    Returns:
        LLMProvider实例
    """
    global _cached_llm_provider, _cached_llm_provider_time
    
    import os
    current_time = time.time()
    
    with _llm_provider_lock:
        if _cached_llm_provider_time > 0 and (current_time - _cached_llm_provider_time) < _LLM_PROVIDER_CACHE_TTL:
            return _cached_llm_provider
    
    provider = None
    
    try:
        import requests
        ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        ollama_model = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
        ollama_fallback_model = os.getenv("OLLAMA_FALLBACK_MODEL", "qwen2.5:3b")
        ollama_keep_alive = os.getenv("OLLAMA_KEEP_ALIVE", "24h")
        response = requests.get(f"{ollama_host}/api/tags", timeout=3)
        if response.status_code == 200:
            logger.info(
                f"使用本地Ollama作为LLM Provider ({ollama_model}, fallback={ollama_fallback_model}, keep_alive={ollama_keep_alive})"
            )
            provider = LocalLLMProvider(
                base_url=ollama_host,
                model=ollama_model,
                keep_alive=ollama_keep_alive,
            )
    except Exception as e:
        logger.debug(f"Ollama服务检测失败: {e}")

    with _llm_provider_lock:
        if _cached_llm_provider_time > 0 and (current_time - _cached_llm_provider_time) < _LLM_PROVIDER_CACHE_TTL:
            return _cached_llm_provider
        _cached_llm_provider = provider
        _cached_llm_provider_time = current_time

    if not provider:
        logger.warning("未检测到可用的本地Ollama LLM Provider")
    
    return provider
