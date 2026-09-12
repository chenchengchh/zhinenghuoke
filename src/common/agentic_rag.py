"""
Agentic RAG 系统 (AgenticRAGSystem)

基于Phase 3设计文档实现
自主Agent驱动的RAG系统

架构：
1. ReAct Agent框架 - 增强版推理引擎
2. 工具注册与调用 - 智能工具选择
3. 多步推理链 - 支持回溯和修正
4. 自我评估与修正 - 答案质量验证
5. 多维度置信度计算

注意：类型定义已统一到 src.common.types.rag
"""

import logging
import re
import time
import threading
import concurrent.futures
from typing import List, Dict, Optional, Any, Callable, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict

from .types.rag import (
    AgentAction, AgentThought, AgentResult
)
from .core_utils.confidence import ConfidenceCalculator
from .tracing_support import append_trace_span
from src.common.utils import call_llm_safe
from src.config.settings import REPLY_LLM_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


# 共享的 safe_math_eval 实现（来自 core_utils.safe_eval）
# 保留 _safe_math_eval 别名以保持向后兼容
from .core_utils.safe_eval import safe_math_eval as _safe_math_eval
from .core_utils.safe_eval import safe_math_eval  # 重新导出供外部使用


@dataclass
class Tool:
    """工具定义"""
    name: str
    description: str
    func: Callable
    parameters: Dict[str, Any] = field(default_factory=dict)
    priority: int = 0


@dataclass
class QueryAnalysis:
    """查询分析结果"""
    original_query: str
    intent: str = "unknown"
    complexity: float = 0.5
    keywords: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    suggested_tools: List[str] = field(default_factory=list)
    need_multi_step: bool = False


@dataclass
class AnswerCandidate:
    """答案候选"""
    answer: str
    source: str
    confidence: float
    retrieved_info: List[Dict] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)


AGENTIC_INTENT_PATTERNS = {
    "price": ["价格", "多少钱", "费用", "收费", "成本", "报价"],
    "product": ["功能", "产品", "介绍", "特点", "规格", "参数"],
    "service": ["服务", "售后", "客服", "支持", "维护"],
    "cooperation": ["合作", "加盟", "代理", "分销", "渠道"],
    "complaint": ["投诉", "退款", "问题", "故障", "不满"],
    "comparison": ["对比", "区别", "哪个好", "选择", "比较"],
    "guide": ["怎么", "如何", "步骤", "教程", "方法"],
}

AGENTIC_COMPLEXITY_INDICATORS = [
    "同时", "另外", "还有", "以及", "并且",
    "对比", "比较", "区别", "差异",
    "为什么", "原因", "怎么解决"
]

AGENTIC_STOPWORDS = {
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这"
}


