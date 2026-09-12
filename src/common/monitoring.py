"""
生产级监控模块

提供RAG系统和业务指标的完整监控能力：
- Prometheus指标收集
- 业务指标埋点
- RAG性能追踪
- 告警规则配置

基于Prometheus + Grafana最佳实践
"""

import time
import threading
from loguru import logger
from typing import Optional, Dict, Any, List, Callable
from functools import wraps
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict


class MetricType(Enum):
    """指标类型"""
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"


@dataclass
class MetricConfig:
    """指标配置"""
    name: str
    description: str
    metric_type: MetricType
    labels: List[str] = field(default_factory=list)
    buckets: Optional[List[float]] = None


class SimpleMetrics:
    """
    简化指标收集器

    在没有prometheus_client库时提供基础指标收集
    当prometheus可用时自动升级到完整实现
    """

    def __init__(self):
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._last_update: Dict[str, float] = {}
        self._metrics_lock = threading.Lock()

        self._enabled = True
        self._prometheus_available = self._check_prometheus()

        if self._prometheus_available:
            self._init_prometheus()
            logger.info("Prometheus指标收集已启用")
        else:
            logger.warning("Prometheus未安装，使用简化指标收集")

        self._metric_attr_map = {
            'messages_total': 'messages_total',
            'messages_processed': 'messages_processed',
            'messages_failed': 'messages_failed',
            'rag_retrieval_latency': 'retrieval_latency',
            'retrieval_count': 'retrieval_count',
            'rag_answer_confidence': 'answer_confidence',
            'rag_knowledge_hit_total': 'knowledge_hit',
            'rag_knowledge_miss_total': 'knowledge_miss',
            'llm_generation_latency': 'llm_latency',
            'llm_tokens_generated': 'llm_tokens',
            'llm_errors_total': 'llm_errors',
            'llm_inflight_requests': 'llm_inflight',
            'llm_executor_queue_size': 'llm_queue_size',
            'llm_executor_capacity': 'llm_capacity',
            'circuit_breaker_state': 'circuit_breaker_state',
            'circuit_breaker_failure_count': 'circuit_breaker_failure_count',
            'circuit_breaker_success_count': 'circuit_breaker_success_count',
            'human_escalation_total': 'human_escalation',
            'message_response_time': 'response_time',
            'intent_distribution': 'intent_distribution',
            'strategy_distribution': 'strategy_distribution',
            'cache_hit_total': 'cache_hit',
            'cache_miss_total': 'cache_miss',
            'workflow_runs_total': 'workflow_runs',
            'workflow_recovery_total': 'workflow_recovery',
            'outbox_retries_total': 'outbox_retries',
            'outbound_idempotency_hits_total': 'outbound_idempotency_hits',
            'outbound_send_attempts_total': 'outbound_send_attempts',
            'outbound_send_latency_seconds': 'outbound_send_latency',
            'outbound_recent_reply_store_entries': 'outbound_recent_reply_store_entries',
            'outbound_recent_reply_store_conversations': 'outbound_recent_reply_store_conversations',
            'outbound_dispatch_queue_size': 'outbound_dispatch_queue_size',
            'outbound_dispatch_active_conversations': 'outbound_dispatch_active_conversations',
            'outbound_dispatch_wait_latency_seconds': 'outbound_dispatch_wait_latency',
        }

    def _check_prometheus(self) -> bool:
        """检查prometheus_client是否可用"""
        try:
            from prometheus_client import Counter, Gauge, Histogram
            return True
        except ImportError:
            return False

    def _init_prometheus(self):
        """初始化Prometheus指标"""
        from prometheus_client import Counter, Gauge, Histogram, CollectorRegistry

        self.registry = CollectorRegistry()

        # 业务指标
        self.messages_total = Counter(
            'messages_total',
            'Total messages received',
            ['message_type'],
            registry=self.registry
        )

        self.messages_processed = Counter(
            'messages_processed_total',
            'Total messages processed successfully',
            registry=self.registry
        )

        self.messages_failed = Counter(
            'messages_failed_total',
            'Total messages processing failed',
            ['error_type'],
            registry=self.registry
        )

        self.response_time = Histogram(
            'message_response_time_seconds',
            'Message response time in seconds',
            buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
            registry=self.registry
        )

        self.human_escalation = Counter(
            'human_escalation_total',
            'Total conversations escalated to human',
            registry=self.registry
        )

        # RAG指标
        self.retrieval_latency = Histogram(
            'rag_retrieval_latency_seconds',
            'RAG retrieval latency in seconds',
            ['retrieval_type'],
            buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
            registry=self.registry
        )

        self.retrieval_count = Counter(
            'rag_retrieval_total',
            'Total retrieval operations',
            ['status'],
            registry=self.registry
        )

        self.answer_confidence = Histogram(
            'rag_answer_confidence',
            'RAG answer confidence score',
            buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
            registry=self.registry
        )

        self.knowledge_hit = Counter(
            'rag_knowledge_hit_total',
            'Total knowledge base hits',
            registry=self.registry
        )

        self.knowledge_miss = Counter(
            'rag_knowledge_miss_total',
            'Total knowledge base misses',
            registry=self.registry
        )

        # 系统指标
        self.cpu_usage = Gauge(
            'system_cpu_usage_percent',
            'CPU usage percentage',
            registry=self.registry
        )

        self.memory_usage = Gauge(
            'system_memory_usage_percent',
            'Memory usage percentage',
            registry=self.registry
        )

        self.vector_count = Gauge(
            'rag_vector_count',
            'Number of vectors in knowledge base',
            registry=self.registry
        )

        self.llm_model_loaded = Gauge(
            'llm_model_loaded',
            'Whether LLM model is loaded (1=yes, 0=no)',
            registry=self.registry
        )

        # LLM指标
        self.llm_latency = Histogram(
            'llm_generation_latency_seconds',
            'LLM generation latency in seconds',
            buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
            registry=self.registry
        )

        self.llm_tokens = Histogram(
            'llm_tokens_generated',
            'Number of tokens generated per request',
            buckets=[10, 50, 100, 200, 500, 1000],
            registry=self.registry
        )

        self.llm_errors = Counter(
            'llm_errors_total',
            'Total LLM errors',
            ['error_type'],
            registry=self.registry
        )

        self.llm_inflight = Gauge(
            'llm_inflight_requests',
            'Current in-flight LLM requests',
            registry=self.registry
        )

        self.llm_queue_size = Gauge(
            'llm_executor_queue_size',
            'Current queued LLM tasks waiting in executor',
            registry=self.registry
        )

        self.llm_capacity = Gauge(
            'llm_executor_capacity',
            'Configured LLM executor capacity',
            registry=self.registry
        )

        self.circuit_breaker_state = Gauge(
            'circuit_breaker_state',
            'Circuit breaker state value (closed=0, half_open=1, open=2)',
            registry=self.registry
        )

        self.circuit_breaker_failure_count = Gauge(
            'circuit_breaker_failure_count',
            'Current circuit breaker failure count',
            registry=self.registry
        )

        self.circuit_breaker_success_count = Gauge(
            'circuit_breaker_success_count',
            'Current circuit breaker success count',
            registry=self.registry
        )

        # Durable Workflow 指标
        self.workflow_runs = Counter(
            'workflow_runs_total',
            'Total durable workflow runs',
            ['status'],  # success, failed, paused
            registry=self.registry
        )

        self.workflow_recovery = Counter(
            'workflow_recovery_total',
            'Total workflow recovery attempts',
            ['result'],  # success, failed
            registry=self.registry
        )

        self.outbox_retries = Counter(
            'outbox_retries_total',
            'Total outbox message retries',
            ['result'],  # sent, failed, waiting
            registry=self.registry
        )

        self.outbound_idempotency_hits = Counter(
            'outbound_idempotency_hits_total',
            'Outbound idempotency and duplicate guard hits',
            ['match_type'],
            registry=self.registry
        )

        self.outbound_send_attempts = Counter(
            'outbound_send_attempts_total',
            'Outbound send attempts by channel and result',
            ['channel', 'result'],
            registry=self.registry
        )

        self.outbound_send_latency = Histogram(
            'outbound_send_latency_seconds',
            'Outbound send latency in seconds',
            ['channel', 'stage'],
            buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
            registry=self.registry
        )

        self.outbound_recent_reply_store_entries = Gauge(
            'outbound_recent_reply_store_entries',
            'Current entries stored in recent reply store',
            registry=self.registry
        )

        self.outbound_recent_reply_store_conversations = Gauge(
            'outbound_recent_reply_store_conversations',
            'Current conversations tracked in recent reply store',
            registry=self.registry
        )

        self.outbound_dispatch_queue_size = Gauge(
            'outbound_dispatch_queue_size',
            'Current queued outbound dispatch tasks',
            registry=self.registry
        )

        self.outbound_dispatch_active_conversations = Gauge(
            'outbound_dispatch_active_conversations',
            'Current conversations with queued outbound dispatch tasks',
            registry=self.registry
        )

        self.outbound_dispatch_wait_latency = Histogram(
            'outbound_dispatch_wait_latency_seconds',
            'Queue wait latency before outbound dispatch starts',
            buckets=[0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
            registry=self.registry
        )

    def inc_counter(self, name: str, value: float = 1, labels: Optional[Dict] = None):
        """增加计数器"""
        if not self._enabled:
            return

        key = self._make_key(name, labels)

        if self._prometheus_available:
            try:
                attr_name = self._metric_attr_map.get(name, name)
                counter = getattr(self, attr_name, None)
                if counter and labels:
                    counter.labels(**labels).inc(value)
                elif counter:
                    counter.inc(value)
            except Exception as e:
                logger.warning(f"Failed to increment counter {name}: {e}")
        else:
            with self._metrics_lock:
                self._counters[key] += value

    def set_gauge(self, name: str, value: float, labels: Optional[Dict] = None):
        """设置仪表值"""
        if not self._enabled:
            return

        key = self._make_key(name, labels)

        if self._prometheus_available:
            try:
                attr_name = self._metric_attr_map.get(name, name)
                gauge = getattr(self, attr_name, None)
                if gauge and labels:
                    gauge.labels(**labels).set(value)
                elif gauge:
                    gauge.set(value)
            except Exception as e:
                logger.warning(f"Failed to set gauge {name}: {e}")
        else:
            with self._metrics_lock:
                self._gauges[key] = value
                self._last_update[key] = time.time()

    def observe_histogram(self, name: str, value: float, labels: Optional[Dict] = None):
        """记录直方图值"""
        if not self._enabled or value < 0:
            return

        key = self._make_key(name, labels)

        if self._prometheus_available:
            try:
                attr_name = self._metric_attr_map.get(name, name)
                histogram = getattr(self, attr_name, None)
                if histogram and labels:
                    histogram.labels(**labels).observe(value)
                elif histogram:
                    histogram.observe(value)
            except Exception as e:
                logger.warning(f"Failed to observe histogram {name}: {e}")
        else:
            with self._metrics_lock:
                self._histograms[key].append(value)
                if len(self._histograms[key]) > 1000:
                    self._histograms[key] = self._histograms[key][-1000:]

    def _make_key(self, name: str, labels: Optional[Dict] = None) -> str:
        """生成指标键名"""
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def track_latency(self, metric_name: str, labels: Optional[Dict] = None):
        """延迟追踪装饰器（支持同步和异步函数）"""
        import asyncio
        def decorator(func: Callable):
            if asyncio.iscoroutinefunction(func):
                @wraps(func)
                async def async_wrapper(*args, **kwargs):
                    start_time = time.time()
                    try:
                        return await func(*args, **kwargs)
                    finally:
                        duration = time.time() - start_time
                        self.observe_histogram(metric_name, duration, labels)
                return async_wrapper
            else:
                @wraps(func)
                def wrapper(*args, **kwargs):
                    start_time = time.time()
                    try:
                        return func(*args, **kwargs)
                    finally:
                        duration = time.time() - start_time
                        self.observe_histogram(metric_name, duration, labels)
                return wrapper
        return decorator

    def get_summary(self) -> Dict[str, Any]:
        """获取指标摘要"""
        if self._prometheus_available:
            return self._get_prometheus_summary()
        else:
            return self._get_simple_summary()

    def _get_prometheus_summary(self) -> Dict[str, Any]:
        """获取Prometheus指标摘要"""
        summary = {
            "enabled": True,
            "metrics": {
                "counters": [],
                "gauges": [],
                "histograms": []
            }
        }

        # 收集Counter指标
        for name in ['messages_total', 'messages_processed', 'messages_failed',
                     'retrieval_count', 'knowledge_hit', 'knowledge_miss', 'human_escalation']:
            counter = getattr(self, name, None)
            if counter:
                try:
                    # 获取当前值
                    summary["metrics"]["counters"].append({
                        "name": name,
                        "type": "counter"
                    })
                except Exception:
                    pass

        return summary

    def _get_simple_summary(self) -> Dict[str, Any]:
        """获取简化指标摘要"""
        return {
            "enabled": False,
            "mode": "simple",
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histogram_samples": {k: len(v) for k, v in self._histograms.items()}
        }

    def record_message(self, message_type: str, success: bool = True, error_type: str = None):
        """记录消息处理"""
        self.inc_counter('messages_total', labels={'message_type': message_type})

        if success:
            self.inc_counter('messages_processed')
        else:
            self.inc_counter('messages_failed', labels={'error_type': error_type or 'unknown'})

    def record_response_time(self, duration: float):
        """记录响应时间"""
        self.observe_histogram('message_response_time', duration)

    def record_retrieval(self, retrieval_type: str, latency: float, success: bool = True):
        """记录检索操作"""
        self.observe_histogram('rag_retrieval_latency', latency, labels={'retrieval_type': retrieval_type})
        self.inc_counter('retrieval_count', labels={'status': 'success' if success else 'error'})

    def record_knowledge_hit(self, hit: bool):
        """记录知识库命中"""
        if hit:
            self.inc_counter('rag_knowledge_hit_total')
        else:
            self.inc_counter('rag_knowledge_miss_total')

    def record_confidence(self, confidence: float):
        """记录答案置信度"""
        self.observe_histogram('rag_answer_confidence', confidence)

    def record_llm_latency(self, latency: float):
        """记录LLM延迟"""
        self.observe_histogram('llm_generation_latency', latency)

    def record_llm_error(self, error_type: str):
        """记录LLM错误类型"""
        self.inc_counter('llm_errors_total', labels={'error_type': error_type or 'unknown'})

    def update_llm_executor(self, *, inflight: int, queued: int, capacity: int):
        """更新LLM执行器运行指标"""
        self.set_gauge('llm_inflight_requests', max(float(inflight), 0.0))
        self.set_gauge('llm_executor_queue_size', max(float(queued), 0.0))
        self.set_gauge('llm_executor_capacity', max(float(capacity), 0.0))

    def update_circuit_breaker(self, *, state: str, failure_count: int = 0, success_count: int = 0):
        """更新熔断器当前状态指标"""
        state_map = {
            'closed': 0.0,
            'half_open': 1.0,
            'open': 2.0,
        }
        self.set_gauge('circuit_breaker_state', state_map.get(str(state or '').lower(), -1.0))
        self.set_gauge('circuit_breaker_failure_count', max(float(failure_count), 0.0))
        self.set_gauge('circuit_breaker_success_count', max(float(success_count), 0.0))

    def record_llm_tokens(self, tokens: int):
        """记录生成的Token数"""
        self.observe_histogram('llm_tokens_generated', tokens)

    def record_human_escalation(self):
        """记录转人工"""
        self.inc_counter('human_escalation_total')

    def record_intent_distribution(self, intent_type: str):
        """记录意图类型分布"""
        self.inc_counter('intent_distribution', labels={'intent_type': intent_type})

    def record_strategy_distribution(self, strategy: str):
        """记录回复策略分布"""
        self.inc_counter('strategy_distribution', labels={'strategy': strategy})

    def record_cache_hit(self, cache_type: str):
        """记录缓存命中"""
        self.inc_counter('cache_hit_total', labels={'cache_type': cache_type})

    def record_cache_miss(self, cache_type: str):
        """记录缓存未命中"""
        self.inc_counter('cache_miss_total', labels={'cache_type': cache_type})

    def record_workflow_run(self, status: str):
        """记录工作流运行"""
        self.inc_counter('workflow_runs_total', labels={'status': status})

    def record_workflow_recovery(self, success: bool):
        """记录工作流恢复"""
        self.inc_counter('workflow_recovery_total', labels={'result': 'success' if success else 'failed'})

    def record_outbox_retry(self, result: str):
        """记录Outbox重试"""
        self.inc_counter('outbox_retries_total', labels={'result': result})

    def record_outbound_idempotency_hit(self, match_type: str):
        """记录出站幂等/重复命中。"""
        self.inc_counter(
            'outbound_idempotency_hits_total',
            labels={'match_type': str(match_type or 'unknown')[:40]},
        )

    def record_outbound_send_attempt(self, *, channel: str, success: bool):
        """记录发送通道尝试结果。"""
        self.inc_counter(
            'outbound_send_attempts_total',
            labels={
                'channel': str(channel or 'unknown')[:20],
                'result': 'success' if success else 'failed',
            },
        )

    def record_outbound_send_latency(self, *, channel: str, stage: str, duration: float):
        """记录发送链延迟。"""
        self.observe_histogram(
            'outbound_send_latency_seconds',
            duration,
            labels={
                'channel': str(channel or 'unknown')[:20],
                'stage': str(stage or 'unknown')[:30],
            },
        )

    def update_outbound_recent_reply_store(self, *, conversations: int, entries: int):
        """更新最近回复窗口运行态。"""
        self.set_gauge('outbound_recent_reply_store_conversations', max(float(conversations), 0.0))
        self.set_gauge('outbound_recent_reply_store_entries', max(float(entries), 0.0))

    def update_outbound_dispatch_runtime(self, *, queue_size: int, active_conversations: int):
        """更新出站调度器运行态。"""
        self.set_gauge('outbound_dispatch_queue_size', max(float(queue_size), 0.0))
        self.set_gauge('outbound_dispatch_active_conversations', max(float(active_conversations), 0.0))

    def record_outbound_dispatch_wait(self, wait_seconds: float):
        """记录出站调度队列等待耗时。"""
        self.observe_histogram('outbound_dispatch_wait_latency_seconds', wait_seconds)

    def update_system_metrics(self, cpu_percent: float = None, memory_percent: float = None):
        """更新系统指标"""
        try:
            import psutil
        except ImportError:
            return

        if cpu_percent is not None:
            self.set_gauge('system_cpu_usage_percent', cpu_percent)
        else:
            try:
                self.set_gauge('system_cpu_usage_percent', psutil.cpu_percent())
            except Exception:
                pass

        if memory_percent is not None:
            self.set_gauge('system_memory_usage_percent', memory_percent)
        else:
            try:
                memory = psutil.virtual_memory()
                self.set_gauge('system_memory_usage_percent', memory.percent)
            except Exception:
                pass

    def update_vector_count(self, count: int):
        """更新向量数量"""
        self.set_gauge('rag_vector_count', count)

    def update_llm_status(self, loaded: bool):
        """更新LLM状态"""
        self.set_gauge('llm_model_loaded', 1 if loaded else 0)

    def start_metrics_server(self, port: int = 9090):
        """启动Prometheus指标服务器"""
        if not self._prometheus_available:
            logger.warning("Prometheus not available, cannot start metrics server")
            return

        try:
            from prometheus_client import start_http_server
            start_http_server(port, registry=self.registry)
            logger.info(f"Metrics server started on port {port}")
        except Exception as e:
            logger.error(f"Failed to start metrics server: {e}")


class MetricsContext:
    """指标追踪上下文管理器"""

    def __init__(self, metrics: SimpleMetrics, metric_name: str, labels: Optional[Dict] = None):
        self.metrics = metrics
        self.metric_name = metric_name
        self.labels = labels
        self.start_time: float = 0
        self.success: bool = True

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = time.time() - self.start_time
        self.metrics.observe_histogram(self.metric_name, duration, self.labels)
        if exc_type is not None:
            self.success = False
        return False

    def record(self):
        """手动记录完成"""
        duration = time.time() - self.start_time
        self.metrics.observe_histogram(self.metric_name, duration, self.labels)


# 全局指标实例
_global_metrics: Optional[SimpleMetrics] = None
_metrics_lock = threading.Lock()


def get_metrics() -> SimpleMetrics:
    """获取全局指标实例（线程安全）"""
    global _global_metrics
    if _global_metrics is None:
        with _metrics_lock:
            if _global_metrics is None:
                _global_metrics = SimpleMetrics()
    return _global_metrics


def init_metrics() -> SimpleMetrics:
    """初始化全局指标"""
    global _global_metrics
    with _metrics_lock:
        _global_metrics = SimpleMetrics()
    return _global_metrics