class EnhancedReActAgent:
    """
    增强版ReAct Agent

    结合推理(Reasoning)和动作(Action)的Agent框架
    支持LLM增强推理能力、多步推理、自我修正
    """

    MAX_STEPS = 10
    CONFIDENCE_THRESHOLD = 0.7
    MIN_CONFIDENCE_FOR_ANSWER = 0.5
    MAX_EXECUTION_TIME = 30

    def __init__(self, tools: List[Tool], llm_provider=None):
        self.tools = {tool.name: tool for tool in tools}
        self.llm_provider = llm_provider
        self.thought_history: List[AgentThought] = []
        self.answer_candidates: List[AnswerCandidate] = []
        self.retrieval_attempts = 0
        self.max_retrieval_attempts = 3
        self._skip_kg_tool = False
        self._run_lock = threading.Lock()
        from src.common.utils import get_thread_pool_manager
        self._tool_executor = get_thread_pool_manager().get_or_create("agent_tool", max_workers=2)

    def _execute_step_with_timeout(
        self,
        step_func: Callable,
        *args,
        timeout: float = 5.0,
        **kwargs,
    ) -> Any:
        """带超时的单步执行保护。

        在独立线程中运行单步逻辑（如 _think / LLM 推理），防止某一步耗时异常
        导致主循环长时间阻塞。每个步骤独立计时，超时后立即返回 None，
        主循环依据 None 决定是否终止推理。

        注意：传入的 step_func 应避免在执行期间持有外部锁或修改共享状态而未做线程隔离。
        """
        started_at = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(step_func, *args, **kwargs)
            try:
                result = future.result(timeout=timeout)
                return result
            except concurrent.futures.TimeoutError:
                logger.warning(
                    f"[Agentic RAG] 单步执行超时 ({timeout}s)，强制中断本步"
                )
                append_trace_span(
                    getattr(self, "_trace_payload", {}),
                    name="agentic_step_timeout",
                    stage="agentic_tool",
                    status="timeout",
                    duration_ms=(time.perf_counter() - started_at) * 1000,
                    metadata={"timeout_seconds": timeout},
                )
                return None
            except Exception as e:
                logger.warning(f"[Agentic RAG] 单步执行异常: {e}")
                return None

    def run(self, query: str, conversation_history: List[Dict] = None) -> AgentResult:
        """
        运行Agent（线程安全，带总体超时保护）

        Args:
            query: 用户查询
            conversation_history: 对话历史

        Returns:
            AgentResult: Agent执行结果
        """
        if not self._run_lock.acquire(timeout=5):
            logger.warning("Agent.run: 另一个查询正在执行中，跳过")
            return AgentResult(
                answer="系统繁忙，请稍后再试",
                confidence=0.0,
                reasoning_chain=[],
                sources=[],
                metadata={"skipped": True, "reason": "Agent并发限制"}
            )
        try:
            return self._run_internal(query, conversation_history)
        finally:
            self._run_lock.release()

    def _run_internal(self, query: str, conversation_history: List[Dict] = None) -> AgentResult:
        """Agent内部执行逻辑"""
        start_time = time.time()
        self.thought_history = []
        self.answer_candidates = []
        self.retrieval_attempts = 0
        self._trace_payload: Dict[str, Any] = {}
        
        query_analysis = self._analyze_query(query)
        logger.info(f"[查询分析] 意图: {query_analysis.intent} | 复杂度: {query_analysis.complexity:.2f} | 工具: {query_analysis.suggested_tools}")
        
        context = {
            "query": query,
            "query_analysis": query_analysis,
            "retrieved_info": [],
            "kg_info": None,
            "conversation_history": conversation_history or [],
            "failed_tools": set(),
            "retry_count": 0
        }
        tools_used = set()
        consecutive_refine_count = 0

        step = 0
        while step < self.MAX_STEPS:
            step += 1

            # 总体执行时间硬上限检查：超过 MAX_EXECUTION_TIME 秒强制终止
            elapsed = time.time() - start_time
            if elapsed > self.MAX_EXECUTION_TIME:
                logger.warning(
                    f"[Agentic RAG] 达到总执行时间上限 {self.MAX_EXECUTION_TIME}s "
                    f"(已耗时 {elapsed:.1f}s)，强制结束推理"
                )
                break

            # 单步执行加超时保护：防止 _think 自身卡死
            thought = self._execute_step_with_timeout(
                self._think, query, context, step, timeout=5.0
            )
            if thought is None:
                logger.warning(
                    f"[Agentic RAG] 第{step}步执行超时或异常，跳出主循环"
                )
                break
            self.thought_history.append(thought)

            logger.debug(f"[步骤{step}] 思考: {thought.thought} | 动作: {thought.action.value}")

            if thought.action == AgentAction.ANSWER:
                thought.is_final = True
                break

            if thought.action == AgentAction.RETRIEVE:
                result = self._execute_tool("vector_retriever", query=query, top_k=5)
                if result.get("success") and result.get("results"):
                    context["retrieved_info"].extend(result.get("results", []))
                    thought.observation = f"检索到 {len(result.get('results', []))} 条结果"
                    self.retrieval_attempts += 1
                    consecutive_refine_count = 0
                else:
                    thought.observation = "检索无结果"
                    context["failed_tools"].add("vector_retriever")
                tools_used.add("vector_retriever")

            elif thought.action == AgentAction.KG_QUERY:
                if getattr(self, '_skip_kg_tool', False) or "kg_query" not in self.tools:
                    thought.observation = "知识图谱工具不可用，跳过"
                    thought.action = AgentAction.RETRIEVE
                    result = self._execute_tool("vector_retriever", query=query, top_k=5)
                    if result.get("success") and result.get("results"):
                        context["retrieved_info"].extend(result.get("results", []))
                        thought.observation = f"改用向量检索，获取到 {len(result.get('results', []))} 条结果"
                    else:
                        thought.observation = "向量检索也无结果"
                else:
                    result = self._execute_tool("kg_query", query=query)
                    if result.get("success"):
                        context["kg_info"] = result
                        thought.observation = f"图谱查询成功，找到 {len(result.get('entities', []))} 个实体"
                    else:
                        thought.observation = "图谱查询失败"
                        context["failed_tools"].add("kg_query")
                    tools_used.add("kg_query")

            elif thought.action == AgentAction.REFINE:
                consecutive_refine_count += 1
                if consecutive_refine_count >= 2:
                    thought.action = AgentAction.GENERATE
                    thought.observation = "连续REFINE次数过多，强制进入GENERATE"
                    candidate = self._generate_candidate(query, context)
                    if candidate:
                        self.answer_candidates.append(candidate)
                        thought.observation = f"强制生成候选答案，置信度: {candidate.confidence:.2f}"
                    else:
                        thought.observation = "答案生成失败"
                else:
                    thought.observation = self._refine_information(query, context)

            elif thought.action == AgentAction.VERIFY:
                consecutive_refine_count += 1
                if consecutive_refine_count >= 2:
                    thought.action = AgentAction.GENERATE
                    thought.observation = "连续VERIFY次数过多，强制进入GENERATE"
                    candidate = self._generate_candidate(query, context)
                    if candidate:
                        self.answer_candidates.append(candidate)
                        thought.observation = f"强制生成候选答案，置信度: {candidate.confidence:.2f}"
                    else:
                        thought.observation = "答案生成失败"
                else:
                    thought.observation = self._verify_answer(query, context)

            elif thought.action == AgentAction.GENERATE:
                consecutive_refine_count = 0
                candidate = self._generate_candidate(query, context)
                if candidate:
                    self.answer_candidates.append(candidate)
                    thought.observation = f"生成候选答案，置信度: {candidate.confidence:.2f}"
                else:
                    thought.observation = "答案生成失败"

            elif thought.action == AgentAction.ASK_CLARIFY:
                thought.is_final = True
                break

            if thought.is_final:
                break
            
            if step >= 3 and not context["retrieved_info"]:
                thought.observation = "多次检索无结果，尝试生成通用回复"
                break

        final_answer, final_confidence = self._select_best_answer(query, context)
        
        needs_human = (
            final_confidence < self.CONFIDENCE_THRESHOLD or
            len(context["retrieved_info"]) == 0
        )

        self_correction_count = sum(
            1 for t in self.thought_history
            if "修正" in t.thought or "纠正" in t.thought or "重试" in t.thought
        )

        processing_time = time.time() - start_time
        
        return AgentResult(
            answer=final_answer,
            confidence=final_confidence,
            retrieved=bool(context["retrieved_info"]),
            sources=context["retrieved_info"][:5],
            reasoning_chain=self.thought_history,
            tools_used=list(tools_used),
            needs_human=needs_human,
            self_correction_count=self_correction_count,
            metadata={
                "query_analysis": {
                    "intent": query_analysis.intent,
                    "complexity": query_analysis.complexity,
                    "keywords": query_analysis.keywords,
                    "suggested_tools": query_analysis.suggested_tools,
                },
                    "trace": self._trace_payload.get("trace", {}),
                "processing_time": processing_time,
                "retrieval_attempts": self.retrieval_attempts,
                "candidate_count": len(self.answer_candidates)
            }
        )

    def _analyze_query(self, query: str) -> QueryAnalysis:
        """轻量查询分析，仅服务 Agentic RAG 内部决策。"""
        analysis = QueryAnalysis(original_query=query)
        analysis.intent = self._detect_intent(query)
        analysis.complexity = self._calculate_query_complexity(query)
        analysis.keywords = self._extract_query_keywords(query)
        analysis.entities = self._extract_query_entities(query)
        analysis.suggested_tools = self._suggest_tools(analysis)
        analysis.need_multi_step = (
            analysis.complexity > 0.6 or len(analysis.suggested_tools) > 1
        )
        return analysis

    def _detect_intent(self, query: str) -> str:
        """检测意图。"""
        query_lower = query.lower()
        for intent, patterns in AGENTIC_INTENT_PATTERNS.items():
            if any(pattern in query_lower for pattern in patterns):
                return intent
        return "general"

    def _calculate_query_complexity(self, query: str) -> float:
        """计算复杂度。"""
        complexity = 0.3
        if len(query) > 50:
            complexity += 0.1
        if len(query) > 100:
            complexity += 0.1

        indicator_count = sum(
            1 for indicator in AGENTIC_COMPLEXITY_INDICATORS if indicator in query
        )
        complexity += min(indicator_count * 0.15, 0.3)

        question_marks = query.count("？") + query.count("?")
        if question_marks > 1:
            complexity += 0.1

        return min(complexity, 1.0)

    def _extract_query_keywords(self, query: str) -> List[str]:
        """提取关键词。"""
        words = re.findall(r'[\u4e00-\u9fa5]+', query)
        keywords = [word for word in words if word not in AGENTIC_STOPWORDS and len(word) >= 2]
        return list(set(keywords))[:10]

    def _extract_query_entities(self, query: str) -> List[str]:
        """提取实体（简化版）。"""
        number_pattern = r'\d+(?:\.\d+)?(?:万|千|百|十|元|块|件|个|台|套)?'
        return re.findall(number_pattern, query)[:5]

    def _think(self, query: str, context: Dict, step: int) -> AgentThought:
        """思考下一步动作"""
        query_analysis = context.get("query_analysis")
        has_retrieved = bool(context["retrieved_info"])
        has_kg = context.get("kg_info") is not None
        has_candidates = len(self.answer_candidates) > 0

        if self.llm_provider and step == 1 and query_analysis and query_analysis.complexity > 0.5:
            return self._llm_think(query, context, step)

        if step == 1:
            suggested_tools = query_analysis.suggested_tools if query_analysis else ["vector_retriever"]
            
            if "kg_query" in suggested_tools and "kg_query" in self.tools:
                return AgentThought(
                    step=step,
                    thought="根据查询分析，需要同时检索知识库和知识图谱",
                    action=AgentAction.RETRIEVE,
                    action_input={"query": query, "tools": suggested_tools}
                )
            
            return AgentThought(
                step=step,
                thought="首先从知识库检索相关信息",
                action=AgentAction.RETRIEVE,
                action_input={"query": query}
            )

        if not has_retrieved and self.retrieval_attempts < self.max_retrieval_attempts:
            return AgentThought(
                step=step,
                thought=f"检索未成功(尝试{self.retrieval_attempts}次)，尝试优化查询重试",
                action=AgentAction.RETRIEVE,
                action_input={"query": self._optimize_query(query, context)}
            )

        if has_retrieved and not has_kg and "kg_query" in self.tools and "kg_query" not in context.get("failed_tools", set()):
            return AgentThought(
                step=step,
                thought="已有检索结果，尝试查询知识图谱获取更多关系信息",
                action=AgentAction.KG_QUERY,
                action_input={"query": query}
            )

        if has_retrieved:
            confidence = self._calculate_confidence(context)
            
            if confidence < self.MIN_CONFIDENCE_FOR_ANSWER and self.retrieval_attempts < self.max_retrieval_attempts:
                return AgentThought(
                    step=step,
                    thought=f"置信度较低({confidence:.2f})，尝试整合和精炼信息",
                    action=AgentAction.REFINE,
                    action_input={}
                )
            
            if has_candidates:
                best_confidence = max(c.confidence for c in self.answer_candidates)
                if best_confidence < self.CONFIDENCE_THRESHOLD:
                    return AgentThought(
                        step=step,
                        thought="验证已生成的答案质量",
                        action=AgentAction.VERIFY,
                        action_input={}
                    )

            return AgentThought(
                step=step,
                thought=f"信息充足(置信度:{confidence:.2f})，准备生成最终答案",
                action=AgentAction.ANSWER,
                action_input={},
                is_final=True
            )

        return AgentThought(
            step=step,
            thought="无法获取足够信息，准备输出默认回复",
            action=AgentAction.ANSWER,
            action_input={},
            is_final=True
        )

    def _optimize_query(self, query: str, context: Dict) -> str:
        """优化查询"""
        query_analysis = context.get("query_analysis")
        if query_analysis and query_analysis.keywords:
            optimized = " ".join(query_analysis.keywords[:5])
            logger.info(f"[查询优化] 原始: {query} -> 优化: {optimized}")
            return optimized
        return query

    def _suggest_tools(self, analysis: QueryAnalysis) -> List[str]:
        """建议工具。"""
        tools = ["vector_retriever"]
        if analysis.intent in ["comparison", "guide"]:
            tools.append("kg_query")
        if any(entity in analysis.entities for entity in ["元", "块", "万", "千"]):
            tools.append("calculator")
        return tools

    def _llm_think(self, query: str, context: Dict, step: int) -> AgentThought:
        """使用LLM进行智能思考"""
        try:
            query_analysis = context.get("query_analysis")
            complexity = query_analysis.complexity if query_analysis else 0.5
            
            prompt = f"""分析用户问题，决定最佳处理策略。

用户问题: {query}
问题复杂度: {complexity:.2f}
关键词: {', '.join(query_analysis.keywords) if query_analysis else '无'}

可用操作:
- RETRIEVE: 从知识库检索信息（适合需要查找具体信息的问题）
- KG_QUERY: 查询知识图谱获取实体关系（适合需要理解关系的问题）
- ANSWER: 直接回答（当问题非常简单，无需检索时）

请分析问题，选择最合适的下一步操作。
只回答操作名称（RETRIEVE/KG_QUERY/ANSWER）。"""

            raw_response = call_llm_safe(self.llm_provider, prompt, timeout=15.0)
            response = (raw_response or "").strip().upper()

            if "ANSWER" in response:
                return AgentThought(
                    step=step,
                    thought="LLM判断问题简单，可直接回答",
                    action=AgentAction.ANSWER,
                    action_input={},
                    is_final=True
                )
            elif "KG_QUERY" in response:
                return AgentThought(
                    step=step,
                    thought="LLM建议先查询知识图谱",
                    action=AgentAction.KG_QUERY,
                    action_input={"query": query}
                )
            else:
                return AgentThought(
                    step=step,
                    thought="LLM建议从知识库检索",
                    action=AgentAction.RETRIEVE,
                    action_input={"query": query}
                )
        except Exception as e:
            logger.warning(f"LLM思考失败: {e}")
            return AgentThought(
                step=step,
                thought="默认从知识库检索",
                action=AgentAction.RETRIEVE,
                action_input={"query": query}
            )

    def _refine_information(self, query: str, context: Dict) -> str:
        """精炼信息"""
        retrieved = context.get("retrieved_info", [])
        
        if not retrieved:
            return "无信息可精炼"
        
        seen_questions = set()
        unique_results = []
        for r in retrieved:
            q = r.get("question", "")[:50]
            if q not in seen_questions:
                seen_questions.add(q)
                unique_results.append(r)
        
        context["retrieved_info"] = unique_results
        
        if len(unique_results) < len(retrieved):
            return f"去重后保留 {len(unique_results)} 条结果"
        
        return "信息已整理"

    def _verify_answer(self, query: str, context: Dict) -> str:
        """验证答案"""
        retrieved = context.get("retrieved_info", [])
        
        if not retrieved:
            return "验证失败：无检索结果"
        
        if len(retrieved) >= 3:
            return "验证通过：检索到足够的相关信息"
        
        if len(retrieved) >= 1:
            best_score = retrieved[0].get("score", 0)
            if best_score > 50:
                return "验证通过：最佳结果匹配度高"
        
        return "验证通过：检索到基本信息"

    def _generate_candidate(self, query: str, context: Dict) -> Optional[AnswerCandidate]:
        """生成答案候选"""
        retrieved = context.get("retrieved_info", [])
        kg_info = context.get("kg_info")
        
        if not retrieved and not kg_info:
            return None
        
        if self.llm_provider and retrieved:
            return self._llm_generate_candidate(query, retrieved, kg_info)
        
        if retrieved:
            best_result = retrieved[0]
            answer = best_result.get("answer", "")
            raw_score = best_result.get("score", 0)
            confidence = min(raw_score / 100, 1.0) if raw_score > 1 else min(raw_score, 1.0)
            if not answer or not answer.strip():
                confidence = min(confidence, 0.2)
            
            return AnswerCandidate(
                answer=answer,
                source="retrieval",
                confidence=confidence,
                retrieved_info=retrieved[:3]
            )
        
        return None

    def _llm_generate_candidate(self, query: str, retrieved: List[Dict], kg_info: Dict) -> Optional[AnswerCandidate]:
        """使用LLM生成增强答案候选"""
        try:
            context_parts = []
            for i, r in enumerate(retrieved[:5]):
                context_parts.append(f"[参考{i+1}] 问题: {r.get('question', '')}\n答案: {r.get('answer', '')[:200]}")

            context_text = "\n\n".join(context_parts)

            if kg_info and kg_info.get("entities"):
                entities = ", ".join(kg_info["entities"][:5])
                context_text += f"\n\n相关实体: {entities}"

            prompt = f"""基于以下参考信息回答用户问题。

用户问题: {query}

参考信息:
{context_text}

要求:
1. 综合参考信息给出准确、专业的回答
2. 如果参考信息不足，诚实说明并提供相关建议
3. 回答要简洁明了，不超过300字
4. 如果涉及价格，请明确说明
5. 如果是产品功能问题，请列出主要功能点

请直接回答:"""

            answer = call_llm_safe(self.llm_provider, prompt, timeout=REPLY_LLM_TIMEOUT_SECONDS)
            
            confidence = self._calculate_llm_answer_confidence(answer, retrieved)
            
            return AnswerCandidate(
                answer=answer.strip(),
                source="llm_enhanced",
                confidence=confidence,
                retrieved_info=retrieved[:3],
                metadata={"has_kg": kg_info is not None}
            )
        except Exception as e:
            logger.warning(f"LLM生成答案失败: {e}")
            if retrieved:
                return AnswerCandidate(
                    answer=retrieved[0].get("answer", "抱歉，无法生成回答。"),
                    source="fallback",
                    confidence=0.3,
                    retrieved_info=retrieved[:1]
                )
            return None

    def _calculate_llm_answer_confidence(self, answer: str, retrieved: List[Dict]) -> float:
        """计算LLM答案的置信度"""
        confidence = 0.5
        
        if len(answer) > 50:
            confidence += 0.1
        if len(answer) > 100:
            confidence += 0.05
        
        if any(kw in answer for kw in ["建议", "可以", "提供", "支持"]):
            confidence += 0.1
        
        if any(kw in answer for kw in ["不确定", "可能", "也许", "大概"]):
            confidence -= 0.1
        
        if retrieved:
            best_score = retrieved[0].get("score", 0)
            confidence += min(best_score / 200, 0.2)
        
        return min(max(confidence, 0.1), 1.0)

    def _select_best_answer(self, query: str, context: Dict) -> Tuple[str, float]:
        """选择最佳答案"""
        if self.answer_candidates:
            best_candidate = max(self.answer_candidates, key=lambda c: c.confidence)
            return best_candidate.answer, best_candidate.confidence
        
        retrieved = context.get("retrieved_info", [])
        if retrieved:
            best_result = retrieved[0]
            answer = best_result.get("answer", "")
            raw_score = best_result.get("score", 0)
            confidence = min(raw_score / 100, 1.0) if raw_score > 1 else min(raw_score, 1.0)
            if not answer or not answer.strip():
                confidence = min(confidence, 0.2)
            return answer, confidence
        
        return "抱歉，我目前无法回答您的问题，请尝试换一种方式描述或联系人工客服。", 0.1

    TOOL_TIMEOUT = 10

    def _execute_tool(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        """执行工具（复用线程池，带独立超时保护）"""
        tool = self.tools.get(tool_name)
        if not tool:
            return {"success": False, "error": f"工具 {tool_name} 不存在"}

        started_at = time.perf_counter()
        try:
            future = self._tool_executor.submit(tool.func, **kwargs)
            result = future.result(timeout=self.TOOL_TIMEOUT)
            append_trace_span(
                self._trace_payload,
                name=f"tool_{tool_name}",
                stage="agentic_tool",
                status="ok" if result.get("success") else "error",
                error=result.get("error", "") if not result.get("success") else "",
                duration_ms=(time.perf_counter() - started_at) * 1000,
                metadata={
                    "tool_name": tool_name,
                    "success": bool(result.get("success")),
                    "result_size": len(result.get("results", [])) if isinstance(result.get("results"), list) else 0,
                },
            )
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"工具执行超时({self.TOOL_TIMEOUT}s): {tool_name}")
            error_message = f"工具 {tool_name} 执行超时"
            append_trace_span(
                self._trace_payload,
                name=f"tool_{tool_name}",
                stage="agentic_tool",
                status="timeout",
                error=error_message,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                metadata={"tool_name": tool_name, "success": False},
            )
            return {"success": False, "error": error_message}
        except Exception as e:
            logger.error(f"工具执行失败: {e}")
            error_message = str(e)
            append_trace_span(
                self._trace_payload,
                name=f"tool_{tool_name}",
                stage="agentic_tool",
                status="error",
                error=error_message,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                metadata={"tool_name": tool_name, "success": False},
            )
            return {"success": False, "error": error_message}

    def _calculate_confidence(self, context: Dict) -> float:
        """计算置信度"""
        retrieved = context.get("retrieved_info", [])
        kg_info = context.get("kg_info")
        
        reasoning_score = 0.0
        if kg_info and kg_info.get("reasoning_result"):
            reasoning_score = kg_info["reasoning_result"].get("best_path", 0)
        
        return ConfidenceCalculator.calculate_agentic_confidence(
            retrieved_results=retrieved,
            kg_info=kg_info,
            has_llm=self.llm_provider is not None,
            reasoning_score=reasoning_score
        )


class AgenticRAG:
    """
    Agentic RAG 系统

    功能：
    1. ReAct/ReAct+Agent框架
    2. 工具注册与调用
    3. 多步推理链
    4. 自我评估与修正
    5. LLM增强推理能力
    6. 查询分析与优化
    """

    def __init__(
        self,
        knowledge_base,
        knowledge_graph_service=None,
        embedding_service=None,
        llm_provider=None
    ):
        self.knowledge_base = knowledge_base
        self.knowledge_graph_service = knowledge_graph_service
        self.embedding_service = embedding_service
        self.llm_provider = llm_provider

        tools = [
            Tool(
                name="vector_retriever",
                description="从知识库检索相关内容",
                func=self._create_retriever_func(),
                priority=1
            )
        ]

        if knowledge_graph_service:
            tools.append(Tool(
                name="kg_query",
                description="查询知识图谱",
                func=self._create_kg_query_func(),
                priority=2
            ))

        tools.extend([
            Tool(
                name="calculator",
                description="执行计算",
                func=self._create_calculator_func(),
                priority=3
            )
        ])

        self.agent = EnhancedReActAgent(tools, llm_provider=llm_provider)

    def _create_retriever_func(self):
        """创建检索函数"""
        kb = self.knowledge_base
        def retrieve(query: str, top_k: int = 5, category: str = None) -> Dict[str, Any]:
            try:
                results = kb.search(query, top_k=top_k, category=category)
                return {
                    "success": True,
                    "results": [
                        {
                            "question": item.question,
                            "answer": item.answer,
                            "score": score,
                            "category": item.category,
                            "keywords": item.keywords if hasattr(item, 'keywords') else []
                        }
                        for item, score in results
                    ]
                }
            except Exception as e:
                logger.error(f"检索失败: {e}")
                return {"success": False, "error": str(e), "results": []}
        return retrieve

    def _create_kg_query_func(self):
        """创建知识图谱查询函数"""
        kg = self.knowledge_graph_service
        def kg_query(query: str, max_paths: int = 5) -> Dict[str, Any]:
            if kg is None:
                return {"success": False, "error": "知识图谱服务未初始化"}
            return kg.query_with_reasoning(query, max_paths)
        return kg_query

    def _create_calculator_func(self):
        """创建计算函数（使用安全AST解析替代eval）"""
        def calculate(expression: str) -> Dict[str, Any]:
            try:
                allowed_chars = set("0123456789.+-*/() ")
                if not all(c in allowed_chars for c in expression):
                    return {"success": False, "error": "非法表达式"}
                import ast
                import operator
                allowed_ops = {
                    ast.Add: operator.add, ast.Sub: operator.sub,
                    ast.Mult: operator.mul, ast.Div: operator.truediv,
                    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
                    ast.Pow: operator.pow, ast.USub: operator.neg,
                }
                tree = ast.parse(expression, mode='eval')
                def _eval(node):
                    if isinstance(node, ast.Constant): return node.value
                    elif isinstance(node, ast.Num): return node.n
                    elif isinstance(node, ast.BinOp):
                        left, right = _eval(node.left), _eval(node.right)
                        op_type = type(node.op)
                        if op_type == ast.Pow and abs(right) > 100:
                            raise ValueError("指数绝对值不能超过100")
                        if op_type in allowed_ops: return allowed_ops[op_type](left, right)
                        raise ValueError(f"不支持的操作符: {op_type}")
                    elif isinstance(node, ast.UnaryOp):
                        operand = _eval(node.operand)
                        op_type = type(node.op)
                        if op_type in allowed_ops: return allowed_ops[op_type](operand)
                        raise ValueError(f"不支持的一元操作符: {op_type}")
                    else:
                        raise ValueError(f"不支持的节点类型: {type(node)}")
                result = _eval(tree.body)
                return {"success": True, "result": result}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return calculate

    def query(
        self,
        user_query: str,
        conversation_history: List[Dict] = None,
        use_kg: bool = True
    ) -> AgentResult:
        """
        Agentic RAG 查询

        Args:
            user_query: 用户查询
            conversation_history: 对话历史
            use_kg: 是否使用知识图谱

        Returns:
            AgentResult: Agent执行结果
        """
        self._skip_kg_tool = not use_kg or self.knowledge_graph_service is None
        self.agent._skip_kg_tool = self._skip_kg_tool

        result = self.agent.run(user_query, conversation_history)

        return result

    def query_with_reasoning(
        self,
        user_query: str,
        conversation_history: List[Dict] = None,
        use_kg: bool = True
    ) -> Dict[str, Any]:
        """
        带推理过程的查询

        Args:
            user_query: 用户查询
            conversation_history: 对话历史
            use_kg: 是否使用知识图谱

        Returns:
            Dict: 包含答案、推理链和置信度
        """
        result = self.query(
            user_query=user_query,
            conversation_history=conversation_history,
            use_kg=use_kg
        )

        return {
            "answer": result.answer,
            "confidence": result.confidence,
            "knowledge_sources": result.sources,
            "reasoning_chain": [
                {
                    "step": t.step,
                    "thought": t.thought,
                    "action": t.action.value,
                    "observation": t.observation
                }
                for t in result.reasoning_chain
            ],
            "tools_used": result.tools_used,
            "needs_human": result.needs_human,
            "self_correction_count": result.self_correction_count,
            "metadata": result.metadata
        }


from functools import lru_cache

@lru_cache(maxsize=1)
def get_agentic_rag_system(
    knowledge_base=None,
    knowledge_graph_service=None,
    embedding_service=None,
    llm_provider=None
) -> AgenticRAG:
    if knowledge_base is None:
        from .unified_knowledge_service import get_unified_knowledge_service
        knowledge_base = get_unified_knowledge_service()
    if llm_provider is None:
        try:
            from .llm_service import get_default_llm_provider
            llm_provider = get_default_llm_provider()
        except Exception as e:
            logger.warning(f"获取LLM提供者失败: {e}")
    return AgenticRAG(
        knowledge_base,
        knowledge_graph_service,
        embedding_service,
        llm_provider
    )
