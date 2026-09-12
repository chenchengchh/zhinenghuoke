"""
Bot服务 - 核心业务逻辑

职责：
1. 浏览器生命周期管理
2. 消息回复和自动回复
3. 任务调度

消息处理流程：
1. 轮询 message_monitor.fetch_messages() 获取新消息
2. 保存消息到数据库
3. 生成智能回复
4. 通过 message_monitor 发送回复
"""
import threading
import queue
import time
import hashlib
import re
import uuid
import contextlib
import concurrent.futures
import itertools
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, TypedDict, Callable
from concurrent.futures import Future
from loguru import logger
from src.douyin_bot.browser_manager import BrowserManager
from src.douyin_bot.login_handler import LoginHandler
from src.douyin_bot.crawler import Crawler
from src.douyin_bot.message_sender import MessageSender
from src.douyin_bot.message_monitor import MessageMonitor
from src.douyin_bot.rpa_launcher import RPALauncher
from src.common.chat_store import ChatStoreFacade
from src.common.database import DatabaseManager
from src.common.browser_context_service import get_browser_context_service
from src.common.monitoring import get_metrics
from src.common.purchase_intent_analyzer import get_purchase_intent_analyzer
from src.common.customer_acquisition_service import get_customer_acquisition_service
from src.common.fallback_reply_service import get_fallback_reply_service as _get_fallback_reply_service
from src.common.marketing_tracking_service import marketing_tracking_service
from src.common.multi_turn_dialogue_manager import MultiTurnDialogueManager
from src.common.proactive_service_engine import ProactiveServiceEngine
from src.common.conversation_id import build_conversation_id, resolve_conversation_id
from src.common.enhanced_customer_service import resolve_context_session_id
from src.common.industry_schema_service import get_industry_schema_service
from src.common.logical_message import build_logical_message_id
from src.common.message_workflow_manager import get_message_workflow_manager
from src.common.handoff_ticket_repository import get_handoff_ticket_repository
from src.common.reply_eligibility_service import get_reply_eligibility_service
from src.common.types.reply_eligibility import (
    EligibilityAction,
    ReplyEligibilityDecision,
    ReplyEligibilityInput,
)
from src.common.enterprise import MessageBus, SessionManager, ConfigManager, CircuitBreaker, CircuitBreakerConfig
from src.config.settings import (
    CRAWLER_QUEUE_PRIORITY_DEFAULT,
    CRAWLER_RESUME_STALE_MINUTES,
    CRAWLER_SKIP_EXISTING_VIDEOS_DEFAULT,
    CRAWLER_WORKER_THREADS_DEFAULT,
    REPLY_LLM_TIMEOUT_SECONDS,
)
from src.web.chat_session_coordinator import ChatSessionCoordinator
from src.web.conversation_switch_service import ConversationSwitchService
from src.web.inbound_reply_components import (
    _CustomerIntentGateway,
    _InboundReplyComponentFactory,
    _InboundDeliveryExecutionResult,
    _InboundDeliveryPlan,
    _InboundEventGateway,
    _MessageStateRepository,
    _InboundReplyExecutionDeps,
    _InboundReplyExecutorAccessBundle,
    _InboundReplyExecutorFailureAccessBundle,
    _InboundReplyExecutorFailureInboundStateAccessBundle,
    _InboundReplyExecutorFailureOutboxStateAccessBundle,
    _InboundReplyExecutorFailureStateAccessBundle,
    _InboundReplyExecutorFailureWorkflowBindingAccessBundle,
    _InboundReplyExecutorFailureWorkflowFailureAccessBundle,
    _InboundReplyExecutorFailureWorkflowAccessBundle,
    _InboundReplyExecutorIntentAccessBundle,
    _InboundReplyExecutionProvider,
    _InboundReplyFailureProvider,
    _InboundReplyFactoryDeps,
    _InboundReplyOrchestrator,
    _InboundReplyExecutor,
    _InboundReplyPlanDeps,
    _InboundReplyPlanProvider,
    _InboundReplyPlannerAccessBundle,
    _InboundReplyPlanner,
    _InboundReplyRequestProvider,
    _InboundReplyRequest,
    _InboundReplyRequestDeps,
    _OutboxEventGateway,
    _ReplyGenerator,
    _ReplySender,
    _WorkflowRunGateway,
)
from src.web.inbound_pipeline_service import InboundPipelineService
from src.web.inbound_source_adapter import InboundSourceAdapter
from src.web.monitoring_decision_service import MonitoringDecisionService
from src.web.outbound_delivery_orchestrator import OutboundDeliveryOrchestrator
from src.web.outbound_workflow_delivery_coordinator import OutboundWorkflowDeliveryCoordinator
from src.web.outbound_send_verifier import OutboundDeliveryAttemptResult, OutboundSendVerifier
from src.web.outbound_send_gateway import OutboundSendGateway, OutboundSendResult
from src.web.inbound_idempotency_service import InboundIdempotencyService
from src.web.outbound_dispatch_coordinator import OutboundDispatchCoordinator
from src.web.outbound_idempotency_service import OutboundIdempotencyService
from src.web.outbound_recent_reply_store import OutboundRecentReplyStore
from src.web.outbound_send_diagnostics_store import OutboundSendDiagnosticsStore
from src.infrastructure.runtime_paths import get_browser_user_data_dir
from .inbound_reply_chain_mixin import InboundReplyChainMixin
from .outbound_send_mixin import OutboundSendMixin
from .browser_lifecycle_mixin import BrowserLifecycleMixin
from .monitoring_lifecycle_mixin import MonitoringLifecycleMixin
from .reply_content_quality_mixin import ReplyContentQualityMixin
from .retry_compensation_mixin import RetryCompensationMixin
from .search_crawler_mixin import SearchCrawlerMixin
from .purchase_intent_mixin import PurchaseIntentMixin
from .task_scheduler_mixin import TaskSchedulerMixin
from .identity_resolution_mixin import IdentityResolutionMixin


def get_enhanced_customer_service():
    """提供可 monkeypatch 的增强客服服务入口。"""
    from src.common.enhanced_customer_service import get_enhanced_customer_service as _get_service

    return _get_service()


def get_unified_knowledge_service():
    """提供可 monkeypatch 的统一知识库入口。"""
    from src.common.unified_knowledge_service import get_unified_knowledge_service as _get_service

    return _get_service()


def get_fallback_reply_service():
    """提供可 monkeypatch 的通用降级回复服务入口。"""
    return _get_fallback_reply_service()


class _ReplyLockEntry(TypedDict):
    """回复锁条目类型"""
    lock: "threading.RLock"  # type: ignore[misc]
    ref_count: int


class _InboundReplyFactoryAssembly(TypedDict):
    """主回复链 factory 的高层装配包。"""
    request: _InboundReplyRequestDeps
    plan: _InboundReplyPlanDeps
    execution: _InboundReplyExecutionDeps
    failure: _InboundReplyFailureProvider
    workflow_gateway: _WorkflowRunGateway
    planner_inbound_gateway: _InboundEventGateway
    executor_inbound_gateway: _InboundEventGateway
    customer_intent_gateway: _CustomerIntentGateway
    outbox_gateway: _OutboxEventGateway


class _InboundReplyGatewayBundle(TypedDict):
    """主回复链依赖的仓储/状态网关集合。"""
    inbound_state_bundle: "_InboundReplyInboundStateBundle"
    access_bundle: "_InboundReplyAccessBundle"


class _InboundReplyInboundStateBundle(TypedDict):
    """主回复链入站状态访问边界。"""
    planner_inbound_gateway: _InboundEventGateway
    executor_inbound_gateway: _InboundEventGateway


class _InboundReplyAccessBundle(TypedDict):
    """主回复链非入站状态的访问边界。"""
    workflow_gateway: _WorkflowRunGateway
    customer_intent_gateway: _CustomerIntentGateway
    outbox_gateway: _OutboxEventGateway


class _InboundReplyStageBundle(TypedDict):
    """主回复链依赖的阶段能力集合。"""
    request: _InboundReplyRequestDeps
    plan: _InboundReplyPlanDeps
    execution: _InboundReplyExecutionDeps
    failure: _InboundReplyFailureProvider


class _InboundReplyFactoryAssemblyBuilder:
    """聚合 factory 需要的 provider/gateway，避免 BotService 直接逐项拼装。"""

    def __init__(
        self,
        *,
        stage_bundle_resolver,
        gateway_bundle_resolver,
    ):
        self._stage_bundle_resolver = stage_bundle_resolver
        self._gateway_bundle_resolver = gateway_bundle_resolver

    def build(self) -> _InboundReplyFactoryAssembly:
        stage_bundle = self._stage_bundle_resolver()
        gateway_bundle = self._gateway_bundle_resolver()
        return {
            "request": stage_bundle["request"],
            "plan": stage_bundle["plan"],
            "execution": stage_bundle["execution"],
            "failure": stage_bundle["failure"],
            "workflow_gateway": gateway_bundle["access_bundle"]["workflow_gateway"],
            "planner_inbound_gateway": gateway_bundle["inbound_state_bundle"]["planner_inbound_gateway"],
            "executor_inbound_gateway": gateway_bundle["inbound_state_bundle"]["executor_inbound_gateway"],
            "customer_intent_gateway": gateway_bundle["access_bundle"]["customer_intent_gateway"],
            "outbox_gateway": gateway_bundle["access_bundle"]["outbox_gateway"],
        }


class _InboundReplyAssembler:
    """聚合主回复链本地装配逻辑，统一暴露 assembly 与 component factory 获取边界。"""

    def __init__(
        self,
        *,
        stage_bundle_resolver: Callable[[], _InboundReplyStageBundle],
        gateway_bundle_resolver: Callable[[], _InboundReplyGatewayBundle],
        assembly_builder_resolver: Optional[Callable[[], _InboundReplyFactoryAssemblyBuilder]] = None,
    ):
        self._stage_bundle_resolver = stage_bundle_resolver
        self._gateway_bundle_resolver = gateway_bundle_resolver
        self._assembly_builder_resolver = assembly_builder_resolver

    def _resolve_stage_and_gateway_bundles(
        self,
    ) -> tuple[_InboundReplyStageBundle, _InboundReplyGatewayBundle]:
        return self._stage_bundle_resolver(), self._gateway_bundle_resolver()

    def get_factory_assembly(self) -> _InboundReplyFactoryAssembly:
        if self._assembly_builder_resolver is not None:
            return self._assembly_builder_resolver().build()
        stage_bundle, gateway_bundle = self._resolve_stage_and_gateway_bundles()
        return {
            "request": stage_bundle["request"],
            "plan": stage_bundle["plan"],
            "execution": stage_bundle["execution"],
            "failure": stage_bundle["failure"],
            "workflow_gateway": gateway_bundle["access_bundle"]["workflow_gateway"],
            "planner_inbound_gateway": gateway_bundle["inbound_state_bundle"]["planner_inbound_gateway"],
            "executor_inbound_gateway": gateway_bundle["inbound_state_bundle"]["executor_inbound_gateway"],
            "customer_intent_gateway": gateway_bundle["access_bundle"]["customer_intent_gateway"],
            "outbox_gateway": gateway_bundle["access_bundle"]["outbox_gateway"],
        }

    def get(self) -> _InboundReplyComponentFactory:
        stage_bundle, gateway_bundle = self._resolve_stage_and_gateway_bundles()
        return _InboundReplyComponentFactory(
            _InboundReplyFactoryDeps(
                request=stage_bundle["request"],
                plan=stage_bundle["plan"],
                execution=stage_bundle["execution"],
                failure=stage_bundle["failure"],
                planner_access=_InboundReplyPlannerAccessBundle(
                    inbound_gateway=gateway_bundle["inbound_state_bundle"]["planner_inbound_gateway"],
                    workflow_gateway=gateway_bundle["access_bundle"]["workflow_gateway"],
                ),
                executor_access=_InboundReplyExecutorAccessBundle(
                    intent_access=_InboundReplyExecutorIntentAccessBundle(
                        customer_intent_gateway=gateway_bundle["access_bundle"]["customer_intent_gateway"],
                    ),
                    failure_access=_InboundReplyExecutorFailureAccessBundle(
                        state_access=_InboundReplyExecutorFailureStateAccessBundle(
                            inbound_state_access=_InboundReplyExecutorFailureInboundStateAccessBundle(
                                inbound_gateway=gateway_bundle["inbound_state_bundle"]["executor_inbound_gateway"],
                            ),
                            outbox_state_access=_InboundReplyExecutorFailureOutboxStateAccessBundle(
                                outbox_gateway=gateway_bundle["access_bundle"]["outbox_gateway"],
                            ),
                        ),
                        workflow_access=_InboundReplyExecutorFailureWorkflowAccessBundle(
                            binding_access=_InboundReplyExecutorFailureWorkflowBindingAccessBundle(
                                workflow_gateway=gateway_bundle["access_bundle"]["workflow_gateway"],
                            ),
                            failure_access=_InboundReplyExecutorFailureWorkflowFailureAccessBundle(
                                workflow_gateway=gateway_bundle["access_bundle"]["workflow_gateway"],
                            ),
                        ),
                    ),
                ),
            )
        )


class BotService(
    InboundReplyChainMixin,
    OutboundSendMixin,
    BrowserLifecycleMixin,
    MonitoringLifecycleMixin,
    ReplyContentQualityMixin,
    RetryCompensationMixin,
    SearchCrawlerMixin,
    PurchaseIntentMixin,
    TaskSchedulerMixin,
    IdentityResolutionMixin,
):
    """
    Bot服务单例类

    职责：
    1. 浏览器生命周期管理
    2. 消息回复和自动回复
    3. 任务调度
    """
    _instance: Optional['BotService'] = None
    _lock = threading.Lock()
    # 类级别属性注解（让 Pylance 在 __init__ 之外的访问能识别属性类型）
    db: 'DatabaseManager'  # type: ignore[type-arg]
    chat_store: 'ChatStoreFacade'
    message_workflow_manager: Any
    is_running: bool
    current_task: str
    _stop_flag: bool
    _runtime_state_lock: 'threading.RLock'
    _browser_state: str
    _monitoring_state: str
    _last_monitor_start_error: str
    _dialogue_manager: 'MultiTurnDialogueManager'
    _proactive_engine: 'ProactiveServiceEngine'
    browser_manager: Optional['BrowserManager']
    page: Optional[Any]  # playwright Page
    login_handler: Optional['LoginHandler']
    crawler: Optional['Crawler']
    sender: Optional['MessageSender']
    message_monitor: Optional['MessageMonitor']
    rpa_launcher: Optional['RPALauncher']
    _use_rpa_mode: bool
    config_manager: 'ConfigManager'
    message_bus: 'MessageBus'
    session_manager: 'SessionManager'
    cb_config: 'CircuitBreakerConfig'
    circuit_breaker: 'CircuitBreaker'
    _boundary_guard: Any
    _page_close_cleanup_in_progress: bool
    progress_info: Dict[str, Any]
    last_search_summary: Optional[Dict[str, Any]]
    COMMENT_TIME_PRESET_LABELS = {
        "": "不限",
        "1": "1天内",
        "7": "7天内",
        "30": "30天内",
        "180": "半年内",
        "365": "1年内",
    }
    VIDEO_QUEUE_PRIORITY_LABELS = {
        "latest_unprocessed": "最新未处理优先",
        "publish_desc": "发布时间倒序",
        "hot_desc": "热度优先",
        "discovered_desc": "最近发现优先",
    }
    TASK_PRIORITY_HIGH = 0
    TASK_PRIORITY_NORMAL = 50
    TASK_PRIORITY_LOW = 100
    MAX_OUTBOX_RETRY_COUNT = 3

    # 统一消息过滤规则（RPA模式和传统模式共用）
    INVALID_MESSAGE_PATTERNS = [
        '已撤回', '正在输入', '正在加载',
        '系统消息', '系统通知', '上拉', '下拉', '滑动的',
        '[图片]', '[语音]', '[视频]', '[表情]', '[文件]', '[链接]',
        '[红包]', '[位置]', '[名片]', '[小程序]', '[商品]',
        '小时前在线', '分钟前在线', '刚刚在线',
        '对方回复或关注你之前', '只能发送一条文字消息', '抖音自律公约',
    ]

    BOT_MESSAGE_PATTERNS = [
        '您好，我是智揽人工智能模型', '您好，我是智能客服', '您好，我是小助手',
        '我收到您的消息了', '感谢您的咨询，我们会尽快',
        '关键词搜索爬取功能可以', '智能私信功能支持：',
        '远程协助服务：如遇复杂问题', '客服联系方式：您可以通过系统内',
        '对公转账需要：公司名称', '正在为您查询相关信息',
        '正在处理中，请稍等',
        '请问您想了解哪方面的信息', '请问您对哪个方案感兴趣',
        '我为您推荐合适的方案', '请问有什么可以帮您的',
    ]

    BOT_MESSAGE_REGEX_PATTERNS = [
        r'^序号[：:]\s*\d',
        r'^.{0,4}(智能客服|智能助手|AI助手|AI客服)[：:，,]',
    ]
    
    # 类型注解 - 解决IDE类型检查问题
    db: DatabaseManager
    is_running: bool
    current_task: str
    browser_manager: Optional[BrowserManager]
    page: Optional[Any]
    login_handler: Optional[LoginHandler]
    crawler: Optional[Crawler]
    sender: Optional[MessageSender]
    message_monitor: Optional[MessageMonitor]
    rpa_launcher: Optional[RPALauncher]
    message_bus: MessageBus
    message_handlers: Any
    _agent_mode: bool
    purchase_intent_analyzer: Any
    customer_acquisition_service: Any

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(BotService, cls).__new__(cls)
                cls._instance._initialized = False
                cls._instance._init_lock = threading.Lock()
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_init_lock'):
            self._init_lock = threading.Lock()
        with self._init_lock:
            if self._initialized:
                return
        
            # 数据库
            self.db = DatabaseManager()
            self.chat_store = ChatStoreFacade(self.db)
            self.message_workflow_manager = get_message_workflow_manager(self.db)
            self._inbound_pipeline_service = None
            self._inbound_source_adapter = None
            self._monitoring_decision_service = None
            self._conversation_switch_service = None

            # 状态
            self.is_running = False
            self.current_task = "Idle"
            self._stop_flag = False
            self._runtime_state_lock = threading.RLock()
            self._browser_state = "stopped"
            self._monitoring_state = "stopped"
            self._last_monitor_start_error = ""

            # 多轮对话管理
            self._dialogue_manager = MultiTurnDialogueManager()

            # 主动服务引擎
            self._proactive_engine = ProactiveServiceEngine()
        
            # 浏览器组件
            self.browser_manager = None
            self.page = None
            self.login_handler = None
            self.crawler = None
            self.sender = None
            self.message_monitor = None

            # RPA引擎组件 (新增)
            self.rpa_launcher = None
            self._use_rpa_mode = True  # 可配置是否使用RPA模式

            # 企业级组件 (新增)
            self.config_manager = ConfigManager()
            self.message_bus = MessageBus(max_workers=10)
            self.session_manager = SessionManager(db=self.db)
            self.cb_config = CircuitBreakerConfig(
                failure_threshold=5,
                success_threshold=3,
                timeout=max(float(REPLY_LLM_TIMEOUT_SECONDS), 20.0)
            )
            self.circuit_breaker = CircuitBreaker("bot_service", self.cb_config)
            self.message_bus.start()
        
            # 设置消息总线处理器
            from src.common.enterprise.message_handlers import setup_message_handlers
            self.message_handlers = setup_message_handlers(
                self.message_bus, 
                db=self.db, 
                bot_service=self
            )

            # 自动回复状态
            self.is_monitoring_messages = False
            self._monitor_active = False
            self._last_monitor_check_time = 0
            self._last_message_time = None
            self._last_send_error = ""
        
            # 未读消息数缓存（避免每次轮询都读取大JSON文件）
            self._unread_count_cache = 0
            self._unread_count_cache_time = 0
            self._unread_count_cache_ttl = 10
        
            # 消息幂等性校验缓存（防止重复处理）
            self._processed_messages_cache: Dict[str, tuple] = {}
            self._processed_messages_lock = threading.Lock()
            self._processed_messages_ttl = 120
            self._short_message_done_ttl = 90
            self._short_message_skip_ttl = 60
            self._short_message_processing_ttl = 60
            self._failed_message_retry_gate_ttl = 60
            self._pending_retry_timers: Dict[str, threading.Timer] = {}
            self._pending_retry_timers_lock = threading.Lock()
        
            # Agent模式（与旧版兼容）
            self._agent_mode = False
        
            # 登录状态缓存
            self._login_status_cache = False
            self._last_login_check_time = 0
            self._browser_context_id = str(get_browser_context_service().get_active_account_id() or "").strip()
        
            # RPA重试状态
            self._rpa_retry_count = 0
            self._rpa_recovery_attempt_time = 0
            self._rpa_last_retry_time = 0
        
            # 进度信息
            self.progress_info = {
                "total": 0,
                "current": 0,
                "detail": ""
            }
            self.last_search_summary = None
            self._search_task_pending = False
            self._active_search_task_id = ""
        
            # 购买意向分析器
            self.purchase_intent_analyzer = get_purchase_intent_analyzer(industry="software")
        
            # 获客服务
            self.customer_acquisition_service = get_customer_acquisition_service(
                database=self.db,
                message_sender=None  # 后续设置
            )
        
            # 消息轮询独立守护线程（定时触发轮询信号，Worker线程执行实际Playwright操作）
            self._monitor_poll_thread = None
            self._monitor_poll_running = False
            self._monitor_poll_stop_event = threading.Event()
            self._poll_signal = threading.Event()  # 轮询信号：轮询线程设置，Worker线程检查

            # Worker线程
            self._task_sequence = itertools.count()
            self._shutdown_task_marker = object()
            self.task_queue = self._create_task_queue()
            self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self.worker_thread.start()
        
            # 自回复检测缓存及锁
            self._recent_sent_cache = {}
            self._recent_sent_cache_lock = threading.Lock()
        
            # 会话级回复锁
            self._reply_locks: Dict[str, _ReplyLockEntry] = {}
            self._reply_locks_lock = threading.Lock()
            self._reply_locks_max_size = 200
        
            # 消息回复线程池（避免每条消息创建独立线程）
            self._reply_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=10,
                thread_name_prefix="reply_worker",
            )

            # 发送成功后的非关键落库与 tracking 后处理线程池，避免阻塞主回复链尾部。
            self._post_send_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="reply_post",
            )
        
            # LLM调用专用线程池（提供超时保护，避免LLM挂起导致线程泄漏）
            self._llm_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=5,
                thread_name_prefix="llm_worker",
            )
            self._llm_request_timeout_seconds = max(float(REPLY_LLM_TIMEOUT_SECONDS) + 5.0, 15.0)
            self._llm_slot_semaphore = threading.BoundedSemaphore(value=4)
            self._llm_slot_lock = threading.Lock()
            self._llm_inflight_count = 0
        
            # 应用状态持久化
            from src.douyin_bot.app_state_persistor import get_app_state_persistor
            self._state_persistor = get_app_state_persistor()
            self._load_persisted_state()
            self._perform_startup_recovery()

            self._initialized = True

    def _get_chat_store(self) -> ChatStoreFacade:
        chat_store = getattr(self, "chat_store", None)
        if chat_store is None:
            database = getattr(self, "db", None) or DatabaseManager()
            self.db = database
            chat_store = ChatStoreFacade(database)
            self.chat_store = chat_store
        return chat_store

    def _get_inbound_pipeline_service(self) -> InboundPipelineService:
        service = getattr(self, "_inbound_pipeline_service", None)
        if service is None:
            service = InboundPipelineService(self)
            self._inbound_pipeline_service = service
        return service

    def _get_inbound_source_adapter(self) -> InboundSourceAdapter:
        adapter = getattr(self, "_inbound_source_adapter", None)
        if adapter is None:
            adapter = InboundSourceAdapter(self)
            self._inbound_source_adapter = adapter
        return adapter

    def _get_outbound_send_gateway(self) -> OutboundSendGateway:
        gateway = getattr(self, "_outbound_send_gateway_instance", None)
        if gateway is None:
            gateway = OutboundSendGateway(self)
            self._outbound_send_gateway_instance = gateway
        return gateway

    def _get_outbound_dispatch_coordinator(self) -> OutboundDispatchCoordinator:
        coordinator = getattr(self, "_outbound_dispatch_coordinator_instance", None)
        if coordinator is None:
            coordinator = OutboundDispatchCoordinator()
            self._outbound_dispatch_coordinator_instance = coordinator
        return coordinator

    def _get_outbound_send_diagnostics_store(self) -> OutboundSendDiagnosticsStore:
        store = getattr(self, "_outbound_send_diagnostics_store_instance", None)
        if store is None:
            store = OutboundSendDiagnosticsStore()
            self._outbound_send_diagnostics_store_instance = store
        return store

    def _get_outbound_send_verifier(self) -> OutboundSendVerifier:
        verifier = getattr(self, "_outbound_send_verifier", None)
        if verifier is None:
            verifier = OutboundSendVerifier(
                normalize_live_target_name=self._normalize_live_target_name,
                get_live_conversation_name_samples=self._get_live_conversation_name_samples,
            )
            self._outbound_send_verifier = verifier
        return verifier

    def _get_inbound_idempotency_service(self) -> InboundIdempotencyService:
        service = getattr(self, "_inbound_idempotency_service_instance", None)
        if service is None:
            service = InboundIdempotencyService(
                processed_messages_ttl=getattr(self, "_processed_messages_ttl", 7200),
                short_message_done_ttl=getattr(self, "_short_message_done_ttl", 90),
                short_message_skip_ttl=getattr(self, "_short_message_skip_ttl", 60),
                short_message_processing_ttl=getattr(self, "_short_message_processing_ttl", 60),
                failed_message_retry_gate_ttl=getattr(self, "_failed_message_retry_gate_ttl", 60),
            )
            self._inbound_idempotency_service_instance = service
        return service

    def _get_outbound_idempotency_service(self) -> OutboundIdempotencyService:
        service = getattr(self, "_outbound_idempotency_service_instance", None)
        if service is None:
            service = OutboundIdempotencyService()
            self._outbound_idempotency_service_instance = service
        return service

    def _get_outbound_recent_reply_store(self) -> OutboundRecentReplyStore:
        store = getattr(self, "_outbound_recent_reply_store_instance", None)
        if store is None:
            store = OutboundRecentReplyStore()
            self._outbound_recent_reply_store_instance = store
        return store

    def _get_outbound_delivery_orchestrator(self) -> OutboundDeliveryOrchestrator:
        orchestrator = getattr(self, "_outbound_delivery_orchestrator", None)
        if orchestrator is None:
            orchestrator = OutboundDeliveryOrchestrator(self)
            self._outbound_delivery_orchestrator = orchestrator
        return orchestrator

    def _get_outbound_workflow_delivery_coordinator(self) -> OutboundWorkflowDeliveryCoordinator:
        coordinator = getattr(self, "_outbound_workflow_delivery_coordinator", None)
        if coordinator is None:
            coordinator = OutboundWorkflowDeliveryCoordinator(
                cancel_pending_retry_timer=self._cancel_pending_retry_timer,
                state_repository=self._get_inbound_message_state_repository(),
                workflow_manager=self.message_workflow_manager,
            )
            self._outbound_workflow_delivery_coordinator = coordinator
        return coordinator

    def _get_monitoring_decision_service(self) -> MonitoringDecisionService:
        service = getattr(self, "_monitoring_decision_service", None)
        if service is None:
            service = MonitoringDecisionService(self)
            self._monitoring_decision_service = service
        return service

    def _get_conversation_switch_service(self) -> ConversationSwitchService:
        service = getattr(self, "_conversation_switch_service", None)
        if service is None:
            service = ConversationSwitchService(self)
            self._conversation_switch_service = service
        return service

    def _get_chat_session_coordinator(self) -> ChatSessionCoordinator:
        coordinator = getattr(self, "_chat_session_coordinator", None)
        if coordinator is None:
            coordinator = ChatSessionCoordinator(self)
            self._chat_session_coordinator = coordinator
        return coordinator

    # ==================== 状态查询 ====================

    def _ensure_runtime_state_lock(self):
        if not hasattr(self, "_runtime_state_lock") or self._runtime_state_lock is None:
            self._runtime_state_lock = threading.RLock()

    def _update_runtime_state(
        self,
        *,
        is_running: Optional[bool] = None,
        current_task: Optional[str] = None,
        stop_flag: Optional[bool] = None,
        is_monitoring_messages: Optional[bool] = None,
        monitor_active: Optional[bool] = None,
        browser_state: Optional[str] = None,
        monitoring_state: Optional[str] = None,
        last_monitor_check_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """统一维护运行时共享状态，避免多线程直接散写。"""
        self._ensure_runtime_state_lock()
        with self._runtime_state_lock:
            if is_running is not None:
                self.is_running = is_running
            if current_task is not None:
                self.current_task = current_task
            if stop_flag is not None:
                self._stop_flag = stop_flag
            # Phase 5 关键修复：is_monitoring_messages 与 monitor_active 同步
            # 以 monitor_active 为唯一权威字段，is_monitoring_messages 由其派生
            if monitor_active is not None:
                self._monitor_active = monitor_active
                self.is_monitoring_messages = monitor_active  # 自动同步
            elif is_monitoring_messages is not None:
                # 兼容旧调用：以传入的 is_monitoring_messages 为准并同步
                self.is_monitoring_messages = is_monitoring_messages
                self._monitor_active = is_monitoring_messages
            if browser_state is not None:
                self._browser_state = browser_state
            if monitoring_state is not None:
                self._monitoring_state = monitoring_state
                # monitoring_state=stopped 时强制同步关闭
                if monitoring_state == "stopped":
                    self._monitor_active = False
                    self.is_monitoring_messages = False
            if last_monitor_check_time is not None:
                self._last_monitor_check_time = last_monitor_check_time
            return {
                "is_running": self.is_running,
                "current_task": self.current_task,
                "stop_flag": self._stop_flag,
                "is_monitoring_messages": self.is_monitoring_messages,
                "monitor_active": self._monitor_active,
                "browser_state": getattr(self, "_browser_state", "unknown"),
                "monitoring_state": getattr(self, "_monitoring_state", "unknown"),
                "last_monitor_check_time": getattr(self, "_last_monitor_check_time", 0),
            }

    def _get_runtime_state_snapshot(self) -> Dict[str, Any]:
        self._ensure_runtime_state_lock()
        with self._runtime_state_lock:
            return {
                "is_running": getattr(self, "is_running", False),
                "current_task": getattr(self, "current_task", "Idle"),
                "stop_flag": getattr(self, "_stop_flag", False),
                "is_monitoring_messages": getattr(self, "is_monitoring_messages", False),
                "monitor_active": getattr(self, "_monitor_active", False),
                "browser_state": getattr(self, "_browser_state", "unknown"),
                "monitoring_state": getattr(self, "_monitoring_state", "unknown"),
                "last_monitor_check_time": getattr(self, "_last_monitor_check_time", 0),
            }

    def get_browser_context_id(self) -> str:
        browser_context_id = str(getattr(self, "_browser_context_id", "") or "").strip()
        if browser_context_id:
            return browser_context_id
        browser_context_id = str(get_browser_context_service().get_active_account_id() or "").strip()
        self._browser_context_id = browser_context_id
        return browser_context_id

    def set_browser_context(self, context_id: str) -> str:
        self._browser_context_id = str(context_id or "").strip()
        return self._browser_context_id

    def _resolve_browser_user_data_dir(self):
        browser_context_id = self.get_browser_context_id()
        if browser_context_id:
            return get_browser_context_service().resolve_browser_user_data_dir(browser_context_id)
        return get_browser_user_data_dir()

    def _resolve_runtime_account_scope(self, force_refresh: bool = False) -> Dict[str, Any]:
        if not self.browser_manager:
            return {"account_id": "", "resolved": False, "source": ""}
        rpa_engine = getattr(getattr(self, "rpa_launcher", None), "rpa_engine", None)
        if rpa_engine and hasattr(rpa_engine, "resolve_my_user_id"):
            try:
                account_id = str(rpa_engine.resolve_my_user_id() or "").strip()
                if account_id:
                    return {"account_id": account_id, "resolved": True, "source": "rpa_engine"}
            except Exception as exc:
                logger.debug(f"通过 RPA 引擎解析当前运行账号失败: {exc}")
        target_page = (
            getattr(self.browser_manager, "monitor_page", None)
            or getattr(self.browser_manager, "page", None)
            or getattr(self, "page", None)
        )
        if not target_page:
            return {"account_id": "", "resolved": False, "source": ""}
        try:
            sender = getattr(self, "sender", None)
            api_interceptor = getattr(getattr(self, "message_monitor", None), "api_interceptor", None)
            if not sender or getattr(sender, "page", None) is not target_page:
                sender = MessageSender(target_page, self.db, api_interceptor=api_interceptor)
            else:
                sender.api_interceptor = api_interceptor
            return dict(sender.resolve_current_account_scope(force_refresh=force_refresh) or {})
        except Exception as exc:
            logger.debug(f"解析当前运行账号失败: {exc}")
            return {"account_id": "", "resolved": False, "source": ""}

    def get_browser_runtime_snapshot(
        self,
        *,
        force_login_check: bool = False,
        force_scope_refresh: bool = False,
        persist_registry: bool = False,
    ) -> Dict[str, Any]:
        browser_context_id = self.get_browser_context_id()
        account_service = get_browser_context_service()
        browser_context = account_service.get_account(browser_context_id) if browser_context_id else None
        browser_user_data_dir = str(self._resolve_browser_user_data_dir())
        is_running = bool(self._sync_browser_runtime_state() and self.browser_manager)
        is_logged_in = bool(getattr(self, "_login_status_cache", False))
        if force_login_check and is_running:
            try:
                is_logged_in = bool(self.check_login())
            except Exception:
                is_logged_in = False

        scope = {"account_id": "", "resolved": False, "source": ""}
        if is_running:
            try:
                future = self._submit_task(
                    self._resolve_runtime_account_scope,
                    force_refresh=force_scope_refresh,
                )
                scope = dict(future.result(timeout=3) or {})
                self._runtime_account_scope_cache = {
                    "account_id": str(scope.get("account_id") or "").strip(),
                    "resolved": bool(scope.get("resolved", False)),
                    "source": str(scope.get("source") or "").strip(),
                }
            except Exception as exc:
                logger.warning(f"解析当前运行账号范围超时或失败，已降级跳过: {exc}")
                scope = {"account_id": "", "resolved": False, "source": ""}

        payload = {
            "browser_context_id": browser_context_id,
            "browser_context": browser_context,
            "browser_user_data_dir": browser_user_data_dir,
            "browser_running": is_running,
            "is_logged_in": is_logged_in,
            "current_login_account_id": str(scope.get("account_id") or "").strip(),
            "current_login_account_source": str(scope.get("source") or "").strip(),
            "current_login_account_resolved": bool(scope.get("resolved", False)),
        }

        if persist_registry and browser_context_id:
            try:
                account_service.note_login_snapshot(
                    browser_context_id,
                    is_logged_in=is_logged_in,
                    resolved_account_id=payload["current_login_account_id"],
                    resolved_account_source=payload["current_login_account_source"],
                    last_error="" if is_logged_in else "等待扫码登录",
                )
                payload["browser_context"] = account_service.get_account(browser_context_id)
            except Exception as exc:
                logger.debug(f"更新账户登录快照失败: {exc}")

        return payload

    def _get_lightweight_browser_runtime_snapshot(self) -> Dict[str, Any]:
        browser_context_id = self.get_browser_context_id()
        account_service = get_browser_context_service()
        browser_context = account_service.get_account(browser_context_id) if browser_context_id else None
        browser_user_data_dir = str(self._resolve_browser_user_data_dir())
        runtime_state = self._get_runtime_state_snapshot()
        browser_state = str(runtime_state.get("browser_state", "") or "").strip().lower()
        browser_running = bool(runtime_state.get("is_running")) and browser_state not in {"", "stopped", "unknown"}

        scope_cache = dict(getattr(self, "_runtime_account_scope_cache", {}) or {})
        cached_account_id = str(scope_cache.get("account_id") or "").strip()
        if not cached_account_id and isinstance(browser_context, dict):
            cached_account_id = str(browser_context.get("resolved_account_id") or "").strip()
        cached_source = str(scope_cache.get("source") or "").strip()
        if not cached_source and isinstance(browser_context, dict):
            cached_source = str(browser_context.get("resolved_account_source") or "").strip()
        cached_resolved = bool(scope_cache.get("resolved", False) or cached_account_id)

        return {
            "browser_context_id": browser_context_id,
            "browser_context": browser_context,
            "browser_user_data_dir": browser_user_data_dir,
            "browser_running": browser_running,
            "is_logged_in": bool(getattr(self, "_login_status_cache", False)),
            "current_login_account_id": cached_account_id,
            "current_login_account_source": cached_source,
            "current_login_account_resolved": cached_resolved,
        }

    def _set_last_send_error(self, reason: str) -> str:
        normalized = str(reason or "").strip()
        self._last_send_error = normalized[:200]
        return self._last_send_error

    def _clear_last_send_error(self):
        self._last_send_error = ""

    def _get_last_send_error(self, default: str = "send_failed") -> str:
        reason = str(getattr(self, "_last_send_error", "") or "").strip()
        return reason or default

    def _set_last_send_target_name(self, customer_name: str) -> str:
        normalized = str(customer_name or "").strip()
        self._last_send_target_name = normalized[:100]
        return self._last_send_target_name

    def _clear_last_send_target_name(self):
        self._last_send_target_name = ""
        self._last_send_target_meta = {}

    def _get_last_send_target_name(self, default: str = "") -> str:
        target = str(getattr(self, "_last_send_target_name", "") or "").strip()
        return target or str(default or "").strip()

    def _set_last_send_target_meta(self, **kwargs) -> dict:
        payload = {
            "requested_name": str(kwargs.get("requested_name", "") or "").strip()[:100],
            "resolved_name": str(kwargs.get("resolved_name", "") or "").strip()[:100],
            "source": str(kwargs.get("source", "") or "").strip()[:40],
            "identity_level": str(kwargs.get("identity_level", "") or "").strip()[:40],
            "conversation_id": str(kwargs.get("conversation_id", "") or "").strip()[:120],
            "customer_id": str(kwargs.get("customer_id", "") or "").strip()[:120],
            "platform": str(kwargs.get("platform", "douyin") or "douyin").strip()[:40],
        }
        self._last_send_target_meta = payload
        return payload

    def _get_last_send_target_meta(self) -> dict:
        payload = getattr(self, "_last_send_target_meta", {}) or {}
        return payload if isinstance(payload, dict) else {}

    def get_send_diagnostics(self, trace_id: str = "") -> dict:
        diagnostics = self._get_outbound_send_gateway().get_diagnostics(trace_id=trace_id)
        coordinator = self._get_outbound_dispatch_coordinator()
        with contextlib.suppress(Exception):
            diagnostics["dispatch_runtime"] = coordinator.get_runtime_snapshot()
        return diagnostics

    def _update_llm_runtime_metrics(self):
        """刷新 LLM 执行器运行态指标。"""
        metrics = get_metrics()
        inflight = getattr(self, "_llm_inflight_count", 0)
        executor = getattr(self, "_llm_executor", None)
        capacity = int(getattr(executor, "_max_workers", 0) or 0)
        queued = 0
        if executor is not None:
            work_queue = getattr(executor, "_work_queue", None)
            if work_queue is not None and hasattr(work_queue, "qsize"):
                try:
                    queued = max(int(work_queue.qsize()), 0)
                except Exception:
                    queued = 0
        metrics.update_llm_executor(inflight=inflight, queued=queued, capacity=capacity)
        try:
            if hasattr(self, "circuit_breaker") and self.circuit_breaker:
                stats = self.circuit_breaker.get_stats()
                metrics.update_circuit_breaker(
                    state=stats.get("state", "unknown"),
                    failure_count=int(stats.get("failure_count", 0) or 0),
                    success_count=int(stats.get("success_count", 0) or 0),
                )
        except Exception as metrics_e:
            logger.debug(f"刷新熔断器指标失败: {metrics_e}")

    def _log_reply_chain_perf(self, label: str, trace_id: str, metrics: Dict[str, float], **extra_fields: Any) -> None:
        """统一输出主回复链性能日志。"""
        metric_parts = [f"{key}={value:.1f}ms" for key, value in metrics.items()]
        extra_parts = [
            f"{key}={value}"
            for key, value in extra_fields.items()
            if value is not None and value != ""
        ]
        parts = [f"trace={trace_id}"] + metric_parts + extra_parts
        logger.info(f"[回复主链] PERF {label} | {' | '.join(parts)}")

    @staticmethod
    def _record_reply_chain_metric(metrics: Dict[str, float], stage: str, started_at: float) -> float:
        """记录主回复链单阶段耗时，单位毫秒。"""
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        metrics[stage] = elapsed_ms
        return elapsed_ms

    def _submit_llm_request(self, func, /, *args, **kwargs) -> tuple[Optional[Future], Optional[str]]:
        """提交 LLM 请求，带并发配额和完成回调释放。"""
        if not hasattr(self, "_llm_slot_semaphore") or self._llm_slot_semaphore is None:
            self._llm_slot_semaphore = threading.BoundedSemaphore(value=4)
        if not hasattr(self, "_llm_slot_lock") or self._llm_slot_lock is None:
            self._llm_slot_lock = threading.Lock()
        if not hasattr(self, "_llm_inflight_count"):
            self._llm_inflight_count = 0

        if not self._llm_slot_semaphore.acquire(blocking=False):
            self._update_llm_runtime_metrics()
            get_metrics().record_llm_error("executor_saturated")
            return None, "executor_saturated"

        with self._llm_slot_lock:
            self._llm_inflight_count += 1
        self._update_llm_runtime_metrics()

        released = False

        def _release_callback(_fut):
            nonlocal released
            with self._llm_slot_lock:
                if released:
                    return
                released = True
                self._llm_inflight_count = max(self._llm_inflight_count - 1, 0)
            try:
                self._llm_slot_semaphore.release()
            except ValueError:
                logger.debug("LLM 配额释放时检测到重复 release，已忽略")
            self._update_llm_runtime_metrics()

        future = self._llm_executor.submit(func, *args, **kwargs)
        future.add_done_callback(_release_callback)
        return future, None
    
    def _load_persisted_state(self):
        """从持久化存储加载应用状态"""
        try:
            bot_state = self._state_persistor.load_bot_service_state()
            if bot_state:
                source_hash_map = bot_state.get('source_content_hash_map', {})
                if 'processed_messages_cache' in bot_state:
                    self._get_inbound_idempotency_service().restore_cache(
                        (bot_state['processed_messages_cache'], source_hash_map)
                    )
                if 'outbound_idempotency_cache' in bot_state:
                    self._get_outbound_idempotency_service().restore_cache(bot_state['outbound_idempotency_cache'])
                elif 'recent_sent_cache' in bot_state:
                    self._get_outbound_idempotency_service().restore_cache(bot_state['recent_sent_cache'])
                if 'recent_sent_cache' in bot_state:
                    self._get_outbound_recent_reply_store().restore_snapshot(bot_state['recent_sent_cache'])
                if 'login_status_cache' in bot_state:
                    self._login_status_cache = bot_state['login_status_cache']
                if 'use_rpa_mode' in bot_state:
                    self._use_rpa_mode = bot_state['use_rpa_mode']
                logger.info("已从持久化存储恢复BotService状态")
        except Exception as e:
            logger.warning(f"加载持久化BotService状态失败: {e}")

        try:
            rpa_state = self._state_persistor.load_rpa_engine_state()
            if rpa_state and self.rpa_launcher and self.rpa_launcher.rpa_engine:
                self.rpa_launcher.rpa_engine.restore_persisted_state(rpa_state)
                logger.info("已从持久化存储恢复RPA引擎状态")
        except Exception as e:
            logger.warning(f"加载RPA引擎持久化状态失败: {e}")

        try:
            boundary_guard_state = self._state_persistor.load_boundary_guard_state()
            if boundary_guard_state and hasattr(self, '_boundary_guard') and self._boundary_guard:
                self._boundary_guard.restore_state(boundary_guard_state)
                logger.info("已从持久化存储恢复边界保护器状态")
        except Exception as e:
            logger.debug(f"加载边界保护器持久化状态失败: {e}")

        try:
            session_state = self._state_persistor.load_session_state()
            if session_state and hasattr(self, 'session_manager') and self.session_manager:
                if hasattr(self.session_manager, 'restore_state'):
                    self.session_manager.restore_state(session_state)  # type: ignore[attr-defined]
                    logger.info("已从持久化存储恢复会话管理器状态")
        except Exception as e:
            logger.debug(f"加载会话管理器持久化状态失败: {e}")

        try:
            monitor_state = self._state_persistor.load_message_monitor_state()
            if monitor_state:
                logger.debug("已加载消息监控持久化状态(运行时由监控服务自行恢复)")
        except Exception as e:
            logger.debug(f"加载消息监控持久化状态失败: {e}")

    def _perform_startup_recovery(self):
        """执行启动恢复操作，清理上一次运行遗留的不一致状态"""
        try:
            recovery_result = self._state_persistor.perform_startup_recovery(db_manager=self.db)
            if recovery_result.get('orphan_reservations_cleaned', 0) > 0:
                logger.info(f"启动恢复: 清理了 {recovery_result['orphan_reservations_cleaned']} 条孤立预留记录")

            idempotency = self._get_inbound_idempotency_service()
            snapshot = idempotency.get_cache_snapshot()
            stale_keys = []
            for key, (_ts, status) in snapshot.items():
                if status == "processing":
                    stale_keys.append(key)
            with idempotency._lock:
                for key in stale_keys:
                    if key in idempotency._cache:
                        idempotency._cache[key] = (time.time(), "failed_3")
            if stale_keys:
                logger.info(f"启动恢复: 清理了 {len(stale_keys)} 条processing状态的缓存记录")

            try:
                cleaned = self.db.cleanup_orphan_reservations()
                if cleaned > 0:
                    logger.info(f"启动恢复: 清理了 {cleaned} 条孤立预留记录")
            except Exception as db_e:
                logger.warning(f"启动恢复: 清理孤立预留记录失败: {db_e}")

            try:
                if self.rpa_launcher and self.rpa_launcher.rpa_engine:
                    bg = self.rpa_launcher.rpa_engine._boundary_guard
                    if bg and bg._locked_until and bg._locked_until < time.time():
                        logger.info(f"启动恢复: 边界保护器锁定已过期，重置为正常状态")
                        bg._protection_level = type(bg._protection_level)('normal')
                        bg._consecutive_failures = 0
                        bg._locked_until = 0
            except Exception as bg_e:
                logger.debug(f"启动恢复: 边界保护器检查跳过: {bg_e}")

            logger.info("启动恢复操作完成")
        except Exception as e:
            logger.warning(f"启动恢复操作失败: {e}")

    def _restore_monitoring_state(self):
        """恢复自动回复状态（在浏览器初始化完成后调用）

        [FIX-INST:start-reply-bug] 修复根因 A：浏览器上下文已被 crawler 进程重建时，
        旧的 self.page / self.browser_manager.monitor_page 引用已失效（page 被关闭），
        之前的实现会直接 return 跳过自动恢复。本方法会主动调用
        browser_manager.get_monitor_page() 重建 monitor_page，最多重试 5 次。
        """
        try:
            monitoring_state = self._state_persistor.load_monitoring_state()
            if not monitoring_state:
                logger.info("无自动回复状态需要恢复")
                return

            was_monitoring = monitoring_state.get('is_monitoring', False)
            if was_monitoring:
                if not self.browser_manager:
                    logger.warning("浏览器管理器未初始化，跳过自动恢复回复")
                    return

                # [FIX-INST:start-reply-bug] page 失效时不直接 return，主动重建 monitor_page
                page_ready = bool(self.page) and not self.page.is_closed()
                if not page_ready:
                    logger.info("当前 page 已失效或未就绪，尝试通过 get_monitor_page 重建 monitor_page")
                    deadline = time.time() + 5
                    rebuilt_page = None
                    while time.time() < deadline:
                        try:
                            candidate = self.browser_manager.get_monitor_page()
                        except Exception as rebuild_e:
                            candidate = None
                            logger.debug(f"重建 monitor_page 失败，将重试: {rebuild_e}")
                        if candidate and not candidate.is_closed():
                            rebuilt_page = candidate
                            break
                        time.sleep(0.5)
                    if rebuilt_page is not None:
                        self.page = rebuilt_page
                        page_ready = True
                        logger.info("monitor_page 已通过 get_monitor_page 重建成功")
                    else:
                        logger.warning("monitor_page 重建失败，将跳过本次自动恢复，由用户手动启动")
                        return

                if not page_ready:
                    logger.warning("浏览器未就绪，跳过自动恢复回复")
                    return
                logger.info("检测到上次运行时正在运行自动回复，将自动恢复回复")
                self._auto_restore_monitoring_pending = True
                self._submit_task_no_wait(self._auto_restore_monitoring)
            else:
                logger.info("上次运行时未运行自动回复，跳过自动恢复")
        except Exception as e:
            logger.warning(f"恢复自动回复状态失败: {e}")

    def _auto_restore_monitoring(self):
        """自动恢复消息回复（延迟执行，等待浏览器完全就绪）"""
        try:
            logger.info("等待自动恢复条件就绪...")
            deadline = time.time() + 5
            while time.time() < deadline:
                page_ready = bool(self.page) and not self.page.is_closed()
                if self.is_running and self.browser_manager and page_ready:
                    break
                time.sleep(0.2)

            if not self.is_running or not self.browser_manager or not self.page or self.page.is_closed():
                logger.warning("浏览器或聊天页未就绪，跳过自动恢复回复")
                return

            if not self._login_status_cache:
                try:
                    if self.login_handler:
                        status = self.login_handler.check_login_status()
                        self._login_status_cache = status
                        self._last_login_check_time = time.time()
                        if not status:
                            logger.warning("未登录，跳过自动恢复回复")
                            return
                except Exception as e:
                    logger.warning(f"检查登录状态失败，跳过自动恢复: {e}")
                    return

            logger.info("开始自动恢复消息回复...")
            if self.start_message_monitoring(from_auto_restore=True):
                logger.info("消息回复自动恢复完成")
            else:
                logger.warning(
                    "消息回复自动恢复失败: "
                    f"{getattr(self, '_last_monitor_start_error', '') or 'unknown error'}"
                )
        except Exception as e:
            logger.error(f"自动恢复消息回复失败: {e}")
        finally:
            self._auto_restore_monitoring_pending = False

    def _save_state_before_shutdown(self):
        """关闭前保存应用状态"""
        try:
            rpa_engine = None
            boundary_guard = None
            if self.rpa_launcher and self.rpa_launcher.rpa_engine:
                rpa_engine = self.rpa_launcher.rpa_engine
                boundary_guard = rpa_engine._boundary_guard

            self._state_persistor.save_all(
                bot_service=self,
                rpa_engine=rpa_engine,
                boundary_guard=boundary_guard,
                session_manager=self.session_manager,
                message_monitor=self.message_monitor,
            )
        except Exception as e:
            logger.error(f"保存应用状态失败: {e}")

    def get_status(self):
        """获取当前状态"""
        state = self._get_runtime_state_snapshot()
        monitoring_status = self._get_monitoring_status()
        # 优化：改用 shallow copy 减少大数据量下的 CPU 开销
        progress_info = getattr(self, "progress_info", {})
        progress_snapshot = progress_info.copy() if isinstance(progress_info, dict) else {}
        
        last_search_summary = getattr(self, "last_search_summary", None)
        search_summary_snapshot = last_search_summary.copy() if isinstance(last_search_summary, dict) else last_search_summary
        
        effective_monitoring = bool(monitoring_status["effective_monitoring"])
        monitor_source = "bot_service" if effective_monitoring else "none"
        # 后台更新登录状态
        last_login_check_time = getattr(self, "_last_login_check_time", 0)
        task_queue = getattr(self, "task_queue", None)
        task_queue_size = task_queue.qsize() if task_queue and hasattr(task_queue, "qsize") else 0
        # 自动回复运行中时，不再通过状态接口高频塞入重型登录检查任务。
        # 否则会和消息轮询共用同一 Worker，导致页面/轮询表现为“卡住”。
        should_refresh_login_status = (
            (time.time() - last_login_check_time > 10)
            and (state["current_task"] == "Idle")
            and (task_queue_size == 0)
            and state["is_running"]
            and not effective_monitoring
        )
        if should_refresh_login_status:
            self._submit_task_no_wait(self._update_login_status_task)
        elif effective_monitoring and state["is_running"] and (time.time() - last_login_check_time > 10):
            logger.debug("自动回复运行中，跳过状态接口触发的登录检查任务，避免阻塞消息轮询")
        
        # 获取未读消息数（带缓存，避免每次轮询都读取大JSON文件）
        current_time = time.time()
        unread_count_cache_time = getattr(self, "_unread_count_cache_time", 0)
        unread_count_cache_ttl = getattr(self, "_unread_count_cache_ttl", 10)
        if current_time - unread_count_cache_time >= unread_count_cache_ttl:
            try:
                self._unread_count_cache = self.db.get_unread_message_count()
                self._unread_count_cache_time = current_time
            except Exception as e:
                logger.debug(f"获取未读消息数失败: {e}")
        unread_count = getattr(self, "_unread_count_cache", 0)
        
        # 调试信息：如果是刚启动不久，记录日志
        if state["is_running"] and (time.time() - last_login_check_time < 30):
            logger.debug(
                f"登录状态缓存：{getattr(self, '_login_status_cache', False)}, "
                f"最后检查时间：{time.time() - last_login_check_time:.1f}秒前"
            )
        
        current_url = ""
        try:
            if getattr(self, "rpa_launcher", None) and getattr(self.rpa_launcher, "rpa_engine", None) and getattr(self.rpa_launcher.rpa_engine, "page", None):  # type: ignore[union-attr]
                current_url = self.rpa_launcher.rpa_engine.page.url  # type: ignore[union-attr]
            elif getattr(self, "message_monitor", None) and getattr(self.message_monitor, "page", None):  # type: ignore[union-attr]
                current_url = self.message_monitor.page.url  # type: ignore[union-attr]
        except Exception:
            pass

        return {
            "current_url": current_url,
            "is_running": state["is_running"],
            "is_logged_in": getattr(self, "_login_status_cache", False),
            "current_task": state["current_task"],
            "progress": progress_snapshot,
            "last_search_summary": search_summary_snapshot,
            "is_monitoring_messages": state["is_monitoring_messages"],
            "effective_monitoring": effective_monitoring,
            "monitor_source": monitor_source,
            "browser_state": state["browser_state"],
            "monitoring_state": state["monitoring_state"],
            "unread_message_count": unread_count,
            **self._get_lightweight_browser_runtime_snapshot(),
        }

    def _is_browser_session_alive(self) -> bool:
        browser_manager = getattr(self, "browser_manager", None)
        if not browser_manager:
            return False

        try:
            if hasattr(browser_manager, "is_session_alive"):
                return bool(browser_manager.is_session_alive())
        except Exception as e:
            logger.debug(f"检查浏览器会话活性失败: {e}")
            return False

        return False

    def _sync_browser_runtime_state(self) -> bool:
        """当浏览器已被手动关闭时，主动纠正残留运行态。"""
        state = self._get_runtime_state_snapshot()
        has_stale_runtime_mark = state.get("browser_state") in {"running", "starting", "stopping", "recovering", "faulted"}
        if not (
            state.get("is_running")
            or has_stale_runtime_mark
        ):
            return False

        if self._is_browser_session_alive():
            return True

        logger.info("检测到浏览器运行态残留但实际会话已失效，重置为 stopped")
        self._update_runtime_state(
            is_running=False,
            browser_state="stopped",
            current_task="Idle",
            is_monitoring_messages=False,
            monitor_active=False,
            monitoring_state="stopped",
            stop_flag=False,
        )
        self._login_status_cache = False
        try:
            if hasattr(self, "_state_persistor") and self._state_persistor:
                self._state_persistor.save_all(self)
        except Exception as persist_exc:
            logger.debug(f"浏览器状态纠正后持久化失败: {persist_exc}")
        return False

    def _knowledge_driven_fallback_reply(
        self,
        customer_name: str,
        user_message: str,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> tuple[str, str]:
        """兼容旧调用，实际委托给通用降级回复服务。"""
        fallback_service = get_fallback_reply_service()
        try:
            return fallback_service.knowledge_fallback_reply(
                customer_name,
                user_message,
                enterprise_id=enterprise_id,
                schema_id=schema_id,
            )
        except TypeError as exc:
            exc_text = str(exc or "")
            if "enterprise_id" not in exc_text and "schema_id" not in exc_text:
                raise
            return fallback_service.knowledge_fallback_reply(customer_name, user_message)

    def _make_contextual_fallback_reply(
        self,
        customer_name: str,
        user_message: str,
        reason: str,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> dict:
        """优先使用知识库的降级回复；无知识时返回跨行业通用澄清。"""
        fallback_service = get_fallback_reply_service()
        try:
            return fallback_service.make_contextual_fallback_reply(
                customer_name,
                user_message,
                reason,
                enterprise_id=enterprise_id,
                schema_id=schema_id,
            )
        except TypeError as exc:
            exc_text = str(exc or "")
            if "enterprise_id" not in exc_text and "schema_id" not in exc_text:
                raise
            return fallback_service.make_contextual_fallback_reply(customer_name, user_message, reason)

    def _build_knowledge_only_fallback_result(
        self,
        customer_name: str,
        user_message: str,
        reason: str,
        *,
        source: str = "knowledge_only_fallback",
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> dict:
        """唯一非 LLM 兜底：只做知识库检索直出；未命中则不自动回复。"""
        try:
            knowledge_reply, matched_question = self._knowledge_driven_fallback_reply(
                customer_name,
                user_message,
                enterprise_id=enterprise_id,
                schema_id=schema_id,
            )
        except TypeError as exc:
            exc_text = str(exc or "")
            if "enterprise_id" not in exc_text and "schema_id" not in exc_text:
                raise
            knowledge_reply, matched_question = self._knowledge_driven_fallback_reply(
                customer_name,
                user_message,
            )
        if knowledge_reply:
            return {
                "reply": knowledge_reply,
                "intent_level": "C",
                "intent_score": 25,
                "need_human": False,
                "matched_knowledge": matched_question or "知识库兜底",
                "suggested_action": "",
                "fallback_reason": reason,
                "source": source,
            }
        return {
            "reply": "",
            "intent_level": "B",
            "intent_score": 20,
            "need_human": False,
            "reply_disposition": "skip",
            "reason_code": reason,
            "matched_knowledge": "",
            "suggested_action": "",
            "fallback_reason": reason,
            "source": f"{source}_miss",
        }

    @staticmethod
    def _build_deferred_llm_failure_result(reason: str, *, source: str = "llm_generation_deferred") -> dict:
        """外层基础设施故障只暴露状态，不再越权改写成知识库业务回复。"""
        return {
            "reply": "",
            "intent_level": "B",
            "intent_score": 20,
            "need_human": False,
            "reply_disposition": "defer",
            "reason_code": "llm_generation_deferred",
            "matched_knowledge": "",
            "suggested_action": "",
            "fallback_reason": reason,
            "source": source,
        }

    @staticmethod
    def _has_grounded_reply_evidence(smart_result: dict) -> bool:
        matched_knowledge = str((smart_result or {}).get("matched_knowledge", "") or "").strip()
        if matched_knowledge and matched_knowledge != "LLM生成":
            return True

        source = str((smart_result or {}).get("source", "") or "").strip().lower()
        if not source:
            return False
        knowledge_markers = (
            "knowledge_base",
            "knowledge_fallback",
            "grounded",
            "enhanced_base",
        )
        return any(marker in source for marker in knowledge_markers)

    @staticmethod
    def _detect_unverified_claim_categories(reply_content: str) -> list[str]:
        text = str(reply_content or "").strip()
        if not text:
            return []

        categories: list[str] = []
        if re.search(r"(¥|\b\d+(?:\.\d+)?\s*(元|块|w|万|折)\b|报价|价格|费用|收费|预算|票价)", text, re.IGNORECASE):
            categories.append("price")
        if re.search(r"(优惠|折扣|活动价|特价|限时|返现|返\d+|减免|立减|满减|优惠券)", text, re.IGNORECASE):
            categories.append("promotion")
        if re.search(r"(政策|规则|包含|不含|退改|退款|售后|质保|保修|流程|时效|有效期|名额)", text, re.IGNORECASE):
            categories.append("policy")
        if re.search(r"(保证|包过|一定|绝对|肯定|确保|马上安排|当天安排|立即开通|百分之百)", text, re.IGNORECASE):
            categories.append("promise")
        return categories

    @staticmethod
    def _classify_reply_content_risks(reply_content: str) -> list[str]:
        text = str(reply_content or "").strip()
        if not text:
            return []

        categories: list[str] = []
        if re.search(
            r"^(关键词搜索爬取功能|智能私信功能支持|评论自动回复功能支持|营销活动效果追踪功能支持|营销ROI计算功能支持)",
            text,
        ):
            categories.append("system_capability")

        math_hits = 0
        for pattern in (
            r"无法给出数学计算结果",
            r"没有包含任何具体的数学问题",
            r"从未出现任何数学问题",
            r"不存在任何数学问题",
            r"\\frac\{",
            r"\$\$",
            r"LaTeX",
        ):
            if re.search(pattern, text):
                math_hits += 1
        if math_hits >= 3:
            categories.append("math_hallucination")

        if re.search(r"(加微信|留微信|加v|vx|V:|微信号|手机号|电话联系)", text, re.IGNORECASE):
            categories.append("contact_risk")

        if re.search(r"(保证|包过|绝对|百分之百)", text, re.IGNORECASE):
            categories.append("overpromise")

        return categories

    @staticmethod
    def _rewrite_reply_content_by_risks(reply_content: str, risks: list[str]) -> str:
        text = str(reply_content or "").strip()
        if not text or not risks:
            return text

        if "contact_risk" in risks:
            text = re.sub(r"加微信|留微信|加v|vx|V:", "留个接收资料的联系方式", text, flags=re.IGNORECASE)
            text = re.sub(r"微信号|手机号|电话联系", "方便接收资料的联系方式", text, flags=re.IGNORECASE)

        if "overpromise" in risks:
            replacements = {
                "保证": "尽量",
                "包过": "按要求协助",
                "绝对": "通常",
                "百分之百": "尽量",
            }
            for source, target in replacements.items():
                text = text.replace(source, target)

        return text.strip()

    def _can_accept_inbound_auto_reply(self) -> tuple[bool, str]:
        """判断当前是否存在有效的自动回复处理能力。"""
        monitoring_status = {}
        try:
            monitoring_status = self._get_monitoring_status()
            if bool(monitoring_status.get("effective_monitoring")):
                if bool(monitoring_status.get("rpa_monitoring")):
                    return True, "bot_service_rpa"
                if bool(monitoring_status.get("traditional_monitoring")):
                    return True, "bot_service_monitor"
                return True, "bot_service_effective"
        except Exception as e:
            logger.debug(f"判断自动回复有效运行态失败: {e}")

        try:
            state = self._get_runtime_state_snapshot()
        except Exception:
            state = {}

        if bool(state.get("is_monitoring_messages")):
            logger.warning("运行态标记仍为监听中，但未检测到有效监听能力，拒绝接收新的自动回复任务")
            return False, "runtime_without_effective_monitor"

        return False, "inactive"

    def _reserve_search_task(self, keyword: str, max_videos: int) -> str:
        """为搜索任务预占执行权，避免任务尚未起跑前重复入队。"""
        with self._runtime_state_lock:
            if self.current_task != "Idle" or self._search_task_pending:
                return ""
            task_id = f"search_{uuid.uuid4().hex[:12]}"
            self._search_task_pending = True
            self._active_search_task_id = task_id
            logger.info(f"预占搜索任务成功: task_id={task_id}, keyword={keyword}, max_videos={max_videos}")
            return task_id

    def _release_search_task(self, task_id: str):
        """释放搜索任务预占状态。"""
        with self._runtime_state_lock:
            if task_id and self._active_search_task_id and task_id != self._active_search_task_id:
                return
            self._search_task_pending = False
            if not task_id or task_id == self._active_search_task_id:
                self._active_search_task_id = ""

    def _normalize_video_queue_priority(self, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in self.VIDEO_QUEUE_PRIORITY_LABELS:
            return CRAWLER_QUEUE_PRIORITY_DEFAULT
        return normalized

    def _get_video_queue_snapshot(
        self,
        *,
        platform: str,
        keyword: str,
        max_videos: int,
        normalized_priority: str,
        skip_existing_videos: bool,
    ) -> tuple[list[dict], dict]:
        status_counts = self.db.get_video_comment_status_counts(platform=platform, search_keyword=keyword)
        queue_items = self.db.get_video_comment_crawl_queue(
            platform=platform,
            limit=max_videos,
            search_keyword=keyword,
            priority=normalized_priority,
            include_completed=not bool(skip_existing_videos),
            stale_in_progress_minutes=CRAWLER_RESUME_STALE_MINUTES,
        )
        return queue_items, status_counts

    def _update_search_queue_summary(
        self,
        *,
        discovery_stats: Dict[str, Any],
        queue_items: list[dict],
        status_counts: Dict[str, int],
        skip_existing_videos: bool,
    ) -> None:
        if self.last_search_summary is None:
            return
        self.last_search_summary["videos_inserted"] = int(discovery_stats.get("inserted", 0) or 0)
        self.last_search_summary["videos_existing"] = int(discovery_stats.get("existing", 0) or 0)
        self.last_search_summary["videos_skipped_existing"] = (
            int(discovery_stats.get("existing", 0) or 0)
            if skip_existing_videos
            else 0
        )
        self.last_search_summary["pending_videos"] = int(status_counts.get(DatabaseManager.VIDEO_STATUS_PENDING, 0) or 0)
        self.last_search_summary["in_progress_videos"] = int(status_counts.get(DatabaseManager.VIDEO_STATUS_IN_PROGRESS, 0) or 0)
        self.last_search_summary["completed_videos"] = int(status_counts.get(DatabaseManager.VIDEO_STATUS_COMPLETED, 0) or 0)
        self.last_search_summary["failed_videos"] = int(status_counts.get(DatabaseManager.VIDEO_STATUS_FAILED, 0) or 0)
        self.last_search_summary["videos_queued"] = len(queue_items)

    @staticmethod
    def _extract_video_aweme_id(video_meta: Dict[str, Any]) -> str:
        aweme_id = str(video_meta.get("aweme_id", "") or "").strip()
        if aweme_id:
            return aweme_id
        raw_url = str(video_meta.get("video_url", "") or video_meta.get("url", "") or "").strip()
        match = re.search(r"/(?:video|note)/([0-9A-Za-z_-]+)", raw_url)
        return str(match.group(1) if match else "").strip()

    @staticmethod
    def _build_discovered_video_queue_item(video_meta: Dict[str, Any], discovered_order: int) -> Dict[str, Any]:
        author = video_meta.get("author") or {}
        author_name = author.get("nickname", "") if isinstance(author, dict) else ""
        video_url = str(video_meta.get("video_url", "") or video_meta.get("url", "") or "").strip()
        return {
            "aweme_id": BotService._extract_video_aweme_id(video_meta),
            "video_url": video_url,
            "url": video_url,
            "title": str(video_meta.get("title", "") or "").strip(),
            "author_name": str(video_meta.get("author_name", "") or author_name or video_meta.get("author", "") or "").strip(),
            "comment_count": int(video_meta.get("comment_count", 0) or 0),
            "like_count": int(video_meta.get("like_count", 0) or 0),
            "publish_timestamp": int(video_meta.get("publish_timestamp", 0) or 0),
            "_discovered_order": int(discovered_order),
        }

    @staticmethod
    def _sort_discovered_video_queue_items(
        items: List[Dict[str, Any]],
        *,
        normalized_priority: str,
    ) -> List[Dict[str, Any]]:
        def _sort_key(item: Dict[str, Any]):
            publish_ts = int(item.get("publish_timestamp", 0) or 0)
            comment_count = int(item.get("comment_count", 0) or 0)
            like_count = int(item.get("like_count", 0) or 0)
            discovered_order = int(item.get("_discovered_order", 0) or 0)
            if normalized_priority == DatabaseManager.VIDEO_QUEUE_PRIORITY_HOT:
                return (-comment_count, -like_count, -publish_ts, discovered_order)
            if normalized_priority == DatabaseManager.VIDEO_QUEUE_PRIORITY_DISCOVERED:
                return (discovered_order, -publish_ts, -comment_count)
            if normalized_priority == DatabaseManager.VIDEO_QUEUE_PRIORITY_PUBLISH:
                return (-publish_ts, -comment_count, discovered_order)
            return (-publish_ts, discovered_order, -comment_count, -like_count)

        sorted_items = sorted(items, key=_sort_key)
        for item in sorted_items:
            item.pop("_discovered_order", None)
        return sorted_items

    def _discover_videos_until_queue_ready(
        self,
        *,
        search_crawler,
        keyword: str,
        platform: str,
        max_videos: int,
        normalized_priority: str,
        skip_existing_videos: bool,
    ) -> list[dict]:
        discovery_stats: Dict[str, Any] = {
            "inserted": 0,
            "existing": 0,
            "existing_completed": 0,
        }
        if skip_existing_videos:
            status_counts = self.db.get_video_comment_status_counts(platform=platform, search_keyword=keyword)
            self._update_search_queue_summary(
                discovery_stats=discovery_stats,
                queue_items=[],
                status_counts=status_counts,
                skip_existing_videos=skip_existing_videos,
            )
            discovered_videos: List[Dict[str, Any]] = []
            selected_ids = set()
            known_status_map: Dict[str, str] = {}
            for video_meta in search_crawler.search_keyword_stream(keyword, max_results=0):
                if self._get_runtime_state_snapshot()["stop_flag"]:
                    logger.info("收到停止信号，终止搜索任务")
                    break

                url = str(video_meta.get("video_url", "") or video_meta.get("url", "") or "").strip()
                aweme_id = self._extract_video_aweme_id(video_meta)
                if not aweme_id or not url or aweme_id in selected_ids:
                    continue

                self.last_search_summary["videos_discovered"] += 1
                existing_status = known_status_map.get(aweme_id)
                if existing_status is None:
                    existing_status = str(self.db.get_crawled_video_status(platform, aweme_id) or "").strip()
                    known_status_map[aweme_id] = existing_status

                if existing_status:
                    discovery_stats["existing"] += 1
                    if existing_status == DatabaseManager.VIDEO_STATUS_COMPLETED:
                        discovery_stats["existing_completed"] += 1
                    self.progress_info["detail"] = (
                        f"已发现 {self.last_search_summary['videos_discovered']} 个相关视频，"
                        f"跳过已获取 {discovery_stats['existing']} 个，"
                        f"新增待抓取 {len(discovered_videos)} / {max_videos}..."
                    )
                    continue

                discovered_videos.append(video_meta)
                selected_ids.add(aweme_id)
                self.progress_info["detail"] = (
                    f"已发现 {self.last_search_summary['videos_discovered']} 个相关视频，"
                    f"跳过已获取 {discovery_stats['existing']} 个，"
                    f"新增待抓取 {len(discovered_videos)} / {max_videos}..."
                )
                if len(discovered_videos) >= max_videos:
                    logger.info(
                        "已按真实新增视频数补足目标数量，停止继续发现: "
                        f"keyword={keyword}, discovered={self.last_search_summary['videos_discovered']}, "
                        f"skipped_existing={discovery_stats['existing']}, "
                        f"queued_new={len(discovered_videos)}/{max_videos}"
                    )
                    break

            if discovered_videos:
                insert_stats = self.db.upsert_crawled_videos(platform, discovered_videos, search_keyword=keyword)
                discovery_stats["inserted"] = int(insert_stats.get("inserted", 0) or 0)
            queue_items = self._sort_discovered_video_queue_items(
                [
                    self._build_discovered_video_queue_item(video_meta, order)
                    for order, video_meta in enumerate(discovered_videos)
                ],
                normalized_priority=normalized_priority,
            )
            status_counts = self.db.get_video_comment_status_counts(platform=platform, search_keyword=keyword)
            self._update_search_queue_summary(
                discovery_stats=discovery_stats,
                queue_items=queue_items,
                status_counts=status_counts,
                skip_existing_videos=skip_existing_videos,
            )
            if not queue_items:
                self.progress_info["detail"] = (
                    f"已筛查 {self.last_search_summary['videos_discovered']} 个相关视频，"
                    "未发现新的待抓取视频。"
                )
            return queue_items

        queue_items, status_counts = self._get_video_queue_snapshot(
            platform=platform,
            keyword=keyword,
            max_videos=max_videos,
            normalized_priority=normalized_priority,
            skip_existing_videos=skip_existing_videos,
        )
        self._update_search_queue_summary(
            discovery_stats=discovery_stats,
            queue_items=queue_items,
            status_counts=status_counts,
            skip_existing_videos=skip_existing_videos,
        )
        if skip_existing_videos and len(queue_items) >= max_videos:
            self.progress_info["detail"] = (
                f"已存在 {len(queue_items)} 个待处理视频，直接复用并开始抓取评论..."
            )
            return queue_items

        discovered_videos: List[Dict[str, Any]] = []
        search_limit = 0 if skip_existing_videos else max_videos
        for video_meta in search_crawler.search_keyword_stream(keyword, max_results=search_limit):
            if self._get_runtime_state_snapshot()["stop_flag"]:
                logger.info("收到停止信号，终止搜索任务")
                break

            url = video_meta.get("url", "")
            if not url:
                continue

            discovered_videos.append(video_meta)
            self.last_search_summary["videos_discovered"] += 1
            if skip_existing_videos:
                batch_stats = self.db.upsert_crawled_videos(platform, [video_meta], search_keyword=keyword)
                for key in ("inserted", "existing", "existing_completed"):
                    discovery_stats[key] += int(batch_stats.get(key, 0) or 0)
                queue_items, status_counts = self._get_video_queue_snapshot(
                    platform=platform,
                    keyword=keyword,
                    max_videos=max_videos,
                    normalized_priority=normalized_priority,
                    skip_existing_videos=skip_existing_videos,
                )
                self._update_search_queue_summary(
                    discovery_stats=discovery_stats,
                    queue_items=queue_items,
                    status_counts=status_counts,
                    skip_existing_videos=skip_existing_videos,
                )
                self.progress_info["detail"] = (
                    f"已发现 {self.last_search_summary['videos_discovered']} 个相关视频，"
                    f"待抓取队列 {len(queue_items)} / {max_videos}..."
                )
                if len(queue_items) >= max_videos:
                    logger.info(
                        "待抓取视频已补足目标数量，停止继续发现: "
                        f"keyword={keyword}, discovered={self.last_search_summary['videos_discovered']}, "
                        f"queue={len(queue_items)}/{max_videos}"
                    )
                    break
            else:
                self.progress_info["detail"] = (
                    f"已发现 {self.last_search_summary['videos_discovered']} / {max_videos} 个相关视频，"
                    "正在整理待爬取队列..."
                )

        if not skip_existing_videos:
            discovery_stats = self.db.upsert_crawled_videos(platform, discovered_videos, search_keyword=keyword)

        queue_items, status_counts = self._get_video_queue_snapshot(
            platform=platform,
            keyword=keyword,
            max_videos=max_videos,
            normalized_priority=normalized_priority,
            skip_existing_videos=skip_existing_videos,
        )
        self._update_search_queue_summary(
            discovery_stats=discovery_stats,
            queue_items=queue_items,
            status_counts=status_counts,
            skip_existing_videos=skip_existing_videos,
        )
        return queue_items

    @staticmethod
    def _resolve_video_crawl_status(crawl_result: Dict[str, Any]) -> tuple[str, str]:
        termination_reason = str(crawl_result.get("termination_reason", "") or "").strip()
        risk_reason = str(crawl_result.get("risk_control_reason", "") or "").strip()
        completeness_warning = str(crawl_result.get("completeness_warning", "") or "").strip()
        if termination_reason == "stop_requested":
            return DatabaseManager.VIDEO_STATUS_PENDING, "stop_requested"
        if crawl_result.get("risk_control_detected"):
            return DatabaseManager.VIDEO_STATUS_FAILED, risk_reason or completeness_warning or termination_reason or "risk_control_detected"
        if crawl_result.get("reached_comment_end"):
            return DatabaseManager.VIDEO_STATUS_COMPLETED, ""
        return DatabaseManager.VIDEO_STATUS_FAILED, completeness_warning or termination_reason or "crawl_not_completed"

    @staticmethod
    def _parse_iso_datetime(value: str):
        """安全解析 ISO 时间字符串。"""
        from datetime import datetime

        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    def get_agent_status(self):
        """获取双Agent进程状态"""
        state = self._get_runtime_state_snapshot()
        monitoring_status = self._get_monitoring_status()
        effective_monitoring = bool(monitoring_status["effective_monitoring"])
        monitor_source = "bot_service" if effective_monitoring else "none"
        crawl_agent_status = {
            "running": False,
            "task": None,
            "last_activity": None
        }
        monitor_agent_status = {
            "running": effective_monitoring,
            "task": monitor_source if effective_monitoring else None,
            "last_activity": None
        }
        if state["is_running"]:
            crawl_agent_status["running"] = True
            crawl_agent_status["task"] = state["current_task"]
        if hasattr(self, '_last_message_time') and self._last_message_time:
            monitor_agent_status["last_activity"] = self._last_message_time
        return {
            "crawl_agent": crawl_agent_status,
            "monitor_agent": monitor_agent_status,
            "agent_mode": getattr(self, "_agent_mode", False),
            "browser_state": state["browser_state"],
            "monitoring_state": state["monitoring_state"],
            "monitor_source": monitor_source,
            "effective_monitoring": effective_monitoring,
        }

    def get_workflow_runs(self, statuses: Optional[List[str]] = None, limit: int = 100) -> List[dict]:
        """获取消息工作流运行列表。"""
        return self.message_workflow_manager.list_runs(statuses=statuses, limit=limit)

    def get_workflow_run(self, workflow_run_id: str) -> Optional[dict]:
        """获取单个消息工作流运行详情。"""
        return self.message_workflow_manager.get_run(workflow_run_id)

    def get_outbox_events(self, statuses: Optional[List[str]] = None, limit: int = 100) -> List[dict]:
        """获取出站事件列表。"""
        return self.db.list_outbox_events(statuses=statuses, limit=limit)

    def get_handoff_tickets(
        self,
        statuses: Optional[List[str]] = None,
        owner: str = "",
        limit: int = 100,
    ) -> List[dict]:
        try:
            self.message_workflow_manager.reconcile_paused_handoff_tickets(
                limit=max(int(limit or 0), 1) * 4
            )
        except Exception as exc:
            logger.debug(f"补齐 paused workflow handoff tickets 失败: {exc}")
        return get_handoff_ticket_repository().list_tickets(statuses=statuses, owner=owner, limit=limit)

    def claim_handoff_ticket(self, ticket_id: str, owner: str) -> bool:
        return get_handoff_ticket_repository().claim_ticket(ticket_id, owner)

    def resolve_handoff_ticket(self, ticket_id: str, approved_reply: str, resolution_note: str = "") -> bool:
        repository = get_handoff_ticket_repository()
        ticket = repository.get_ticket(ticket_id) or {}
        if not repository.resolve_ticket(ticket_id, approved_reply, resolution_note=resolution_note):
            return False
        workflow_run_id = str(ticket.get("workflow_run_id", "") or "").strip()
        if workflow_run_id:
            self.message_workflow_manager.resume_human_run(
                workflow_run_id,
                approved_reply=approved_reply,
                ticket_id=ticket_id,
            )
        return True

    def _schedule_outbox_retry(
        self,
        *,
        outbox_id: str,
        workflow_run_id: str = "",
        retry_count: int,
        reason: str,
        delay_seconds: Optional[int] = None,
    ) -> str:
        """统一调度出站补偿重试。"""
        from datetime import datetime, timedelta

        retry_count = max(int(retry_count or 0), 1)
        delay = delay_seconds if delay_seconds is not None else min(30 * retry_count, 300)
        next_retry_at = (datetime.now() + timedelta(seconds=delay)).isoformat()

        if workflow_run_id:
            return self.message_workflow_manager.schedule_outbox_retry(
                workflow_run_id=workflow_run_id,
                outbox_id=outbox_id,
                retry_count=retry_count,
                reason=reason,
                delay_seconds=delay,
            )

        self._get_inbound_message_state_repository().update_outbox_event(
            outbox_id,
            status="retry_pending",
            retry_count=retry_count,
            next_retry_at=next_retry_at,
            reason=reason,
        )
        return next_retry_at

    def _build_retry_timer_key(
        self,
        *,
        outbox_id: str = "",
        workflow_run_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
    ) -> str:
        """为延迟重试任务生成稳定键，便于去重和取消。"""
        for prefix, value in (
            ("outbox", outbox_id),
            ("workflow", workflow_run_id),
            ("logical", logical_message_id),
            ("source", source_message_id),
        ):
            normalized = str(value or "").strip()
            if normalized:
                return f"{prefix}:{normalized}"
        return ""

    def _resolve_retry_timer_key_from_message(self, message: Optional[dict]) -> str:
        payload = message or {}
        return self._build_retry_timer_key(
            outbox_id=str(payload.get("_reply_outbox_id", "") or payload.get("reply_outbox_id", "") or payload.get("outbox_id", "")).strip(),
            workflow_run_id=str(payload.get("workflow_run_id", "") or payload.get("_workflow_run_id", "")).strip(),
            logical_message_id=str(payload.get("logical_message_id", "") or "").strip(),
            source_message_id=str(payload.get("msg_id", "") or payload.get("message_id", "")).strip(),
        )

    def _register_pending_retry_timer(self, key: str, timer: Any) -> None:
        if not key:
            return
        pending_timers = getattr(self, "_pending_retry_timers", None)
        if pending_timers is None:
            pending_timers = {}
            self._pending_retry_timers = pending_timers
        pending_lock = getattr(self, "_pending_retry_timers_lock", None)
        if pending_lock is None:
            pending_lock = threading.Lock()
            self._pending_retry_timers_lock = pending_lock
        with pending_lock:
            previous = pending_timers.get(key)
            pending_timers[key] = timer
        if previous and previous is not timer:
            with contextlib.suppress(Exception):
                previous.cancel()

    def _pop_pending_retry_timer(self, key: str) -> Optional[threading.Timer]:
        if not key:
            return None
        pending_timers = getattr(self, "_pending_retry_timers", None)
        if pending_timers is None:
            return None
        pending_lock = getattr(self, "_pending_retry_timers_lock", None)
        if pending_lock is None:
            return pending_timers.pop(key, None)
        with pending_lock:
            return pending_timers.pop(key, None)

    def _has_pending_retry_timer(self, key: str) -> bool:
        if not key:
            return False
        pending_timers = getattr(self, "_pending_retry_timers", None)
        if not pending_timers:
            return False
        pending_lock = getattr(self, "_pending_retry_timers_lock", None)
        if pending_lock is None:
            return key in pending_timers
        with pending_lock:
            return key in pending_timers

    def _cancel_pending_retry_timer(
        self,
        *,
        outbox_id: str = "",
        workflow_run_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
    ) -> bool:
        key = self._build_retry_timer_key(
            outbox_id=outbox_id,
            workflow_run_id=workflow_run_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
        )
        timer = self._pop_pending_retry_timer(key)
        if not timer:
            return False
        with contextlib.suppress(Exception):
            timer.cancel()
        return True

    def _cancel_all_pending_retry_timers(self) -> int:
        pending_timers = getattr(self, "_pending_retry_timers", None)
        if not pending_timers:
            return 0
        pending_lock = getattr(self, "_pending_retry_timers_lock", None)
        if pending_lock is None:
            pending = list(pending_timers.values())
            pending_timers.clear()
        else:
            with pending_lock:
                pending = list(pending_timers.values())
                pending_timers.clear()
        for timer in pending:
            with contextlib.suppress(Exception):
                timer.cancel()
        return len(pending)

    @staticmethod
    def _allow_background_delivery_without_new_inbound(_trigger: str = "") -> bool:
        """统一约束后台恢复/重试是否允许在没有新消息时主动发送。"""
        return False

    def _freeze_auto_delivery_work_item(
        self,
        *,
        outbox_id: str = "",
        workflow_run_id: str = "",
        reason: str = "",
        suggested_reply: str = "",
    ) -> None:
        freeze_reason = str(reason or "await_new_inbound").strip() or "await_new_inbound"
        self._cancel_pending_retry_timer(outbox_id=outbox_id, workflow_run_id=workflow_run_id)
        if outbox_id:
            with contextlib.suppress(Exception):
                self.db.update_outbox_event(
                    outbox_id,
                    status="cancelled",
                    reason=freeze_reason[:100],
                    next_retry_at="",
                    workflow_run_id=workflow_run_id,
                )
        if workflow_run_id:
            with contextlib.suppress(Exception):
                self.message_workflow_manager.pause_for_human(
                    workflow_run_id,
                    reason=freeze_reason[:200],
                    suggested_reply=str(suggested_reply or ""),
                    outbox_id=outbox_id,
                )

    def _can_execute_delayed_retry(self, message: dict) -> tuple[bool, str]:
        monitoring_available, monitor_source = self._can_accept_inbound_auto_reply()
        if not monitoring_available:
            return False, f"monitor_inactive:{monitor_source or 'inactive'}"

        outbox_id = str(
            message.get("_reply_outbox_id", "")
            or message.get("reply_outbox_id", "")
            or message.get("outbox_id", "")
            or ""
        ).strip()
        if outbox_id:
            current_outbox = {}
            with contextlib.suppress(Exception):
                current_outbox = self.db.get_outbox_event(outbox_id) or {}
            current_status = str(current_outbox.get("status", "") or "").strip().lower()
            if current_status in {"cancelled", "sent", "delivered", "skipped_duplicate", "failed"}:
                return False, f"outbox_finalized:{current_status}"

        workflow_run_id = str(message.get("workflow_run_id", "") or message.get("_workflow_run_id", "") or "").strip()
        if workflow_run_id:
            workflow_run = {}
            with contextlib.suppress(Exception):
                workflow_run = self.message_workflow_manager.get_run(workflow_run_id) or {}
            workflow_status = str(workflow_run.get("status", "") or "").strip().lower()
            if workflow_status in {"paused", "completed", "failed"}:
                return False, f"workflow_blocked:{workflow_status}"

        return True, "ok"

    def _execute_delayed_retry_if_allowed(self, message: dict, _retry_depth: int = 0) -> bool:
        retry_key = self._resolve_retry_timer_key_from_message(message)
        self._pop_pending_retry_timer(retry_key)

        if not self._allow_background_delivery_without_new_inbound("cooling_retry"):
            outbox_id = str(
                message.get("_reply_outbox_id", "")
                or message.get("reply_outbox_id", "")
                or message.get("outbox_id", "")
                or ""
            ).strip()
            workflow_run_id = str(
                message.get("workflow_run_id", "")
                or message.get("_workflow_run_id", "")
                or ""
            ).strip()
            self._freeze_auto_delivery_work_item(
                outbox_id=outbox_id,
                workflow_run_id=workflow_run_id,
                reason="await_new_inbound:cooling_retry",
                suggested_reply=str(message.get("_cached_reply_content", "") or ""),
            )
            logger.info(
                f"延迟重试已冻结: customer={message.get('customer_name', '')} "
                f"conversation_id={message.get('conversation_id', '') or '-'} "
                f"outbox_id={outbox_id or '-'} reason=await_new_inbound:cooling_retry"
            )
            return False

        allowed, reason = self._can_execute_delayed_retry(message)
        if not allowed:
            logger.info(
                f"延迟重试已跳过: customer={message.get('customer_name', '')} "
                f"conversation_id={message.get('conversation_id', '') or '-'} "
                f"outbox_id={message.get('_reply_outbox_id', '') or message.get('reply_outbox_id', '') or message.get('outbox_id', '') or '-'} "
                f"reason={reason}"
            )
            return False

        self._execute_inbound_reply_job(message, _retry_depth=_retry_depth)
        return True

    def _execute_persisted_workflow_retry_if_allowed(self, *, workflow_run_id: str, outbox_id: str) -> bool:
        retry_key = self._build_retry_timer_key(outbox_id=outbox_id, workflow_run_id=workflow_run_id)
        self._pop_pending_retry_timer(retry_key)

        if not self._allow_background_delivery_without_new_inbound("outbox_retry"):
            outbox_event = self.db.get_outbox_event(outbox_id) or {}
            self._freeze_auto_delivery_work_item(
                outbox_id=outbox_id,
                workflow_run_id=workflow_run_id,
                reason="await_new_inbound:outbox_retry",
                suggested_reply=str(outbox_event.get("reply_content", "") or ""),
            )
            logger.info(
                f"持久化补偿重试已冻结: workflow_run_id={workflow_run_id or '-'} "
                f"outbox_id={outbox_id or '-'} reason=await_new_inbound:outbox_retry"
            )
            return False

        monitoring_available, monitor_source = self._can_accept_inbound_auto_reply()
        if not monitoring_available:
            logger.info(
                f"持久化补偿重试已跳过: workflow_run_id={workflow_run_id or '-'} "
                f"outbox_id={outbox_id or '-'} reason=monitor_inactive:{monitor_source or 'inactive'}"
            )
            return False

        workflow_run = self.message_workflow_manager.get_run(workflow_run_id) or {}
        outbox_event = self.db.get_outbox_event(outbox_id) or {}
        workflow_status = str(workflow_run.get("status", "") or "").strip().lower()
        outbox_status = str(outbox_event.get("status", "") or "").strip().lower()
        if workflow_status != "waiting_retry":
            logger.info(
                f"持久化补偿重试已跳过: workflow_run_id={workflow_run_id or '-'} "
                f"outbox_id={outbox_id or '-'} reason=workflow_status:{workflow_status or '-'}"
            )
            return False
        if outbox_status != "retry_pending":
            logger.info(
                f"持久化补偿重试已跳过: workflow_run_id={workflow_run_id or '-'} "
                f"outbox_id={outbox_id or '-'} reason=outbox_status:{outbox_status or '-'}"
            )
            return False

        reply_content = str(outbox_event.get("reply_content", "") or "").strip()
        customer_name = str(workflow_run.get("customer_name", "") or outbox_event.get("customer_name", "") or "").strip()
        conversation_id = str(workflow_run.get("conversation_id", "") or outbox_event.get("conversation_id", "") or "").strip()
        if not reply_content or not customer_name:
            logger.info(
                f"持久化补偿重试已跳过: workflow_run_id={workflow_run_id or '-'} "
                f"outbox_id={outbox_id or '-'} reason=missing_payload"
            )
            return False

        result = self._deliver_workflow_reply(
            workflow_run_id=workflow_run_id,
            inbound_logical_message_id=str(workflow_run.get("logical_message_id", "") or ""),
            conversation_id=conversation_id,
            customer_name=customer_name,
            reply_content=reply_content,
            customer_id=str(workflow_run.get("customer_id", "") or ""),
            platform=str(workflow_run.get("platform", "douyin") or outbox_event.get("platform", "douyin") or "douyin"),
            original_content=str(workflow_run.get("content", "") or ""),
            outbox_id=outbox_id,
            logical_message_id=str(outbox_event.get("logical_message_id", "") or ""),
            reply_msg_id=str(outbox_event.get("source_message_id", "") or ""),
            trigger="outbox_retry",
            retry_count=max(int(outbox_event.get("retry_count", 0) or 0), 0),
        )
        return bool(result.get("success"))

    def _resume_persisted_retry_workflows(self, limit: int = 20) -> int:
        """恢复持久化的 waiting_retry/retry_pending 任务。"""
        monitoring_status = self._get_monitoring_status()
        if not bool(monitoring_status.get("effective_monitoring")):
            return 0

        try:
            waiting_runs = list(self.db.list_workflow_runs(statuses=["waiting_retry"], limit=max(int(limit or 0), 1) * 4) or [])
        except Exception as e:
            logger.debug(f"读取 waiting_retry 工作流失败: {e}")
            return 0

        restored = 0
        now = datetime.now()
        for run in waiting_runs:
            workflow_run_id = str(run.get("workflow_run_id", "") or "").strip()
            outbox_id = str(run.get("outbox_id", "") or "").strip()
            if not workflow_run_id or not outbox_id:
                continue

            outbox_event = self.db.get_outbox_event(outbox_id) or {}
            if str(outbox_event.get("status", "") or "").strip().lower() != "retry_pending":
                continue
            if not str(outbox_event.get("reply_content", "") or "").strip():
                continue

            if not self._allow_background_delivery_without_new_inbound("outbox_retry"):
                self._freeze_auto_delivery_work_item(
                    outbox_id=outbox_id,
                    workflow_run_id=workflow_run_id,
                    reason="await_new_inbound:outbox_retry",
                    suggested_reply=str(outbox_event.get("reply_content", "") or ""),
                )
                continue

            retry_key = self._build_retry_timer_key(
                outbox_id=outbox_id,
                workflow_run_id=workflow_run_id,
                logical_message_id=str(run.get("logical_message_id", "") or ""),
            )
            if self._has_pending_retry_timer(retry_key):
                continue

            next_retry_at = self._parse_iso_datetime(str(outbox_event.get("next_retry_at", "") or ""))
            delay_seconds = 0.0
            if next_retry_at is not None:
                delay_seconds = max((next_retry_at - now).total_seconds(), 0.0)

            try:
                if delay_seconds > 0.5:
                    timer = threading.Timer(
                        delay_seconds,
                        self._execute_persisted_workflow_retry_if_allowed,
                        kwargs={"workflow_run_id": workflow_run_id, "outbox_id": outbox_id},
                    )
                    timer.daemon = True
                    self._register_pending_retry_timer(retry_key, timer)
                    timer.start()
                else:
                    self._register_pending_retry_timer(retry_key, object())
                    self._reply_executor.submit(
                        self._execute_persisted_workflow_retry_if_allowed,
                        workflow_run_id=workflow_run_id,
                        outbox_id=outbox_id,
                    )
                restored += 1
            except Exception as e:
                self._pop_pending_retry_timer(retry_key)
                logger.warning(
                    f"恢复持久化补偿重试失败: workflow_run_id={workflow_run_id}, "
                    f"outbox_id={outbox_id}, error={e}"
                )
                continue

            if restored >= max(int(limit or 0), 1):
                break

        return restored

    def _finalize_workflow_delivery_success(
        self,
        *,
        workflow_run_id: str,
        inbound_logical_message_id: str,
        outbox_id: str,
        reply_msg_id: str,
        trigger: str,
        deduplicated: bool = False,
        inbound_reason_override: str = "",
        bind_outbox: bool = False,
    ) -> None:
        """统一收口补偿发送成功路径，避免 done/success/complete 多处手写。"""
        self._get_outbound_workflow_delivery_coordinator().finalize_success(
            workflow_run_id=workflow_run_id,
            inbound_logical_message_id=inbound_logical_message_id,
            outbox_id=outbox_id,
            reply_msg_id=reply_msg_id,
            trigger=trigger,
            deduplicated=deduplicated,
            inbound_reason_override=inbound_reason_override,
            bind_outbox=bind_outbox,
        )

    def _pause_workflow_delivery(
        self,
        *,
        workflow_run_id: str,
        outbox_id: str,
        reply_msg_id: str,
        reply_content: str,
        reason: str,
        bind_outbox: bool = False,
    ) -> None:
        """统一收口补偿发送暂停路径，避免 cancelled/pause_for_human 多处手写。"""
        self._get_outbound_workflow_delivery_coordinator().pause_delivery(
            workflow_run_id=workflow_run_id,
            outbox_id=outbox_id,
            reply_msg_id=reply_msg_id,
            reply_content=reply_content,
            reason=reason,
            bind_outbox=bind_outbox,
        )

    def _ensure_outbox_event(
        self,
        *,
        outbox_id: str,
        logical_message_id: str,
        conversation_id: str,
        customer_name: str,
        reply_content: str,
        platform: str,
        source_message_id: str = "",
        outbound_source: str = "",
        outbound_trigger: str = "",
    ) -> None:
        """确保出站事件在任意早退分支前已经存在，避免后续状态更新落空。"""
        if not outbox_id:
            if logical_message_id:
                outbox_id = f"outbox_{logical_message_id}"
            else:
                return
        try:
            self.db.create_outbox_event(
                outbox_id=outbox_id,
                logical_message_id=logical_message_id,
                conversation_id=conversation_id,
                customer_name=customer_name,
                reply_content=reply_content,
                platform=platform,
                source_message_id=source_message_id,
                outbound_source=outbound_source,
                outbound_trigger=outbound_trigger,
            )
        except Exception as outbox_e:
            logger.debug(f"创建出站事件失败: {outbox_e}")

    def _finalize_inbound_event_safe(
        self,
        logical_message_id: str,
        *,
        status: str,
        message_id: str = "",
        source_message_id: str = "",
        reply_message_id: str = "",
        reason: str = "",
    ) -> None:
        """安全更新 inbound 逻辑事件，避免业务链路被状态回写异常打断。"""
        if not logical_message_id:
            return
        try:
            finalize_kwargs = {"status": status, "reason": reason}
            if message_id:
                finalize_kwargs["message_id"] = message_id
            if source_message_id:
                finalize_kwargs["source_message_id"] = source_message_id
            if reply_message_id:
                finalize_kwargs["reply_message_id"] = reply_message_id
            self._get_inbound_message_state_repository().finalize_inbound_event_safe(
                logical_message_id,
                **finalize_kwargs,
            )
        except Exception as finalize_e:
            logger.debug(f"更新入站逻辑事件失败: {finalize_e}")

    def _record_workflow_node_safe(
        self,
        workflow_run_id: str,
        node_id: str,
        *,
        status: str,
        metadata: Optional[dict] = None,
        error: str = "",
    ) -> None:
        """安全记录 workflow 节点，避免编排轨迹异常反噬主业务链。"""
        if not workflow_run_id:
            return
        try:
            self.message_workflow_manager.record_node(
                workflow_run_id,
                node_id,
                status=status,
                metadata=metadata or {},
                error=error,
            )
        except Exception as workflow_e:
            logger.debug(f"记录工作流节点失败: {workflow_e}")

    def _pause_workflow_run_safe(
        self,
        workflow_run_id: str,
        *,
        reason: str,
        suggested_reply: str = "",
        outbox_id: str = "",
        eligibility_snapshot: Optional[dict] = None,
        eligibility_action: str = "",
    ) -> None:
        """安全暂停 workflow，供主回复链和补偿发送链复用。"""
        if not workflow_run_id:
            return
        try:
            pause_kwargs: Dict[str, Any] = {
                "reason": str(reason or "")[:100],
                "suggested_reply": suggested_reply,
            }
            if outbox_id:
                pause_kwargs["outbox_id"] = outbox_id
            if eligibility_snapshot:
                pause_kwargs["eligibility_snapshot"] = eligibility_snapshot
            if eligibility_action:
                pause_kwargs["eligibility_action"] = eligibility_action
            self.message_workflow_manager.pause_for_human(workflow_run_id, **pause_kwargs)
        except Exception as workflow_e:
            logger.debug(f"暂停工作流失败: {workflow_e}")

    def _mark_workflow_skipped_safe(
        self,
        workflow_run_id: str,
        *,
        reason: str,
        metadata: Optional[dict] = None,
        eligibility_action: str = "",
    ) -> None:
        if not workflow_run_id:
            return
        workflow_manager = getattr(self, "message_workflow_manager", None)
        if workflow_manager is None or not hasattr(workflow_manager, "mark_skipped"):
            self._record_workflow_node_safe(
                workflow_run_id,
                "send_reply",
                status="skipped",
                metadata=metadata or self._build_skip_workflow_metadata(
                    reason=str(reason or "").strip(),
                    eligibility_action=str(eligibility_action or "").strip(),
                ),
            )
            return
        try:
            workflow_manager.mark_skipped(
                workflow_run_id,
                reason=str(reason or "").strip(),
                metadata=metadata or {},
                eligibility_action=str(eligibility_action or "").strip(),
            )
        except Exception as workflow_e:
            logger.debug(f"标记工作流 skipped 失败: {workflow_e}")

    def _skip_inbound_pipeline(
        self,
        *,
        customer_name: str,
        content: str,
        msg_id: str,
        logical_message_id: str,
        reason: str,
        workflow_run_id: str = "",
        suggested_reply: str = "",
        pause_workflow: bool = False,
        eligibility_snapshot: Optional[dict] = None,
        eligibility_action: str = "",
    ) -> None:
        """统一收口主回复链的 skipped 分支。"""
        self._mark_message_skipped(
            customer_name,
            content,
            reason,
            msg_id,
            logical_message_id=logical_message_id,
            source_message_id=msg_id,
        )
        self._finalize_inbound_event_safe(
            logical_message_id,
            status="skipped",
            source_message_id=msg_id,
            reason=reason,
        )
        if pause_workflow:
            self._pause_workflow_run_safe(
                workflow_run_id,
                reason=reason,
                suggested_reply=suggested_reply,
                eligibility_snapshot=eligibility_snapshot,
                eligibility_action=eligibility_action,
            )

    def _fail_inbound_pipeline(
        self,
        *,
        customer_name: str,
        content: str,
        msg_id: str,
        logical_message_id: str,
        reason: str,
    ) -> None:
        """统一收口主回复链的 failed 分支。"""
        self._mark_message_failed(
            customer_name,
            content,
            msg_id,
            logical_message_id=logical_message_id,
            source_message_id=msg_id,
        )
        self._finalize_inbound_event_safe(
            logical_message_id,
            status="failed",
            source_message_id=msg_id,
            reason=reason,
        )

    def _prepare_inbound_reply_context(
        self,
        *,
        message: dict,
        customer_name: str,
        content: str,
        conversation_id: str,
        customer_id: str,
        platform: str,
        logical_message_id: str,
        workflow_run_id: str,
    ) -> dict:
        """准备主回复链所需的会话上下文，不直接决定跳过/失败语义。"""
        resolved_customer_id = self._resolve_inbound_customer_id(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )

        resolved_conversation_id = conversation_id or self._resolve_conversation_id(
            customer_name,
            platform,
            resolved_customer_id,
        )
        resolved_workflow_run_id = workflow_run_id
        if logical_message_id and not resolved_workflow_run_id:
            resolved_workflow_run_id = self.message_workflow_manager.start_run(
                logical_message_id=logical_message_id,
                conversation_id=resolved_conversation_id,
                customer_name=customer_name,
                content=content,
                customer_id=resolved_customer_id,
                platform=platform,
                enterprise_id=str(message.get("enterprise_id", "") or "").strip(),
                preferred_schema_id=str(message.get("preferred_schema_id", "") or "").strip(),
                tenant_resolution_mode=str(message.get("tenant_resolution_mode", "") or "").strip(),
            )
        self._record_workflow_node_safe(
            resolved_workflow_run_id,
            "prepare_context",
            status="success",
            metadata={"conversation_id": resolved_conversation_id},
        )

        session = self.session_manager.get_session_by_customer(resolved_customer_id, platform)
        if not session:
            try:
                session = self.session_manager.create_session(
                    resolved_customer_id,
                    platform,
                    {"customer_name": customer_name},
                )
            except Exception as sess_e:
                logger.error(f"创建会话失败: {sess_e}")
        if not session:
            return {
                "customer_id": resolved_customer_id,
                "conversation_id": resolved_conversation_id,
                "workflow_run_id": resolved_workflow_run_id,
                "session": None,
                "conversation_history": [],
                "session_id": "",
            }

        self.session_manager.add_message(session.session_id, "inbound", content)
        self._record_workflow_node_safe(
            resolved_workflow_run_id,
            "load_session",
            status="success",
            metadata={"session_id": session.session_id},
        )

        conversation_history = self._get_chat_store().get_recent_messages_dicts(
            resolved_conversation_id,
            limit=10,
        )
        session_id = resolve_context_session_id(
            platform=platform,
            customer_id=resolved_customer_id,
            provided_session_id=message.get("session_id"),
            conversation_history=conversation_history,
        )
        return {
            "customer_id": resolved_customer_id,
            "conversation_id": resolved_conversation_id,
            "workflow_run_id": resolved_workflow_run_id,
            "session": session,
            "conversation_history": conversation_history,
            "session_id": session_id,
        }

    def _resolve_inbound_customer_id(
        self,
        *,
        customer_name: str,
        conversation_id: str,
        customer_id: str,
        platform: str,
    ) -> str:
        """优先按稳定身份补齐 customer_id，最后才回退昵称。"""
        resolved_customer_id = str(customer_id or "").strip()
        if resolved_customer_id and not resolved_customer_id.startswith("temp_"):
            return resolved_customer_id

        normalized_conversation_id = str(conversation_id or "").strip()
        if normalized_conversation_id:
            conversation = self._get_conversation_snapshot(normalized_conversation_id)
            snapshot_customer_id = str(conversation.get("customer_id", "") or "").strip()
            if snapshot_customer_id:
                return snapshot_customer_id
            try:
                for message in reversed(
                    self._get_chat_store().get_recent_messages_dicts(normalized_conversation_id, limit=20) or []
                ):
                    candidate = str(
                        message.get("customer_id", "")
                        or message.get("sec_uid", "")
                        or ""
                    ).strip()
                    if candidate:
                        return candidate
            except Exception as history_e:
                logger.debug(f"按会话历史补齐 customer_id 失败: {history_e}")

        customer = self.db.get_customer_by_nickname(customer_name, platform)
        if customer:
            resolved_customer_id = str(customer.get("sec_uid", "") or "").strip()
            if resolved_customer_id:
                return resolved_customer_id

        conv_id_seed = normalized_conversation_id if normalized_conversation_id else ""
        id_source = f"{customer_name}_{conv_id_seed}"
        return f"temp_{hashlib.md5(id_source.encode('utf-8')).hexdigest()[:12]}"

    def _generate_inbound_smart_reply(
        self,
        *,
        customer_name: str,
        content: str,
        conversation_id: str,
        customer_id: str,
        platform: str,
        session_id: str,
        conversation_history: list,
        workflow_run_id: str,
        trace_id: str,
        cached_smart_result: Any,
        cached_reply_content: str,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        schema_id: str = "",
        tenant_resolution_mode: str = "",
    ) -> dict:
        """生成或复用主回复链的智能回复结果。"""
        customer_service = get_enhanced_customer_service()
        smart_result = None
        future = None
        llm_started_at = 0.0
        perf_metrics: Dict[str, float] = {}
        total_started_at = time.perf_counter()
        normalized_enterprise_id = str(enterprise_id or "").strip()
        normalized_preferred_schema_id = str(preferred_schema_id or "").strip()
        explicit_schema_id = str(schema_id or "").strip()
        normalized_resolution_mode = str(tenant_resolution_mode or "").strip() or "fail_soft"
        resolved_schema_id = explicit_schema_id
        if not resolved_schema_id:
            try:
                resolved_schema_id = str(
                    get_industry_schema_service().resolve_schema_id(
                        enterprise_id=normalized_enterprise_id,
                        preferred_schema_id=normalized_preferred_schema_id,
                        resolution_mode=normalized_resolution_mode,
                    )
                    or ""
                ).strip()
            except Exception as exc:
                logger.debug(f"自动回复解析 schema_id 失败，保留原始上下文: {exc}")
        if workflow_run_id and resolved_schema_id:
            with contextlib.suppress(Exception):
                self.db.upsert_workflow_run(
                    {
                        "workflow_run_id": workflow_run_id,
                        "schema_id": resolved_schema_id,
                    }
                )

        if isinstance(cached_smart_result, dict) and cached_reply_content:
            smart_result = dict(cached_smart_result)
            smart_result.setdefault("enterprise_id", normalized_enterprise_id)
            smart_result.setdefault("schema_id", resolved_schema_id)
            smart_result.setdefault("tenant_resolution_mode", normalized_resolution_mode)
            smart_result["_reused_generated_reply"] = True
            logger.info(f"[回复主链] reuse_generated trace={trace_id} customer={customer_name} reason=cooling_retry")
            self._record_workflow_node_safe(
                workflow_run_id,
                "generate_reply",
                status="success",
                metadata={"need_human": bool((smart_result or {}).get("need_human", False)), "reused": True},
            )
            return smart_result

        try:
            self._record_workflow_node_safe(
                workflow_run_id,
                "generate_reply",
                status="running",
                metadata={"session_id": session_id},
            )
            if self.circuit_breaker.state.value == "open":
                logger.warning(f"LLM熔断器已打开，停止自动回复并等待主链恢复: {customer_name}")
                get_metrics().record_llm_error("circuit_breaker_open")
                self._update_llm_runtime_metrics()
                smart_result = self._build_deferred_llm_failure_result("circuit_breaker_open")
            else:
                llm_started_at = time.perf_counter()
                stage_started_at = time.perf_counter()
                future, submit_error = self._submit_llm_request(
                    customer_service.process_message,
                    message=content,
                    customer_name=customer_name,
                    conversation_history=conversation_history,
                    customer_data={
                        "sec_uid": customer_id,
                        "nickname": customer_name,
                        "platform": platform,
                        "status": "replied",
                        "conversation_id": conversation_id,
                        "enterprise_id": normalized_enterprise_id,
                        "preferred_schema_id": normalized_preferred_schema_id,
                        "schema_id": resolved_schema_id,
                        "tenant_resolution_mode": normalized_resolution_mode,
                    },
                    use_enhanced=True,
                    session_id=session_id
                )
                self._record_reply_chain_metric(perf_metrics, "submit_llm", stage_started_at)
                if submit_error:
                    logger.warning(f"LLM执行器已饱和，停止自动回复并等待后续重试: {customer_name}")
                    smart_result = self._build_deferred_llm_failure_result(submit_error)
                    self._record_workflow_node_safe(
                        workflow_run_id,
                        "generate_reply",
                        status="degraded",
                        metadata={"fallback_reason": submit_error},
                    )
                elif future is None:
                    smart_result = self._build_deferred_llm_failure_result("llm_unavailable")
                    self._record_workflow_node_safe(
                        workflow_run_id,
                        "generate_reply",
                        status="degraded",
                        metadata={"fallback_reason": "llm_unavailable"},
                    )
                else:
                    stage_started_at = time.perf_counter()
                    smart_result = future.result(timeout=self._llm_request_timeout_seconds)
                    if isinstance(smart_result, dict):
                        smart_result.setdefault("enterprise_id", normalized_enterprise_id)
                        smart_result.setdefault("schema_id", resolved_schema_id)
                        smart_result.setdefault("tenant_resolution_mode", normalized_resolution_mode)
                    self._record_reply_chain_metric(perf_metrics, "wait_llm", stage_started_at)
                    get_metrics().record_llm_latency(time.perf_counter() - llm_started_at)
                    if hasattr(self, 'circuit_breaker') and self.circuit_breaker:
                        try:
                            self.circuit_breaker.record_success()
                        except Exception as cb_e:
                            logger.debug(f"记录 LLM 成功到熔断器失败: {cb_e}")
                    self._update_llm_runtime_metrics()
            self._record_workflow_node_safe(
                workflow_run_id,
                "generate_reply",
                status="success",
                metadata={"need_human": bool((smart_result or {}).get("need_human", False))},
            )
        except concurrent.futures.TimeoutError:
            logger.warning(f"智能回复生成超时({self._llm_request_timeout_seconds}秒)，停止自动回复避免错误知识直出: {customer_name}")
            try:
                if future is not None:
                    future.cancel()
            except Exception:
                pass
            get_metrics().record_llm_error("timeout")
            if llm_started_at:
                get_metrics().record_llm_latency(time.perf_counter() - llm_started_at)
            if hasattr(self, 'circuit_breaker') and self.circuit_breaker:
                try:
                    self.circuit_breaker.record_failure()
                    logger.info(f"LLM超时已记录到熔断器，当前状态: {self.circuit_breaker.state}")
                except Exception:
                    pass
            self._update_llm_runtime_metrics()
            smart_result = self._build_deferred_llm_failure_result("llm_timeout")
            self._record_workflow_node_safe(
                workflow_run_id,
                "generate_reply",
                status="timeout",
                metadata={"fallback_reason": "llm_timeout"},
            )
        except Exception as smart_e:
            logger.error(f"智能回复生成异常: {smart_e}")
            if future is not None:
                future.cancel()
            get_metrics().record_llm_error(type(smart_e).__name__.lower())
            if llm_started_at:
                get_metrics().record_llm_latency(time.perf_counter() - llm_started_at)
            if hasattr(self, 'circuit_breaker') and self.circuit_breaker:
                try:
                    self.circuit_breaker.record_failure()
                except Exception:
                    pass
            self._update_llm_runtime_metrics()
            smart_result = self._build_deferred_llm_failure_result(
                f"exception:{str(smart_e)[:50]}",
                source="llm_generation_failed",
            )
            self._record_workflow_node_safe(
                workflow_run_id,
                "generate_reply",
                status="failed",
                error=str(smart_e)[:200],
            )
        self._record_reply_chain_metric(perf_metrics, "total", total_started_at)
        self._log_reply_chain_perf(
            "generate_reply",
            trace_id,
            perf_metrics,
            customer=customer_name,
            reused=bool(isinstance(cached_smart_result, dict) and cached_reply_content),
            need_human=bool((smart_result or {}).get("need_human", False)),
        )
        return smart_result or self._build_deferred_llm_failure_result("empty_smart_result")

    def _attempt_reply_delivery(
        self,
        *,
        conversation_id: str,
        customer_name: str,
        reply_content: str,
        customer_id: str = "",
        platform: str = "douyin",
        intent_level: str = "E",
        intent_score: float = 0.0,
        smart_result: Optional[dict] = None,
        session=None,
        original_content: str = "",
        outbox_id: str = "",
        logical_message_id: str = "",
        reply_msg_id: str = "",
        allow_duplicate_content: bool = False,
        duplicate_as_success: bool = True,
        cancel_duplicate_reservation: bool = False,
        outbound_source: str = "auto_reply",
        outbound_trigger: str = "reply",
    ) -> OutboundDeliveryAttemptResult:
        """复用出站投递判定与统一发送，调用方自行决定状态收口语义。"""
        return self._get_outbound_delivery_orchestrator().attempt_reply_delivery(
            conversation_id=conversation_id,
            customer_name=customer_name,
            reply_content=reply_content,
            customer_id=customer_id,
            platform=platform,
            intent_level=intent_level,
            intent_score=intent_score,
            smart_result=smart_result,
            session=session,
            original_content=original_content,
            outbox_id=outbox_id,
            logical_message_id=logical_message_id,
            reply_msg_id=reply_msg_id,
            allow_duplicate_content=allow_duplicate_content,
            duplicate_as_success=duplicate_as_success,
            cancel_duplicate_reservation=cancel_duplicate_reservation,
            outbound_source=outbound_source,
            outbound_trigger=outbound_trigger,
        )

    def _handle_inbound_delivery_result(
        self,
        *,
        customer_name: str,
        content: str,
        msg_id: str,
        customer_id: str,
        logical_message_id: str,
        workflow_run_id: str,
        conversation_id: str,
        reply_content: str,
        reply_msg_id: str,
        reply_outbox_id: str,
        trace_id: str,
        delivery_result: dict,
    ) -> bool:
        """收口主回复链的发送结果分支，返回是否继续后续意向更新。"""
        delivery_status = str(delivery_result.get("status", "") or "")
        delivery_channel = str(delivery_result.get("channel", "") or delivery_result.get("delivery_channel", "") or "-")

        if delivery_status == "duplicate":
            logger.info(
                f"跳过重复回复 [{customer_name}]: trace={trace_id or '-'} "
                f"conversation_id={conversation_id or '-'} source_message_id={msg_id or '-'} "
                f"logical_message_id={logical_message_id or '-'} outbox_id={reply_outbox_id or '-'}"
            )
            self._skip_inbound_pipeline(
                customer_name=customer_name,
                content=content,
                msg_id=msg_id,
                logical_message_id=logical_message_id,
                reason="duplicate_reply",
            )
            return False

        if delivery_status == "blocked":
            blocked_reason = str(delivery_result.get("message", "") or "")
            block_category = str(delivery_result.get("block_category", "") or "policy_blocked")
            logger.info(
                f"回复冷却中 [{customer_name}]: trace={trace_id or '-'} "
                f"conversation_id={conversation_id or '-'} source_message_id={msg_id or '-'} "
                f"logical_message_id={logical_message_id or '-'} outbox_id={reply_outbox_id or '-'} "
                f"reason={blocked_reason} block_category={block_category}"
            )
            return False

        self._record_workflow_node_safe(
            workflow_run_id,
            "send_reply",
            status="running",
            metadata={"outbox_id": reply_outbox_id, "workflow_action": "send"},
        )

        if delivery_status == "sent":
            logger.info(
                f"[回复主链] send_done trace={trace_id} customer={customer_name} "
                f"conversation_id={conversation_id} source_message_id={msg_id or '-'} "
                f"logical_message_id={logical_message_id or '-'} outbox_id={reply_outbox_id or '-'} "
                f"reply_msg_id={reply_msg_id} channel={delivery_channel}"
            )
            self._mark_message_done(
                customer_name,
                content,
                msg_id,
                conversation_id=conversation_id,
                customer_id=customer_id,
                logical_message_id=logical_message_id,
                source_message_id=msg_id,
            )
            self._record_workflow_node_safe(
                workflow_run_id,
                "send_reply",
                status="success",
                metadata={
                    "outbox_id": reply_outbox_id,
                    "message_id": reply_msg_id,
                    "reason": "reply_sent",
                    "reason_code": "reply_sent",
                    "workflow_action": "send",
                },
            )
            self._finalize_workflow_delivery_success(
                workflow_run_id=workflow_run_id,
                inbound_logical_message_id=logical_message_id,
                outbox_id=reply_outbox_id,
                reply_msg_id=reply_msg_id,
                trigger="reply",
                inbound_reason_override="reply_sent",
                bind_outbox=True,
            )
            return True

        failure_reason = str(delivery_result.get("failure_reason", "") or self._get_last_send_error("reply_send_failed"))
        pause_reason = str(delivery_result.get("message", "") or f"send_failed:{failure_reason[:80]}")
        logger.warning(
            f"[回复主链] send_failed trace={trace_id} customer={customer_name} "
            f"conversation_id={conversation_id} source_message_id={msg_id or '-'} "
            f"logical_message_id={logical_message_id or '-'} outbox_id={reply_outbox_id or '-'} "
            f"status={delivery_status or '-'} channel={delivery_channel} "
            f"failure={failure_reason[:100]} message={str(delivery_result.get('message', '') or '-')[:100]}"
        )
        self._fail_inbound_pipeline(
            customer_name=customer_name,
            content=content,
            msg_id=msg_id,
            logical_message_id=logical_message_id,
            reason=failure_reason[:100],
        )
        self._record_workflow_node_safe(
            workflow_run_id,
            "send_reply",
            status="failed",
            metadata={
                "outbox_id": reply_outbox_id,
                "failure_reason": failure_reason[:100],
                "reason": failure_reason[:100],
                "reason_code": failure_reason[:100],
                "workflow_action": "fail",
            },
            error=failure_reason[:200],
        )
        state_repository = self._get_inbound_message_state_repository()
        if workflow_run_id:
            current_outbox = self.db.get_outbox_event(reply_outbox_id) or {}
            if delivery_status == "failed_non_retryable":
                self._pause_workflow_delivery(
                    workflow_run_id=workflow_run_id,
                    outbox_id=reply_outbox_id,
                    reply_msg_id=reply_msg_id,
                    reply_content=reply_content,
                    reason=pause_reason,
                    bind_outbox=True,
                )
            else:
                retry_count = int(current_outbox.get("retry_count", 0)) + 1
                if self._has_exhausted_outbox_retries(retry_count):
                    exhausted_reason = f"retry_exhausted:{failure_reason[:80]}"
                    self._pause_workflow_delivery(
                        workflow_run_id=workflow_run_id,
                        outbox_id=reply_outbox_id,
                        reply_msg_id=reply_msg_id,
                        reply_content=reply_content,
                        reason=exhausted_reason,
                        bind_outbox=True,
                    )
                else:
                    state_repository.bind_workflow_outbox(workflow_run_id, reply_outbox_id)
                    self.message_workflow_manager.schedule_outbox_retry(
                        workflow_run_id=workflow_run_id,
                        outbox_id=reply_outbox_id,
                        retry_count=retry_count,
                        reason=failure_reason[:100],
                        delay_seconds=min(30 * retry_count, 300),
                        eligibility_action=EligibilityAction.RETRY_LATER.value,
                    )
        return True

    def _prepare_inbound_reply_candidate(
        self,
        *,
        customer_name: str,
        content: str,
        conversation_id: str,
        customer_id: str = "",
        msg_id: str,
        logical_message_id: str,
        workflow_run_id: str,
        trace_id: str,
        platform: str,
        reply_msg_id: str,
        smart_result: dict,
        cached_reply_content: str,
        history_message_count: int = 0,
    ) -> Optional[dict]:
        """规范化回复候选，并收口主链的前置跳过/人工接管出口。"""
        reply_analysis = smart_result.get("reply_analysis") or {}
        retrieval_state = reply_analysis.get("retrieval") if isinstance(reply_analysis, dict) else {}
        retrieval_state = retrieval_state if isinstance(retrieval_state, dict) else {}
        fallback_state = reply_analysis.get("fallback") if isinstance(reply_analysis, dict) else {}
        fallback_state = fallback_state if isinstance(fallback_state, dict) else {}
        rag_llm_state = reply_analysis.get("rag_llm") if isinstance(reply_analysis, dict) else {}
        rag_llm_state = rag_llm_state if isinstance(rag_llm_state, dict) else {}
        generation_cta = reply_analysis.get("generation_cta") if isinstance(reply_analysis, dict) else {}
        generation_cta = generation_cta if isinstance(generation_cta, dict) else {}
        smart_metadata = smart_result.get("metadata") if isinstance(smart_result.get("metadata"), dict) else {}
        smart_metadata = smart_metadata if isinstance(smart_metadata, dict) else {}
        retrieval_final_action = str(
            smart_result.get("retrieval_final_action")
            or retrieval_state.get("final_action")
            or smart_metadata.get("retrieval_final_action")
            or ""
        ).strip()
        reason_code = str(
            smart_result.get("reason_code")
            or smart_metadata.get("reason_code")
            or ""
        ).strip()
        reply_disposition = str(
            smart_result.get("reply_disposition")
            or smart_metadata.get("reply_disposition")
            or ""
        ).strip().lower()
        reply_content = cached_reply_content or smart_result.get("reply") or ""
        if not isinstance(reply_content, str):
            reply_content = str(reply_content or "")
        intent_level = smart_result.get("intent_level", "E")
        intent_score = smart_result.get("intent_score", 0)
        need_human = smart_result.get("need_human", False)
        matched_knowledge = smart_result.get("matched_knowledge", "")
        eligibility_service = get_reply_eligibility_service()
        logger.info(
            f"[回复主链] llm_done trace={trace_id} customer={customer_name} "
            f"need_human={need_human} intent={intent_level} score={float(intent_score or 0):.2f} "
            f"knowledge={'yes' if matched_knowledge else 'no'} "
            f"source={smart_result.get('source', 'unknown')} "
            f"retrieval_final_action={retrieval_final_action or '-'} "
            f"retrieval_final_reason={str(retrieval_state.get('final_reason', '') or '-')[:60]} "
            f"fallback_source={str(fallback_state.get('source', '') or '-')[:40]} "
            f"fallback_reason={str(fallback_state.get('reason', smart_result.get('fallback_reason', '') or '') or '-')[:60]} "
            f"rag_source={str(rag_llm_state.get('source', '') or '-')[:32]} "
            f"reason_code={reason_code or '-'} "
            f"cta_used={bool(generation_cta.get('used', False))} "
            f"cta_contact_phrase={bool(generation_cta.get('contains_contact_phrase', False))} "
            f"cta_text={str(generation_cta.get('cta_text', '') or '-')[:80]} "
            f"reply_len={len(reply_content)} reply_preview={reply_content[:120]}"
        )
        if not intent_level or intent_level not in ("A", "B", "C", "D", "E"):
            intent_level = "E"

        runtime_flags = self._build_default_reply_runtime_flags(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
            smart_result=smart_result,
            reply_content=reply_content,
            history_message_count=history_message_count,
        )

        initial_decision = eligibility_service.evaluate(
            ReplyEligibilityInput(
                customer_name=customer_name,
                content=content,
                conversation_id=conversation_id,
                customer_id=customer_id,
                logical_message_id=logical_message_id,
                workflow_run_id=workflow_run_id,
                platform=platform,
                smart_result=smart_result,
                cached_reply_content=cached_reply_content,
                history_message_count=history_message_count,
                schema_policy=((smart_result.get("reply_policy") or {}).get("eligibility_thresholds") or {}),
                runtime_flags=runtime_flags,
            )
        )
        # #region debug-point reply-eligibility-initial
        try:
            _dbg_path = __import__("os").path.join(__import__("os").getcwd(), ".dbg", "reply-no-response.env")
            _dbg_url = "http://127.0.0.1:7777/event"
            _dbg_session = "reply-no-response"
            try:
                with open(_dbg_path, encoding="utf-8") as _dbg_file:
                    for _dbg_line in _dbg_file:
                        if _dbg_line.startswith("DEBUG_SERVER_URL="):
                            _dbg_url = _dbg_line.split("=", 1)[1].strip() or _dbg_url
                        elif _dbg_line.startswith("DEBUG_SESSION_ID="):
                            _dbg_session = _dbg_line.split("=", 1)[1].strip() or _dbg_session
            except Exception:
                pass
            __import__("urllib.request").request.urlopen(
                __import__("urllib.request").request.Request(
                    _dbg_url,
                    data=__import__("json").dumps(
                        {
                            "sessionId": _dbg_session,
                            "runId": "pre-fix",
                            "hypothesisId": "H4",
                            "location": "src/web/bot_service.py:_prepare_inbound_reply_candidate",
                            "msg": "[DEBUG] reply eligibility initial decision",
                            "data": {
                                "trace_id": trace_id,
                                "customer_name": customer_name,
                                "action": str(initial_decision.action or ""),
                                "reason_code": str(initial_decision.reason_code or ""),
                                "should_send": bool(initial_decision.should_send),
                                "confidence": float(getattr(initial_decision, "confidence", 0.0) or 0.0),
                                "evidence_level": str(getattr(initial_decision, "evidence_level", "") or ""),
                                "risk_level": str(getattr(initial_decision, "risk_level", "") or ""),
                                "reply_len": len(reply_content),
                                "intent_score": float(intent_score or 0.0),
                                "identity_level": str(
                                    ((getattr(initial_decision, "normalized_metadata", None) or {}).get("identity_level", ""))
                                    or ""
                                ),
                                "stable_observation_count": int(
                                    ((getattr(initial_decision, "normalized_metadata", None) or {}).get("stable_observation_count", 0))
                                    or 0
                                ),
                            },
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=0.5,
            ).read()
        except Exception:
            pass
        # #endregion
        reply_content = self._apply_reply_eligibility_decision_content(
            decision=initial_decision,
            reply_content=reply_content,
            smart_result=smart_result,
        )
        if initial_decision.action in {
            EligibilityAction.SKIP.value,
            EligibilityAction.RETRY_LATER.value,
            EligibilityAction.DRAFT_FOR_REVIEW.value,
        } and not reply_content.strip():
            skip_reason = initial_decision.reason_code or (
                "reply_deferred"
                if initial_decision.action in {
                    EligibilityAction.RETRY_LATER.value,
                    EligibilityAction.DRAFT_FOR_REVIEW.value,
                }
                else "reply_skipped"
            )
            self._skip_inbound_pipeline(
                customer_name=customer_name,
                content=content,
                msg_id=msg_id,
                logical_message_id=logical_message_id,
                reason=skip_reason,
            )
            if workflow_run_id:
                self._mark_workflow_skipped_safe(
                    workflow_run_id,
                    reason=skip_reason,
                    metadata=self._build_skip_workflow_metadata(
                        reason=skip_reason,
                        reply_disposition=reply_disposition,
                        eligibility_action=(
                            initial_decision.action
                            if initial_decision.action == EligibilityAction.DRAFT_FOR_REVIEW.value
                            else ""
                        ),
                    ),
                    eligibility_action=(
                        initial_decision.action
                        if initial_decision.action == EligibilityAction.DRAFT_FOR_REVIEW.value
                        else ""
                    ),
                )
            return None

        reply_content = self._sanitize_reply_content(reply_content.strip())
        if not reply_content:
            logger.warning(f"回复内容被垃圾过滤器拦截，跳过发送: {customer_name}")
            self._skip_inbound_pipeline(
                customer_name=customer_name,
                content=content,
                msg_id=msg_id,
                logical_message_id=logical_message_id,
                reason="spam_filtered",
            )
            self._mark_workflow_skipped_safe(
                workflow_run_id,
                reason="spam_filtered",
                metadata=self._build_skip_workflow_metadata(reason="spam_filtered"),
            )
            return None

        claim_categories = self._detect_unverified_claim_categories(reply_content)
        has_grounded_evidence = self._has_grounded_reply_evidence(smart_result)
        reused_generated_reply = bool((smart_result or {}).get("_reused_generated_reply", False))
        if claim_categories and not has_grounded_evidence and reused_generated_reply:
            logger.info(
                f"[回复主链] fact_gate_reused_reply_passthrough trace={trace_id} "
                f"customer={customer_name} categories={','.join(claim_categories)}"
            )
            claim_categories = []
        if claim_categories and not has_grounded_evidence:
            source = str((smart_result or {}).get("source", "") or "").strip().lower()
            is_llm_fallback = source == "llm生成"
            if is_llm_fallback:
                non_promise = [c for c in claim_categories if c != "promise"]
                if not non_promise:
                    softened = self._rewrite_reply_content_by_risks(reply_content, ["overpromise"])
                    if softened and softened != reply_content:
                        logger.info(
                            f"[回复主链] fact_gate_llm_promise_softened trace={trace_id} "
                            f"customer={customer_name} source={source}"
                        )
                        reply_content = softened
                        smart_result["reply"] = reply_content
                        claim_categories = []
                else:
                    logger.info(
                        f"[回复主链] fact_gate_llm_fallback_passthrough trace={trace_id} "
                        f"customer={customer_name} categories={','.join(claim_categories)} source={source}"
                    )
                    if "promise" in claim_categories:
                        softened = self._rewrite_reply_content_by_risks(reply_content, ["overpromise"])
                        if softened and softened != reply_content:
                            reply_content = softened
                            smart_result["reply"] = reply_content
                    claim_categories = []

            if claim_categories and not has_grounded_evidence:
                logger.warning(
                    f"[回复主链] fact_gate_blocked trace={trace_id} customer={customer_name} "
                    f"categories={','.join(claim_categories)} source={smart_result.get('source', 'unknown')}"
                )
                fact_gated_result = self._build_knowledge_only_fallback_result(
                    customer_name,
                    content,
                    "fact_gate_unverified_claim",
                    source="fact_gate_knowledge_only",
                    enterprise_id=str((smart_result or {}).get("enterprise_id", "") or "").strip(),
                    schema_id=str((smart_result or {}).get("schema_id", "") or "").strip(),
                )
                reply_content = str(fact_gated_result.get("reply", "") or "").strip()
                intent_level = fact_gated_result.get("intent_level", intent_level)
                intent_score = fact_gated_result.get("intent_score", intent_score)
                need_human = fact_gated_result.get("need_human", need_human)
                matched_knowledge = fact_gated_result.get("matched_knowledge", matched_knowledge)
                smart_result = {
                    **smart_result,
                    **fact_gated_result,
                    "reply": reply_content,
                    "source": "fact_gate",
                    "fact_gate_categories": claim_categories,
                    "fact_gate_original_source": smart_result.get("source", "unknown"),
                }
                reply_disposition = str(
                    smart_result.get("reply_disposition")
                    or ""
                ).strip().lower()
                reason_code = str(
                    smart_result.get("reason_code")
                    or reason_code
                    or ""
                ).strip()
        final_decision = eligibility_service.evaluate(
            ReplyEligibilityInput(
                customer_name=customer_name,
                content=content,
                conversation_id=conversation_id,
                customer_id=customer_id,
                logical_message_id=logical_message_id,
                workflow_run_id=workflow_run_id,
                platform=platform,
                smart_result={**smart_result, "reply": reply_content},
                cached_reply_content=reply_content,
                history_message_count=history_message_count,
                schema_policy=((smart_result.get("reply_policy") or {}).get("eligibility_thresholds") or {}),
                runtime_flags=self._build_default_reply_runtime_flags(
                    customer_name=customer_name,
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    platform=platform,
                    smart_result={**smart_result, "reply": reply_content},
                    reply_content=reply_content,
                    history_message_count=history_message_count,
                ),
            )
        )
        # #region debug-point reply-eligibility-final
        try:
            _dbg_path = __import__("os").path.join(__import__("os").getcwd(), ".dbg", "reply-no-response.env")
            _dbg_url = "http://127.0.0.1:7777/event"
            _dbg_session = "reply-no-response"
            try:
                with open(_dbg_path, encoding="utf-8") as _dbg_file:
                    for _dbg_line in _dbg_file:
                        if _dbg_line.startswith("DEBUG_SERVER_URL="):
                            _dbg_url = _dbg_line.split("=", 1)[1].strip() or _dbg_url
                        elif _dbg_line.startswith("DEBUG_SESSION_ID="):
                            _dbg_session = _dbg_line.split("=", 1)[1].strip() or _dbg_session
            except Exception:
                pass
            __import__("urllib.request").request.urlopen(
                __import__("urllib.request").request.Request(
                    _dbg_url,
                    data=__import__("json").dumps(
                        {
                            "sessionId": _dbg_session,
                            "runId": "pre-fix",
                            "hypothesisId": "H4",
                            "location": "src/web/bot_service.py:_prepare_inbound_reply_candidate",
                            "msg": "[DEBUG] reply eligibility final decision",
                            "data": {
                                "trace_id": trace_id,
                                "customer_name": customer_name,
                                "action": str(final_decision.action or ""),
                                "reason_code": str(final_decision.reason_code or ""),
                                "should_send": bool(final_decision.should_send),
                                "confidence": float(getattr(final_decision, "confidence", 0.0) or 0.0),
                                "evidence_level": str(getattr(final_decision, "evidence_level", "") or ""),
                                "risk_level": str(getattr(final_decision, "risk_level", "") or ""),
                                "reply_len": len(reply_content),
                                "intent_score": float(intent_score or 0.0),
                                "identity_level": str(
                                    ((getattr(final_decision, "normalized_metadata", None) or {}).get("identity_level", ""))
                                    or ""
                                ),
                                "stable_observation_count": int(
                                    ((getattr(final_decision, "normalized_metadata", None) or {}).get("stable_observation_count", 0))
                                    or 0
                                ),
                            },
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=0.5,
            ).read()
        except Exception:
            pass
        # #endregion
        reply_content = self._apply_reply_eligibility_decision_content(
            decision=final_decision,
            reply_content=reply_content,
            smart_result=smart_result,
        )

        if final_decision.action == EligibilityAction.SKIP.value:
            skip_reason = final_decision.reason_code or "reply_skipped"
            self._skip_inbound_pipeline(
                customer_name=customer_name,
                content=content,
                msg_id=msg_id,
                logical_message_id=logical_message_id,
                reason=skip_reason,
            )
            if workflow_run_id:
                self._mark_workflow_skipped_safe(
                    workflow_run_id,
                    reason=skip_reason,
                    metadata=self._build_skip_workflow_metadata(
                        reason=skip_reason,
                        reply_disposition=reply_disposition,
                    ),
                )
            return None
        if final_decision.action in {EligibilityAction.RETRY_LATER.value, EligibilityAction.DRAFT_FOR_REVIEW.value}:
            # 当 DRAFT_FOR_REVIEW 但已有可用 reply_content 时，保留回复内容继续发送
            # 这样即使置信度低，也不会让客户的消息石沉大海
            if final_decision.action == EligibilityAction.DRAFT_FOR_REVIEW.value and reply_content and reply_content.strip():
                logger.info(
                    f"低置信度但已生成可用回复，按 draft_for_review 降级发送: "
                    f"customer={customer_name}, reason={final_decision.reason_code}, "
                    f"reply_len={len(reply_content)}"
                )
                # 继续往下走，让后续流程使用 reply_content 发送
            else:
                disposition_reason = final_decision.reason_code or "reply_deferred"
                self._skip_inbound_pipeline(
                    customer_name=customer_name,
                    content=content,
                    msg_id=msg_id,
                    logical_message_id=logical_message_id,
                    reason=disposition_reason,
                )
                if workflow_run_id:
                    self._mark_workflow_skipped_safe(
                        workflow_run_id,
                        reason=disposition_reason,
                        metadata=self._build_skip_workflow_metadata(
                            reason=disposition_reason,
                            reply_disposition=reply_disposition,
                            eligibility_action=(
                                final_decision.action
                                if final_decision.action == EligibilityAction.DRAFT_FOR_REVIEW.value
                                else ""
                            ),
                        ),
                        eligibility_action=(
                            final_decision.action
                            if final_decision.action == EligibilityAction.DRAFT_FOR_REVIEW.value
                            else ""
                        ),
                    )
                return None
        if final_decision.action == EligibilityAction.PAUSE_FOR_HUMAN.value:
            self._notify_human_handoff(customer_name, content, reply_content, conversation_id)
            self._skip_inbound_pipeline(
                customer_name=customer_name,
                content=content,
                msg_id=msg_id,
                logical_message_id=logical_message_id,
                reason=final_decision.reason_code or "need_human",
                workflow_run_id=workflow_run_id,
                suggested_reply=reply_content,
                pause_workflow=True,
                eligibility_action=final_decision.action,
            )
            if workflow_run_id:
                self._record_workflow_node_safe(
                    workflow_run_id,
                    "send_reply",
                    status="paused",
                    metadata={
                        "reason": final_decision.reason_code or "need_human",
                        "reason_code": final_decision.reason_code or "need_human",
                        "workflow_action": "pause_for_human",
                        "eligibility_action": final_decision.action,
                        "handoff_level": final_decision.handoff_level,
                    },
                )
            return None

        reply_logical_message_id = build_logical_message_id(
            platform=platform,
            conversation_id=conversation_id,
            customer_name=customer_name,
            direction="outbound",
            content=reply_content,
            source_message_id=reply_msg_id,
        )
        return {
            "reply_content": reply_content,
            "intent_level": intent_level,
            "intent_score": intent_score,
            "reply_logical_message_id": reply_logical_message_id,
            "reply_outbox_id": f"outbox_{reply_logical_message_id}",
        }

    def _build_default_reply_runtime_flags(
        self,
        *,
        customer_name: str,
        conversation_id: str,
        customer_id: str,
        platform: str,
        smart_result: dict,
        reply_content: str,
        history_message_count: int = 0,
    ) -> dict[str, Any]:
        metadata = smart_result.get("metadata") if isinstance(smart_result.get("metadata"), dict) else {}
        eligibility_service = get_reply_eligibility_service()
        policy = eligibility_service.merge_policy(
            ((smart_result.get("reply_policy") or {}).get("eligibility_thresholds") or {}),
            {"allow_no_answer_fail_soft": True},
        )
        normalized_conversation_id = str(conversation_id or "").strip()
        normalized_customer_id = str(customer_id or "").strip()
        normalized_customer_name = str(customer_name or "").strip()
        normalized_reply = str(reply_content or "").strip()
        stable_observation_count = 0
        if metadata:
            stable_observation_count = int(
                metadata.get("stable_observation_count")
                or metadata.get("observation_count")
                or metadata.get("seen_count")
                or 0
            )
        if stable_observation_count <= 0:
            stable_observation_count = 2 if int(history_message_count or 0) > 0 else 1
        recent_reply_cooldown_hit = False
        cooldown_seconds = max(int(getattr(policy, "recent_reply_cooldown_seconds", 0) or 0), 0)
        if normalized_conversation_id and normalized_reply and cooldown_seconds > 0:
            recent_reply_cooldown_hit = self._get_outbound_recent_reply_store().is_recent_duplicate(
                conversation_id=normalized_conversation_id,
                content=normalized_reply,
                platform=platform,
                window_seconds=cooldown_seconds,
            )
        identity_level = "name_fuzzy"
        if normalized_customer_id:
            identity_level = "strict_customer_id"
        elif normalized_conversation_id:
            identity_level = "strict_conversation_id"
        elif normalized_customer_name:
            identity_level = "exact_name"
        return {
            "allow_no_answer_fail_soft": True,
            "identity_level": identity_level,
            "stable_observation_count": stable_observation_count,
            "recent_reply_cooldown_hit": recent_reply_cooldown_hit,
            "preserve_existing_reply": bool((smart_result or {}).get("_reused_generated_reply", False)),
        }

    @staticmethod
    def _apply_reply_eligibility_decision_content(
        *,
        decision: ReplyEligibilityDecision,
        reply_content: str,
        smart_result: dict,
    ) -> str:
        if decision.action == EligibilityAction.ACK_ONLY.value:
            ack_reply = str(decision.final_reply or "").strip()
            if not ack_reply:
                ack_reply = "收到您的消息了，我先帮您确认一下，稍后给您准确回复。"
            smart_result["reply"] = ack_reply
            return ack_reply
        if decision.final_reply:
            normalized_reply = str(decision.final_reply or "").strip()
            smart_result["reply"] = normalized_reply
            return normalized_reply
        return str(reply_content or "").strip()

    @staticmethod
    def _build_skip_workflow_metadata(
        *,
        reason: str,
        reply_disposition: str = "",
        eligibility_action: str = "",
    ) -> dict[str, Any]:
        normalized_reason = str(reason or "").strip()
        metadata: dict[str, Any] = {
            "reason": normalized_reason,
            "reason_code": normalized_reason,
            "workflow_action": "skip",
        }
        normalized_disposition = str(reply_disposition or "").strip().lower()
        normalized_action = str(eligibility_action or "").strip()
        if normalized_disposition:
            metadata["reply_disposition"] = normalized_disposition
        if normalized_action:
            metadata["eligibility_action"] = normalized_action
        return metadata

    def _prepare_inbound_reply_request(self, message: dict) -> Optional[_InboundReplyRequest]:
        """兼容入口：委托给 planner 完成 request 标准化。"""
        return self._get_inbound_reply_planner().prepare_request(message if isinstance(message, dict) else {})

    def _get_inbound_reply_request_deps(self) -> _InboundReplyRequestDeps:
        """装配主回复链 request 阶段能力，便于继续下沉到独立边界。"""
        provider = self._get_inbound_reply_request_provider()
        return _InboundReplyRequestDeps(
            is_message_done=provider.is_message_done,
            is_recently_sent_by_us=provider.is_recently_sent_by_us,
        )

    def _get_inbound_reply_request_provider(self) -> _InboundReplyRequestProvider:
        """装配主回复链 request 阶段 provider，作为更明确的消息判定边界。"""
        return _InboundReplyRequestProvider.from_resolver(
            is_message_done_resolver=lambda: getattr(self, "_is_message_done", None),
            is_recently_sent_by_us_resolver=lambda: getattr(self, "_is_recently_sent_by_us", None),
        )

    def _get_inbound_reply_plan_deps(self) -> _InboundReplyPlanDeps:
        """装配主回复链 plan 阶段能力，便于继续下沉到独立边界。"""
        provider = self._get_inbound_reply_plan_provider()
        return _InboundReplyPlanDeps(
            prepare_inbound_reply_context=provider.prepare_inbound_reply_context,
            generate_inbound_smart_reply=provider.generate_inbound_smart_reply,
            prepare_inbound_reply_candidate=provider.prepare_inbound_reply_candidate,
        )

    def _get_inbound_reply_plan_provider(self) -> _InboundReplyPlanProvider:
        """装配主回复链生成阶段 provider，作为更明确的 ReplyGenerator 边界。"""
        return _InboundReplyPlanProvider.from_resolver(
            prepare_inbound_reply_context_resolver=lambda: getattr(self, "_prepare_inbound_reply_context", None),
            generate_inbound_smart_reply_resolver=lambda: getattr(self, "_generate_inbound_smart_reply", None),
            prepare_inbound_reply_candidate_resolver=lambda: getattr(self, "_prepare_inbound_reply_candidate", None),
        )

    def _get_inbound_reply_generator(self) -> _ReplyGenerator:
        """装配主回复链生成协作者，收口上下文准备、生成与候选构造能力。"""
        return _ReplyGenerator.from_plan_deps(self._get_inbound_reply_plan_deps())

    def _get_inbound_reply_execution_deps(self) -> _InboundReplyExecutionDeps:
        """装配主回复链 execution 阶段能力，便于继续下沉到独立边界。"""
        provider = self._get_inbound_reply_execution_provider()
        return _InboundReplyExecutionDeps(
            attempt_reply_delivery=provider.attempt_reply_delivery,
            handle_inbound_cooling_retry=provider.handle_inbound_cooling_retry,
            handle_inbound_delivery_result=provider.handle_inbound_delivery_result,
        )

    def _get_inbound_reply_execution_provider(self) -> _InboundReplyExecutionProvider:
        """装配主回复链执行阶段 provider，作为更明确的 ReplySender 边界。"""
        return _InboundReplyExecutionProvider.from_resolver(
            attempt_reply_delivery_resolver=lambda: getattr(self, "_attempt_reply_delivery", None),
            handle_inbound_cooling_retry_resolver=lambda: getattr(self, "_handle_inbound_cooling_retry", None),
            handle_inbound_delivery_result_resolver=lambda: getattr(self, "_handle_inbound_delivery_result", None),
        )

    def _get_inbound_reply_sender(self) -> _ReplySender:
        """装配主回复链发送协作者，收口投递、冷却与结果处理能力。"""
        return _ReplySender.from_execution_deps(self._get_inbound_reply_execution_deps())

    def _get_inbound_reply_failure_provider(self) -> _InboundReplyFailureProvider:
        """装配主回复链失败标记 provider，收口零散执行后置能力。"""
        return _InboundReplyFailureProvider.from_resolver(
            mark_message_failed_resolver=lambda: getattr(self, "_mark_message_failed", None),
        )

    def _get_inbound_reply_stage_bundle(self) -> _InboundReplyStageBundle:
        """聚合主回复链阶段能力，形成更明确的阶段边界。"""
        return {
            "request": self._get_inbound_reply_request_deps(),
            "plan": self._get_inbound_reply_plan_deps(),
            "execution": self._get_inbound_reply_execution_deps(),
            "failure": self._get_inbound_reply_failure_provider(),
        }

    def _get_inbound_reply_inbound_state_bundle(self) -> _InboundReplyInboundStateBundle:
        """聚合 planner/executor 的入站状态访问入口。"""
        return {
            "planner_inbound_gateway": self._get_planner_inbound_event_gateway(),
            "executor_inbound_gateway": self._get_executor_inbound_event_gateway(),
        }

    def _get_inbound_reply_access_bundle(self) -> _InboundReplyAccessBundle:
        """聚合 workflow/outbox/intent 的访问入口。"""
        return {
            "workflow_gateway": self._get_inbound_workflow_gateway(),
            "customer_intent_gateway": self._get_inbound_customer_intent_gateway(),
            "outbox_gateway": self._get_inbound_outbox_event_gateway(),
        }

    def _get_inbound_reply_gateway_bundle(self) -> _InboundReplyGatewayBundle:
        """聚合主回复链依赖的仓储/状态网关，形成更明确的访问层边界。"""
        inbound_state_bundle = self._get_inbound_reply_inbound_state_bundle()
        access_bundle = self._get_inbound_reply_access_bundle()
        return {
            "inbound_state_bundle": inbound_state_bundle,
            "access_bundle": access_bundle,
        }

    def _get_inbound_reply_factory_assembly_builder(self) -> _InboundReplyFactoryAssemblyBuilder:
        """提供主回复链高层装配 builder，继续缩小 factory 入口的直接拼装面积。"""
        return _InboundReplyFactoryAssemblyBuilder(
            stage_bundle_resolver=self._get_inbound_reply_stage_bundle,
            gateway_bundle_resolver=self._get_inbound_reply_gateway_bundle,
        )

    def _get_inbound_reply_assembler(self) -> _InboundReplyAssembler:
        """提供主回复链本地 assembler，统一承接 assembly 与 factory 的装配入口。"""
        return _InboundReplyAssembler(
            stage_bundle_resolver=self._get_inbound_reply_stage_bundle,
            gateway_bundle_resolver=self._get_inbound_reply_gateway_bundle,
            assembly_builder_resolver=self._get_inbound_reply_factory_assembly_builder,
        )

    def _get_inbound_reply_factory_assembly(self) -> _InboundReplyFactoryAssembly:
        """聚合 provider/gateway 装配结果，减少 factory 入口的直接拼装面积。"""
        return self._get_inbound_reply_assembler().get_factory_assembly()

    def _get_inbound_reply_factory(self) -> _InboundReplyComponentFactory:
        """统一装配主回复链协作对象，收口 planner/executor/orchestrator 的装配细节。"""
        return self._get_inbound_reply_assembler().get()

    def _get_inbound_reply_planner(self) -> _InboundReplyPlanner:
        """提供主回复链 plan 协作对象，便于后续进一步迁出服务边界。"""
        return self._get_inbound_reply_factory().build_planner()

    def _get_inbound_reply_executor(self) -> _InboundReplyExecutor:
        """提供主回复链 execute 协作对象，便于后续进一步迁出服务边界。"""
        return self._get_inbound_reply_factory().build_executor()

    def _get_inbound_reply_orchestrator(self) -> _InboundReplyOrchestrator:
        """提供主回复链总控协作对象，BotService 仅负责装配入口。"""
        return self._get_inbound_reply_factory().build_orchestrator()

    def _get_inbound_workflow_gateway(self) -> _WorkflowRunGateway:
        return _WorkflowRunGateway.from_manager_resolver(
            lambda: getattr(self, "message_workflow_manager", None)
        )

    def _get_planner_inbound_event_gateway(self) -> _InboundEventGateway:
        return _InboundEventGateway.from_resolver(
            finalize_inbound_event_safe_resolver=lambda: getattr(self, "_finalize_inbound_event_safe", None),
            skip_inbound_pipeline_resolver=lambda: getattr(self, "_skip_inbound_pipeline", None),
        )

    def _get_executor_inbound_event_gateway(self) -> _InboundEventGateway:
        return _InboundEventGateway.from_resolver(
            finalize_inbound_event_safe_resolver=lambda: getattr(getattr(self, "db", None), "finalize_inbound_event", None),
            skip_inbound_pipeline_resolver=lambda: getattr(self, "_skip_inbound_pipeline", None),
        )

    def _get_inbound_outbox_event_gateway(self) -> _OutboxEventGateway:
        return _OutboxEventGateway.from_resolver(
            update_outbox_event_resolver=lambda: getattr(getattr(self, "db", None), "update_outbox_event", None),
        )

    def _get_inbound_customer_intent_gateway(self) -> _CustomerIntentGateway:
        return _CustomerIntentGateway.from_resolver(
            update_customer_intent_resolver=lambda: getattr(getattr(self, "db", None), "update_customer_intent", None),
        )

    def _get_inbound_message_state_repository(self) -> _MessageStateRepository:
        db = getattr(self, "db", None)
        workflow_manager = getattr(self, "message_workflow_manager", None)
        noop = lambda *args, **kwargs: None
        return _MessageStateRepository(
            finalize_inbound_event_safe=getattr(db, "finalize_inbound_event", None) or noop,
            update_outbox_event=getattr(db, "update_outbox_event", None) or noop,
            bind_workflow_outbox=getattr(workflow_manager, "bind_outbox", None) or noop,
            fail_workflow_run=(
                (lambda workflow_run_id, reason: workflow_manager.fail_run(workflow_run_id, reason=reason))
                if workflow_manager is not None and getattr(workflow_manager, "fail_run", None) is not None
                else noop
            ),
            mark_message_failed=getattr(self, "_mark_message_failed", None) or noop,
        )

    def _prepare_inbound_delivery_plan(
        self,
        *,
        message: dict,
        request: _InboundReplyRequest,
    ) -> Optional[_InboundDeliveryPlan]:
        """兼容入口：委托给 planner 生成投递计划。"""
        return self._get_inbound_reply_planner().prepare_plan(
            message=message,
            request=request,
        )

    def _update_inbound_customer_intent_if_needed(
        self,
        *,
        customer_id: str,
        platform: str,
        intent_level: str,
        intent_score: float,
    ) -> None:
        """兼容入口：委托给 executor 完成后置意向更新。"""
        self._get_inbound_reply_executor().update_customer_intent_if_needed(
            customer_id=customer_id,
            platform=platform,
            intent_level=intent_level,
            intent_score=intent_score,
        )

    def _handle_inbound_cooling_retry(
        self,
        *,
        message: dict,
        customer_name: str,
        content: str,
        msg_id: str,
        logical_message_id: str,
        workflow_run_id: str = "",
        reply_outbox_id: str = "",
        smart_result: dict,
        reply_content: str,
        blocked_reason: str,
    ) -> None:
        """收口主回复链的冷却重试调度与耗尽失败路径。"""
        conversation_id = str(message.get("conversation_id", "") or "")
        trace_id = str(logical_message_id or msg_id or "")[:12]
        logger.info(
            f"回复冷却中 [{customer_name}]: trace={trace_id or '-'} "
            f"conversation_id={conversation_id or '-'} source_message_id={msg_id or '-'} "
            f"logical_message_id={logical_message_id or '-'} outbox_id={reply_outbox_id or '-'} "
            f"reason={blocked_reason}，将延迟重试"
        )
        state_repository = self._get_inbound_message_state_repository()
        if not self._allow_background_delivery_without_new_inbound("cooling_retry"):
            freeze_reason = f"await_new_inbound:{str(blocked_reason or 'cooling_retry').strip() or 'cooling_retry'}"
            self._freeze_auto_delivery_work_item(
                outbox_id=reply_outbox_id,
                workflow_run_id=workflow_run_id,
                reason=freeze_reason,
                suggested_reply=reply_content,
            )
            logger.info(
                f"冷却命中后已冻结自动发送: customer={customer_name} trace={trace_id or '-'} "
                f"conversation_id={conversation_id or '-'} outbox_id={reply_outbox_id or '-'} "
                f"reason={freeze_reason}"
            )
            return
        retry_msg = dict(message)
        retry_msg["_cooling_retry_count"] = retry_msg.get("_cooling_retry_count", 0) + 1
        retry_msg["_cached_smart_result"] = dict(smart_result or {})
        retry_msg["_cached_reply_content"] = reply_content
        if reply_outbox_id:
            retry_msg["_reply_outbox_id"] = reply_outbox_id
        if workflow_run_id:
            retry_msg["_workflow_run_id"] = workflow_run_id
        if retry_msg["_cooling_retry_count"] <= 5:
            try:
                self._reply_executor.submit(self._delayed_reply_retry, retry_msg, 3, 0)
                if reply_outbox_id:
                    next_retry_at = (datetime.now() + timedelta(seconds=3)).isoformat()
                    state_repository.update_outbox_event(
                        reply_outbox_id,
                        status="retry_pending",
                        retry_count=int(retry_msg["_cooling_retry_count"]),
                        next_retry_at=next_retry_at,
                        reason=str(blocked_reason or "")[:100],
                        workflow_run_id=workflow_run_id,
                    )
                logger.info(
                    f"已提交冷却重试({retry_msg['_cooling_retry_count']}/5): customer={customer_name} "
                    f"trace={trace_id or '-'} conversation_id={conversation_id or '-'} "
                    f"source_message_id={msg_id or '-'} logical_message_id={logical_message_id or '-'} "
                    f"outbox_id={reply_outbox_id or '-'}"
                )
            except Exception:
                if reply_outbox_id:
                    state_repository.update_outbox_event(
                        reply_outbox_id,
                        status="failed",
                        reason="cooling_retry_submit_failed",
                        workflow_run_id=workflow_run_id,
                    )
                self._fail_inbound_pipeline(
                    customer_name=customer_name,
                    content=content,
                    msg_id=msg_id,
                    logical_message_id=logical_message_id,
                    reason="cooling_retry_submit_failed",
                )
            return

        logger.warning(
            f"冷却重试已达上限，标记失败: customer={customer_name} trace={trace_id or '-'} "
            f"conversation_id={conversation_id or '-'} source_message_id={msg_id or '-'} "
            f"logical_message_id={logical_message_id or '-'} outbox_id={reply_outbox_id or '-'}"
        )
        if reply_outbox_id:
            state_repository.update_outbox_event(
                reply_outbox_id,
                status="failed",
                reason="cooling_retry_exhausted",
                workflow_run_id=workflow_run_id,
            )
        self._fail_inbound_pipeline(
            customer_name=customer_name,
            content=content,
            msg_id=msg_id,
            logical_message_id=logical_message_id,
            reason="cooling_retry_exhausted",
        )

    def _handle_inbound_pipeline_exception(
        self,
        *,
        customer_name: str,
        content: str,
        msg_id: str,
        logical_message_id: str,
        workflow_run_id: str,
        reply_outbox_id: str,
        reply_msg_id: str,
        exc: Exception,
    ) -> None:
        """兼容入口：委托给 executor 完成最外层异常收口。"""
        self._get_inbound_reply_executor().handle_pipeline_exception(
            customer_name=customer_name,
            content=content,
            msg_id=msg_id,
            logical_message_id=logical_message_id,
            workflow_run_id=workflow_run_id,
            reply_outbox_id=reply_outbox_id,
            reply_msg_id=reply_msg_id,
            exc=exc,
        )

    def _execute_inbound_delivery_plan(
        self,
        *,
        message: dict,
        plan: _InboundDeliveryPlan,
    ) -> _InboundDeliveryExecutionResult:
        """兼容入口：委托给 executor 完成投递阶段执行。"""
        return self._get_inbound_reply_executor().execute_plan(
            message=message,
            plan=plan,
        )

    def _deliver_workflow_reply(
        self,
        *,
        workflow_run_id: str = "",
        inbound_logical_message_id: str = "",
        conversation_id: str,
        customer_name: str,
        reply_content: str,
        customer_id: str = "",
        platform: str = "douyin",
        intent_level: str = "E",
        intent_score: float = 0.0,
        smart_result: Optional[dict] = None,
        session=None,
        original_content: str = "",
        outbox_id: str = "",
        logical_message_id: str = "",
        reply_msg_id: str = "",
        trigger: str = "workflow_resume",
        retry_count: int = 0,
    ) -> dict:
        """将工作流中的待发送回复真正投递到统一发送主链。"""
        from datetime import datetime

        reply_content = (reply_content or "").strip()
        customer_name = (customer_name or "").strip()
        conversation_id = (conversation_id or "").strip()
        platform = (platform or "douyin").strip() or "douyin"
        retry_count = max(int(retry_count or 0), 0)

        if not reply_content:
            return {"success": False, "status": "invalid", "message": "reply_content_empty"}
        if not customer_name:
            return {"success": False, "status": "invalid", "message": "customer_name_empty"}

        if not conversation_id:
            conversation_id = self._resolve_conversation_id(customer_name, platform, customer_id)

        if not reply_msg_id:
            reply_msg_id = (
                f"{trigger}_{int(datetime.now().timestamp() * 1000)}_"
                f"{hashlib.md5(reply_content.encode('utf-8')).hexdigest()[:6]}"
            )
        if not logical_message_id:
            logical_message_id = build_logical_message_id(
                platform=platform,
                conversation_id=conversation_id,
                customer_name=customer_name,
                direction="outbound",
                content=reply_content,
                source_message_id=reply_msg_id,
            )
        if not outbox_id:
            outbox_id = f"outbox_{logical_message_id}"

        if workflow_run_id:
            try:
                self._get_inbound_message_state_repository().bind_workflow_outbox(workflow_run_id, outbox_id)
                self.message_workflow_manager.activate_run(
                    workflow_run_id,
                    node_id="send_reply",
                    metadata={"outbox_id": outbox_id, "trigger": trigger, "retry_count": retry_count},
                )
            except Exception as workflow_e:
                logger.debug(f"激活工作流失败: {workflow_e}")

        attempt_result = self._attempt_reply_delivery(
            customer_name=customer_name,
            reply_content=reply_content,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
            intent_level=intent_level,
            intent_score=intent_score,
            smart_result=smart_result or {
                "priority_level": "P1",
                "risk_assessment": {"overall_level": "medium"},
                "sentiment": "neutral",
                "suggested_action": trigger,
            },
            session=session,
            original_content=original_content or reply_content,
            logical_message_id=logical_message_id,
            outbox_id=outbox_id,
            reply_msg_id=reply_msg_id,
            allow_duplicate_content=(trigger != "outbox_retry"),
            duplicate_as_success=True,
            outbound_source="workflow",
            outbound_trigger=trigger,
        )
        delivery_decision = self._get_outbound_send_verifier().resolve_workflow_delivery_decision(attempt_result)
        if delivery_decision.action == "finalize_success":
            self._finalize_workflow_delivery_success(
                workflow_run_id=workflow_run_id,
                inbound_logical_message_id=inbound_logical_message_id,
                outbox_id=outbox_id,
                reply_msg_id=reply_msg_id,
                trigger=trigger,
                deduplicated=delivery_decision.deduplicated,
            )
            return {
                "success": delivery_decision.success,
                "status": delivery_decision.status,
                "message_id": reply_msg_id,
                "outbox_id": outbox_id,
            }
        self._pause_workflow_delivery(
            workflow_run_id=workflow_run_id,
            outbox_id=outbox_id,
            reply_msg_id=reply_msg_id,
            reply_content=reply_content,
            reason=delivery_decision.pause_reason,
        )
        return {
            "success": delivery_decision.success,
            "status": delivery_decision.status,
            "message": delivery_decision.message,
            "outbox_id": outbox_id,
        }

    def _resolve_customer_name_from_conversation(self, conversation_id: str) -> str:
        """根据会话补全客户名，供营销 tracking 状态回写使用。"""
        if not conversation_id:
            return ""

        try:
            for conversation in reversed(self._get_chat_store().get_all_conversations_dicts() or []):
                if conversation.get("conversation_id") != conversation_id:
                    continue
                candidate = (
                    conversation.get("customer_name")
                    or conversation.get("name")
                    or conversation.get("nickname")
                    or conversation.get("customer_id")
                    or ""
                ).strip()
                if candidate:
                    return candidate
        except Exception as conv_e:
            logger.debug(f"通过会话查找客户名失败: {conv_e}")

        try:
            for message in reversed(self._get_chat_store().get_recent_messages_dicts(conversation_id, limit=20) or []):
                candidate = (
                    message.get("customer_name")
                    or message.get("sender_name")
                    or message.get("nickname")
                    or message.get("customer_id")
                    or ""
                ).strip()
                if candidate and candidate not in {"我", "self"}:
                    return candidate
        except Exception as msg_e:
            logger.debug(f"通过消息历史查找客户名失败: {msg_e}")

        return ""

    @staticmethod
    def _normalize_live_target_name(name: str) -> str:
        text = str(name or "").strip().lower()
        if text.startswith("@"):
            text = text[1:]
        return text.replace(" ", "").replace("\u200b", "")

    def _load_persisted_live_state_sources(self) -> List[Dict[str, Any]]:
        sources: List[Dict[str, Any]] = []
        try:
            persistor = getattr(self, "_state_persistor", None)
            if persistor is None:
                from src.douyin_bot.app_state_persistor import get_app_state_persistor

                persistor = get_app_state_persistor()
            if not persistor:
                return sources

            load_rpa_state = getattr(persistor, "load_rpa_engine_state", None)
            if callable(load_rpa_state):
                rpa_state: Any = load_rpa_state() or {}
                if isinstance(rpa_state.get("states"), dict) and rpa_state["states"]:
                    sources.append(rpa_state["states"])

            load_monitor_state = getattr(persistor, "load_message_monitor_state", None)
            if callable(load_monitor_state):
                monitor_state: Any = load_monitor_state() or {}
                conversation_states = monitor_state.get("conversation_states")
                if isinstance(conversation_states, dict) and conversation_states:
                    sources.append(conversation_states)
        except Exception as persisted_e:
            logger.debug(f"读取持久化 live 状态失败: {persisted_e}")
        return sources

    def _get_live_conversation_name_samples(self, limit: int = 12) -> List[str]:
        samples: List[str] = []
        seen = set()
        sources = []
        try:
            rpa_engine = getattr(getattr(self, "rpa_launcher", None), "rpa_engine", None)
            if rpa_engine:
                sources.append(getattr(rpa_engine, "_states", {}) or {})
        except Exception as rpa_e:
            logger.debug(f"收集 RPA live 会话名失败: {rpa_e}")
        try:
            if getattr(self, "message_monitor", None):
                sources.append(getattr(self.message_monitor, "_conversation_states", {}) or {})
        except Exception as monitor_e:
            logger.debug(f"收集 monitor live 会话名失败: {monitor_e}")
        if not sources:
            sources.extend(self._load_persisted_live_state_sources())

        for source in sources:
            for raw_name, state in source.items():
                candidates = [raw_name]
                if isinstance(state, dict):
                    candidates.extend(state.get("aliases", []) or [])
                    candidates.append(state.get("customer_id", ""))
                for candidate in candidates:
                    name = str(candidate or "").strip()
                    normalized = self._normalize_live_target_name(name)
                    if not name or not normalized or normalized in seen:
                        continue
                    seen.add(normalized)
                    samples.append(name)
                    if len(samples) >= limit:
                        return samples
        return samples

    def _collect_send_target_candidates(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> List[str]:
        candidates: List[str] = []
        seen = set()

        def _add(candidate: str):
            text = str(candidate or "").strip()
            if not text:
                return
            normalized = self._normalize_live_target_name(text)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append(text)

        _add(customer_name)
        if customer_id and not str(customer_id).startswith("temp_"):
            _add(customer_id)

        try:
            all_conversations = self._get_chat_store().get_all_conversations_dicts() or []
        except Exception as conv_e:
            logger.debug(f"收集发送目标候选时读取会话失败: {conv_e}")
            all_conversations = []

        if conversation_id:
            for conversation in reversed(all_conversations):
                if str(conversation.get("conversation_id", "")).strip() != str(conversation_id).strip():
                    continue
                _add(conversation.get("customer_name", ""))
                _add(conversation.get("name", ""))
                _add(conversation.get("nickname", ""))
                _add(conversation.get("customer_id", ""))
                break
            try:
                for message in reversed(self._get_chat_store().get_recent_messages_dicts(conversation_id, limit=20) or []):
                    _add(message.get("customer_name", ""))
                    _add(message.get("sender_name", ""))
                    _add(message.get("nickname", ""))
                    _add(message.get("customer_id", ""))
            except Exception as msg_e:
                logger.debug(f"收集发送目标候选时读取消息历史失败: {msg_e}")

        if customer_id:
            for conversation in reversed(all_conversations):
                if str(conversation.get("platform", "douyin")).strip() != str(platform or "douyin").strip():
                    continue
                if str(conversation.get("customer_id", "")).strip() != str(customer_id).strip():
                    continue
                _add(conversation.get("customer_name", ""))
                _add(conversation.get("name", ""))
                _add(conversation.get("nickname", ""))

        return candidates

    def _resolve_live_send_target_name(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> tuple[str, str]:
        live_names = self._get_live_conversation_name_samples(limit=20)
        if not live_names:
            return (str(customer_name or "").strip(), "original")

        normalized_live_pairs = [
            (self._normalize_live_target_name(live_name), live_name)
            for live_name in live_names
            if str(live_name or "").strip()
        ]

        candidates = self._collect_send_target_candidates(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        for candidate in candidates:
            normalized_candidate = self._normalize_live_target_name(candidate)
            if not normalized_candidate:
                continue
            for normalized_live_name, live_name in normalized_live_pairs:
                if normalized_live_name == normalized_candidate:
                    source = "original" if candidate == customer_name else "alias"
                    return live_name, source
            for normalized_live_name, live_name in normalized_live_pairs:
                if not normalized_live_name:
                    continue
                if (
                    normalized_live_name.startswith(normalized_candidate)
                    or normalized_candidate.startswith(normalized_live_name)
                ):
                    source = "original" if candidate == customer_name else "alias"
                    return live_name, source

        return (str(customer_name or "").strip(), "original")

    def _maybe_merge_identity_conversations(
        self,
        *,
        customer_name: str,
        customer_id: str = "",
        platform: str = "douyin",
        conversation_id: str = "",
    ) -> str:
        normalized_customer_id = str(customer_id or "").strip()
        normalized_platform = str(platform or "douyin").strip() or "douyin"
        primary_conversation_id = str(conversation_id or "").strip()
        if not normalized_customer_id or normalized_customer_id.startswith("temp_"):
            return primary_conversation_id
        merge_method = getattr(self.db, "merge_customer_conversations_by_customer_id", None)
        if not callable(merge_method):
            return primary_conversation_id
        try:
            merge_result = merge_method(
                customer_id=normalized_customer_id,
                platform=normalized_platform,
                primary_conversation_id=primary_conversation_id,
                preferred_customer_name=str(customer_name or "").strip(),
            ) or {}
            merge_result_typed: Dict[str, Any] = merge_result if isinstance(merge_result, dict) else {}  # type: ignore[assignment]
            merged_conversation_id = str(
                merge_result_typed.get("primary_conversation_id") or primary_conversation_id
            ).strip()
            alias_ids = merge_result_typed.get("alias_ids", []) or []
            if merge_result_typed.get("merged") and alias_ids:
                logger.info(
                    f"按稳定身份归并会话完成: customer_id={normalized_customer_id}, "
                    f"primary={merged_conversation_id}, aliases={alias_ids}"
                )
            return merged_conversation_id or primary_conversation_id
        except Exception as merge_e:
            logger.debug(f"按稳定身份归并会话失败: {merge_e}")
            return primary_conversation_id

    def _get_conversation_snapshot(self, conversation_id: str) -> Dict[str, Any]:
        normalized_conversation_id = str(conversation_id or "").strip()
        if not normalized_conversation_id:
            return {}
        try:
            for conversation in reversed(self._get_chat_store().get_all_conversations_dicts() or []):
                if str(conversation.get("conversation_id", "")).strip() == normalized_conversation_id:
                    return conversation
        except Exception as conv_e:
            logger.debug(f"读取会话快照失败: {conv_e}")
        return {}

    def _classify_non_retryable_send_failure(self, customer_name: str, failure_reason: str) -> str:
        return self._get_outbound_send_verifier().classify_non_retryable_failure(
            customer_name,
            failure_reason,
        )

    def _verify_send_success(self, send_result, *, customer_name: str = ""):
        return self._get_outbound_send_verifier().verify_send_success(
            send_result,
            customer_name=customer_name,
        )

    def _diagnose_send_failure(self, send_result, *, customer_name: str = ""):
        return self._get_outbound_send_verifier().diagnose_failure(
            send_result,
            customer_name=customer_name,
        )

    def _is_send_failure_retryable(self, failure_reason: str) -> bool:
        return self._get_outbound_send_verifier().is_retryable(failure_reason)

    def _record_marketing_tracking_message(
        self,
        *,
        customer_name: str,
        conversation_id: str,
        reply_content: str,
        platform: str,
        reply_msg_id: str,
        logical_message_id: str,
    ) -> None:
        """在统一出站成功后记录营销 tracking。"""
        customer_name = (customer_name or "").strip()
        if not customer_name:
            return

        try:
            record = marketing_tracking_service.record_message(
                customer_name=customer_name,
                content=reply_content,
                status="sent",
                conversation_id=conversation_id,
                source_message_id=reply_msg_id,
                logical_message_id=logical_message_id,
                platform=platform,
            )
            record_id = (record or {}).get("id", "")
            if record_id:
                marketing_tracking_service.update_message_status(record_id, "delivered")
        except Exception as tracking_e:
            logger.debug(f"记录营销 tracking 发送事件失败: {tracking_e}")

    def _update_marketing_tracking_status(
        self,
        *,
        status: str,
        customer_name: str = "",
        conversation_id: str = "",
        allowed_current_statuses: Optional[List[str]] = None,
    ) -> bool:
        """按客户/会话更新最近一条营销消息状态。"""
        customer_name = (customer_name or "").strip() or self._resolve_customer_name_from_conversation(conversation_id)
        conversation_id = (conversation_id or "").strip()
        if not customer_name and not conversation_id:
            return False

        try:
            return marketing_tracking_service.update_latest_message_status(
                status=status,
                customer_name=customer_name,
                conversation_id=conversation_id,
                allowed_current_statuses=allowed_current_statuses or [],
            )
        except Exception as tracking_e:
            logger.debug(f"更新营销 tracking 状态失败: {tracking_e}")
            return False

    def _fail_inbound_submission(self, message: dict, reason: str) -> None:
        """统一收口入站提交阶段失败，避免逻辑事件长期卡在 processing。"""
        state_repository = self._get_inbound_message_state_repository()
        logical_message_id = str(message.get("logical_message_id", "") or "")
        workflow_run_id = str(message.get("workflow_run_id", "") or "")
        customer_name = str(message.get("customer_name", "") or "")
        content = str(message.get("content", message.get("last_message_content", "")) or "")
        msg_id = str(message.get("msg_id", "") or "")
        short_reason = (reason or "submit_failed")[:100]

        if customer_name and content:
            try:
                state_repository.mark_message_failed(customer_name, content, msg_id)
            except Exception as mark_e:
                logger.debug(f"标记入站提交失败状态失败: {mark_e}")

        if logical_message_id:
            try:
                state_repository.finalize_inbound_event_safe(
                    logical_message_id,
                    status="failed",
                    source_message_id=msg_id,
                    reason=short_reason,
                )
            except Exception as finalize_e:
                logger.debug(f"收口入站提交失败事件失败: {finalize_e}")

        if workflow_run_id:
            try:
                state_repository.fail_workflow_run(workflow_run_id, short_reason)
            except Exception as workflow_e:
                logger.debug(f"收口工作流失败状态失败: {workflow_e}")

    # ==================== Worker线程 ====================
    
    def _create_task_queue(self):
        return queue.PriorityQueue(maxsize=200)

    def _infer_task_priority(self, func) -> int:
        func_name = getattr(func, "__name__", str(func))
        high_priority_names = {
            "_init_browser_instance",
            "_cleanup_and_init_browser",
            "_restart_impl",
            "_start_monitoring_impl",
            "_stop_monitoring_impl",
            "send_outbound_message",
        }
        low_priority_names = {
            "_search_impl",
            "_send_impl",
            "sync_conversations_from_douyin",
            "crawl_comments",
        }
        if func_name in high_priority_names:
            return self.TASK_PRIORITY_HIGH
        if func_name in low_priority_names:
            return self.TASK_PRIORITY_LOW
        return self.TASK_PRIORITY_NORMAL

    def _build_task_item(self, func, args, kwargs, future, priority: Optional[int] = None):
        if not hasattr(self, "_task_sequence") or self._task_sequence is None:
            self._task_sequence = itertools.count()
        task_priority = self._infer_task_priority(func) if priority is None else int(priority)
        return (task_priority, next(self._task_sequence), (func, args, kwargs, future))

    def _build_shutdown_task_item(self):
        if not hasattr(self, "_task_sequence") or self._task_sequence is None:
            self._task_sequence = itertools.count()
        if not hasattr(self, "_shutdown_task_marker") or self._shutdown_task_marker is None:
            self._shutdown_task_marker = object()
        return (self.TASK_PRIORITY_HIGH, next(self._task_sequence), self._shutdown_task_marker)

    def _normalize_task_item(self, task_item):
        if task_item is None:
            return self._build_shutdown_task_item()
        if (
            isinstance(task_item, tuple)
            and len(task_item) == 3
            and isinstance(task_item[0], int)
            and isinstance(task_item[1], int)
        ):
            return task_item
        if isinstance(task_item, tuple) and len(task_item) == 4:
            func, args, kwargs, future = task_item
            return self._build_task_item(func, args, kwargs, future)
        return task_item

    def _extract_task_payload(self, task_item):
        if task_item is None:
            return None
        if (
            isinstance(task_item, tuple)
            and len(task_item) == 3
            and isinstance(task_item[0], int)
            and isinstance(task_item[1], int)
        ):
            payload = task_item[2]
            if payload is getattr(self, "_shutdown_task_marker", None):
                return None
            return payload
        if isinstance(task_item, tuple) and len(task_item) == 4:
            return task_item
        return task_item

    def _worker_loop(self):
        """Worker线程主循环（检查轮询信号执行消息轮询，保证Playwright线程安全）

        [FIX-INST:start-reply-bug] 修复根因 B：极端并发场景下（如 PyInstaller 冻结启动），
        worker_thread 可能比 __init__ 中的 _poll_signal 初始化更早进入 _worker_loop，
        访问 self._poll_signal 会抛 AttributeError。本方法在每次循环开始时做防御性
        检查，__init__ 未就绪时短暂 sleep 等待，避免持续抛错刷屏错误日志。
        """
        logger.info("Worker thread started")

        while True:
            try:
                # [FIX-INST:start-reply-bug] 防御性：等 __init__ 完成 _poll_signal 初始化
                if not hasattr(self, "_poll_signal") or self._poll_signal is None:
                    time.sleep(0.1)
                    continue
                if self._poll_signal.is_set():
                    self._poll_signal.clear()
                    self._poll_message_monitor()

                try:
                    task_item = self.task_queue.get(timeout=0.5)
                except queue.Empty:
                    if hasattr(self, "_poll_signal") and self._poll_signal is not None and self._poll_signal.is_set():
                        self._poll_signal.clear()
                        self._poll_message_monitor()
                    continue
                    
                task_payload = self._extract_task_payload(task_item)
                if task_payload is None:
                    break
                
                try:
                    func, args, kwargs, future = task_payload  # type: ignore[misc]  # type: ignore[misc]
                except (ValueError, TypeError) as unpack_e:
                    logger.error(f"Worker: 任务格式异常: {unpack_e}")
                    self.task_queue.task_done()
                    continue
                    
                func_name = getattr(func, '__name__', str(func))
                logger.debug(f"Worker: Executing task: {func_name}, is_running={self.is_running}")
                
                try:
                    if self.is_running or func_name in [
                        '_init_browser_instance', '_cleanup_and_init_browser',
                        '_restart_impl',
                        '_start_monitoring_impl', '_stop_monitoring_impl',
                        'send_message_task', '_auto_reply_message',
                        '_auto_reply_message_safe', '_on_new_message'
                    ]:
                        result = func(*args, **kwargs)
                        if future:
                            future.set_result(result)
                    else:
                        logger.warning(f"Worker: Task {func_name} rejected - browser not running (is_running={self.is_running})")
                        if future:
                            future.set_exception(Exception("Browser not running"))
                except Exception as e:
                    logger.error(f"Error executing task {func_name}: {e}")
                    if future:
                        future.set_exception(e)
                finally:
                    self.task_queue.task_done()
            
            except Exception as e:
                import traceback
                logger.error(f"Worker loop error: {e}\n{traceback.format_exc()}")
                time.sleep(1)
        
        # 清理
        try:
            if self.browser_manager:
                self.browser_manager.close()
        except Exception as e:
            logger.warning(f"关闭浏览器管理器失败: {e}")

    def _submit_task(self, func, *args, **kwargs):
        """提交任务并返回Future（自动检测并恢复Worker线程，队列满时超时）"""
        self._ensure_worker_alive()
        future = Future()
        try:
            self.task_queue.put(self._build_task_item(func, args, kwargs, future), timeout=30)
        except queue.Full:
            future.set_exception(Exception("任务队列已满，提交超时"))
            logger.warning(f"任务队列已满，提交超时: {getattr(func, '__name__', str(func))}")
        return future

    def _submit_task_no_wait(self, func, *args, **kwargs):
        """提交任务不等待结果（自动检测并恢复Worker线程，队列满时丢弃）"""
        self._ensure_worker_alive()
        try:
            self.task_queue.put_nowait(self._build_task_item(func, args, kwargs, None))
            return True
        except queue.Full:
            logger.warning(f"任务队列已满(200)，丢弃任务: {getattr(func, '__name__', str(func))}")
            return False

    def _ensure_worker_alive(self):
        """确保Worker线程存活，如果已退出则重新创建（保留未处理任务）
        
        修复：旧实现创建新队列后迁移旧队列任务，但_submit_task_no_wait
        已经将_init_browser_instance放入旧队列。当_ensure_worker_alive
        创建新队列时，旧任务可能还在旧队列中未被迁移。
        现改为：先迁移旧任务，再创建新Worker线程。
        """
        if not hasattr(self, 'worker_thread') or not self.worker_thread or not self.worker_thread.is_alive():
            logger.info("Worker线程已退出，重新创建...")
            old_queue = getattr(self, 'task_queue', None)
            self.task_queue = self._create_task_queue()
            if old_queue:
                rescued = 0
                while not old_queue.empty():
                    try:
                        task = old_queue.get_nowait()
                        try:
                            self.task_queue.put_nowait(self._normalize_task_item(task))
                            rescued += 1
                        except queue.Full:
                            logger.warning(f"任务迁移时新队列已满，丢弃任务")
                            break
                    except queue.Empty:
                        break
                if rescued > 0:
                    logger.info(f"从旧队列中迁移了 {rescued} 个待处理任务")
            self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self.worker_thread.start()

    def _process_pending_tasks_during_wait(self):
        """在爬取任务等待间隔中处理队列中的待执行任务
        
        由于爬取和消息回复操作使用不同的浏览器标签页(crawler_page vs monitor_page)，
        它们可以安全地交替执行。此方法允许在爬取任务的等待间隔中，
        处理队列中排队的任务（如启动消息回复、自动回复等），
        避免单Worker线程被长时间爬取任务阻塞导致其他任务无法执行。
        """
        processed = 0
        max_process = 5
        while processed < max_process:
            try:
                task_item = self.task_queue.get_nowait()
            except queue.Empty:
                break
            
            task_payload = self._extract_task_payload(task_item)
            if task_payload is None:
                break
            
            func, args, kwargs, future = task_payload  # type: ignore[misc]
            func_name = func.__name__
            deferred_task_names = {"_search_impl", "_send_impl"}

            if func_name in deferred_task_names:
                try:
                    self.task_queue.put_nowait(task_item)
                    logger.info(f"[等待间隔] 延后长任务，避免嵌套执行: {func_name}")
                except queue.Full:
                    logger.warning(f"[等待间隔] 回退长任务失败，队列已满: {func_name}")
                    if future:
                        future.set_exception(Exception("任务队列已满，无法重新排队"))
                finally:
                    self.task_queue.task_done()
                break
            
            try:
                if self.is_running or func_name in [
                    '_init_browser_instance', '_cleanup_and_init_browser', '_restart_impl',
                    '_start_monitoring_impl', '_stop_monitoring_impl',
                    'send_message_task', '_auto_reply_message',
                    '_on_new_message', '_auto_reply_message_safe'
                ]:
                    logger.info(f"[等待间隔] 执行排队任务: {func_name}")
                    result = func(*args, **kwargs)
                    if future:
                        future.set_result(result)
                else:
                    logger.warning(f"[等待间隔] 任务 {func_name} 被拒绝 - browser not running")
                    if future:
                        future.set_exception(Exception("Browser not running"))
            except Exception as e:
                logger.error(f"[等待间隔] 执行任务 {func_name} 失败: {e}")
                if future:
                    future.set_exception(e)
            finally:
                self.task_queue.task_done()
            
            processed += 1
        
        if processed > 0:
            logger.debug(f"[等待间隔] 处理了 {processed} 个排队任务")

    # ==================== 浏览器管理 ====================
    
    def _init_browser_instance(self):
        """初始化浏览器组件
        
        修复：当is_running=False时，说明之前的浏览器实例已失效（用户手动关闭等），
        必须完全清理旧资源后重建，而不是尝试复用已断开连接的对象。
        
        此方法在Worker线程中执行，可以安全地停止Playwright。
        """
        try:
            logger.info("初始化浏览器组件...")
            
            if self.browser_manager:
                try:
                    browser_manager_alive = False
                    if hasattr(self.browser_manager, "is_session_alive"):
                        browser_manager_alive = bool(self.browser_manager.is_session_alive())
                    elif hasattr(self.browser_manager, "browser") and self.browser_manager.browser:
                        try:
                            browser_manager_alive = bool(self.browser_manager.browser.is_connected())
                        except Exception:
                            browser_manager_alive = False

                    if browser_manager_alive:
                        logger.info("浏览器管理器仍有效，复用现有实例")
                    else:
                        logger.info("浏览器会话已失效，清理旧资源并重新创建")
                        try:
                            self.browser_manager.close()
                        except Exception:
                            pass
                        self.browser_manager = None
                except Exception:
                    try:
                        self.browser_manager.close()  # type: ignore[union-attr]
                    except Exception:
                        pass
                    self.browser_manager = None

            if not self.browser_manager:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        asyncio.set_event_loop(asyncio.new_event_loop())
                        logger.info("已替换关闭的asyncio事件循环")
                except RuntimeError:
                    asyncio.set_event_loop(asyncio.new_event_loop())
                    logger.info("已为Worker线程创建新的asyncio事件循环")
                managed_user_data_dir = self._resolve_browser_user_data_dir()
                logger.info(f"按账户启动浏览器 profile: {managed_user_data_dir}")
                self.browser_manager = BrowserManager(user_data_dir=managed_user_data_dir)
            self.browser_manager.start()

            coordinator = self._get_chat_session_coordinator()
            monitor_page = coordinator._acquire_monitor_page(self.browser_manager, force_new=False)
            if not monitor_page:
                raise RuntimeError("无法获取 monitor page")
            logger.info("监听标签页已创建")
            self._collapse_monitoring_to_single_page(monitor_page)
            
            self._setup_page_close_listener(monitor_page, "监听标签页")
            
            self.page = None
            # 登录状态检测应绑定到当前真实可见的聊天监听页，
            # 否则 start_monitor/check_login 会长期退化为未登录。
            self.login_handler = LoginHandler(monitor_page)
            self.crawler = None
            self.sender = None
            self.message_monitor = MessageMonitor(
                monitor_page,
                platform="douyin",
                browser_manager=self.browser_manager,
                chat_session_coordinator=self._get_chat_session_coordinator(),
            )

            # 初始化RPA引擎 (新增)
            # 注意：RPA引擎需要监听聊天页面，所以使用 monitor_page
            if self._use_rpa_mode:
                try:
                    self.rpa_launcher = RPALauncher(
                        page=monitor_page,
                        browser_manager=self.browser_manager,
                        database=self.db
                    )
                    self.rpa_launcher.setup()
                    if self.rpa_launcher.rpa_engine:
                        self.rpa_launcher.rpa_engine.message_monitor = self.message_monitor
                    logger.info("RPA引擎初始化成功")

                    try:
                        rpa_state = self._state_persistor.load_rpa_engine_state()
                        if rpa_state and self.rpa_launcher.rpa_engine:
                            self.rpa_launcher.rpa_engine.restore_persisted_state(rpa_state)
                    except Exception as rps_e:
                        logger.warning(f"恢复RPA引擎持久化状态失败: {rps_e}")

                    try:
                        bg_state = self._state_persistor.load_boundary_guard_state()
                        if bg_state and self.rpa_launcher.rpa_engine and self.rpa_launcher.rpa_engine._boundary_guard:
                            self.rpa_launcher.rpa_engine._boundary_guard.restore_persisted_state(bg_state)
                    except Exception as bg_e:
                        logger.warning(f"恢复边界保护器持久化状态失败: {bg_e}")
                except Exception as e:
                    logger.warning(f"RPA引擎初始化失败，将在启动监听时重试: {e}")
                    self.rpa_launcher = None

            self._update_runtime_state(is_running=True, browser_state="running", stop_flag=False)
            logger.info("浏览器初始化成功")

            try:
                session_state = self._state_persistor.load_session_state()
                if session_state and self.session_manager:
                    self.session_manager.restore_persisted_state(session_state)
            except Exception as sess_e:
                logger.warning(f"恢复会话管理器持久化状态失败: {sess_e}")

            try:
                mm_state = self._state_persistor.load_message_monitor_state()
                if mm_state and self.message_monitor:
                    self.message_monitor.restore_persisted_state(mm_state)
            except Exception as mm_e:
                logger.warning(f"恢复消息监控器持久化状态失败: {mm_e}")
            
            try:
                if self.rpa_launcher and self.rpa_launcher.rpa_engine:
                    bg = self.rpa_launcher.rpa_engine._boundary_guard
                    if bg and bg._locked_until and bg._locked_until < time.time():
                        logger.info(f"边界保护器锁定已过期(locked_until={bg._locked_until:.0f})，重置为正常状态")
                        bg._protection_level = type(bg._protection_level)('normal')
                        bg._consecutive_failures = 0
                        bg._locked_until = 0
                    elif bg and bg._protection_level.value != 'normal':
                        logger.info(f"边界保护器启动状态: level={bg._protection_level.value}, consecutive_failures={bg._consecutive_failures}，保留恢复的状态")
            except Exception as bg_e:
                logger.debug(f"浏览器初始化后: 边界保护器检查跳过: {bg_e}")
            
            # 初始化完成后立即同步检查登录状态
            try:
                if self.login_handler:
                    logger.info("开始同步检查登录状态...")
                    # 直接调用检测，不通过任务队列（避免异步问题）
                    status = self.login_handler.check_login_status()
                    self._login_status_cache = status
                    self._last_login_check_time = time.time()
                    logger.info(f"登录状态检测完成：{'已登录' if status else '未登录'}")
                    self.get_browser_runtime_snapshot(
                        force_scope_refresh=True,
                        persist_registry=True,
                    )
            except Exception as login_e:
                logger.warning(f"初始化时检查登录状态失败：{login_e}")
                import traceback
                logger.error(f"错误堆栈：{traceback.format_exc()}")

            self._restore_monitoring_state()
                
        except Exception as e:
            logger.error(f"浏览器初始化失败：{e}")
            import traceback
            logger.error(f"错误堆栈：{traceback.format_exc()}")
            self._update_runtime_state(is_running=False, browser_state="faulted")

    def _update_login_status_task(self):
        """更新登录状态"""
        try:
            if self.login_handler:
                status = self.login_handler.check_login_status()
                self._login_status_cache = status
                self._last_login_check_time = time.time()
        except Exception as e:
            logger.error(f"检查登录状态失败: {e}")

    def start_browser(self):
        """启动浏览器
        
        修复：当is_running=False但browser_manager等组件仍存在时（如页面被手动关闭后），
        先通过Worker线程清理旧资源再启动新实例。
        
        关键：浏览器上下文的清理和重建必须在同一线程（Worker线程）中完成，
        避免线程间交叉清理导致状态不一致。
        因此不在主线程中直接调用browser_manager.close()，
        而是将清理和初始化合并为一个Worker任务。
        """
        state = self._get_runtime_state_snapshot()
        if state["browser_state"] in {"starting", "running", "stopping", "recovering"}:
            return
        self._update_runtime_state(browser_state="starting", stop_flag=False)
        self._submit_task_no_wait(self._cleanup_and_init_browser)

    def start_browser_sync(self, timeout_seconds: float = 30.0) -> bool:
        """同步启动浏览器并等待结果，供需要立即继续后续动作的链路使用。"""
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        if self._sync_browser_runtime_state():
            return True

        state = self._get_runtime_state_snapshot()
        if state["browser_state"] in {"starting", "recovering"}:
            deadline = time.time() + max(timeout_seconds, 1.0)
            while time.time() < deadline:
                if self._sync_browser_runtime_state():
                    return True
                time.sleep(0.5)
            self._update_runtime_state(browser_state="faulted")
            logger.error("同步启动浏览器超时：已有启动流程但未进入运行态")
            return False

        self._update_runtime_state(browser_state="starting", stop_flag=False)

        if threading.current_thread() is getattr(self, "worker_thread", None):
            try:
                self._cleanup_and_init_browser()
            except Exception as e:
                logger.error(f"Worker线程内同步启动浏览器失败: {e}")
                self._update_runtime_state(is_running=False, browser_state="faulted")
                return False
            return bool(self._sync_browser_runtime_state())

        future = self._submit_task(self._cleanup_and_init_browser)
        try:
            future.result(timeout=max(timeout_seconds, 1.0))
        except FuturesTimeoutError:
            self._update_runtime_state(is_running=False, browser_state="faulted")
            logger.error("同步启动浏览器超时：Worker线程未在限定时间内完成初始化")
            return False
        except Exception as e:
            self._update_runtime_state(is_running=False, browser_state="faulted")
            logger.error(f"同步启动浏览器失败: {e}")
            return False

        return bool(self._sync_browser_runtime_state())

    def _cleanup_stale_managed_browser_processes(self, *, reason: str = "") -> int:
        """回收跨实例残留的自动化浏览器进程。"""
        try:
            cleaned = int(BrowserManager.cleanup_stale_managed_browser_processes() or 0)
            if cleaned > 0:
                suffix = f"（{reason}）" if reason else ""
                logger.warning(f"已回收 {cleaned} 个残留浏览器进程{suffix}")
            return cleaned
        except Exception as e:
            if reason:
                logger.warning(f"浏览器残留进程回收失败（{reason}）: {e}")
            else:
                logger.warning(f"浏览器残留进程回收失败: {e}")
            return 0
    
    def _cleanup_stale_browser_resources(self):
        """清理残留的浏览器资源（在Worker线程中调用，可安全停止Playwright）
        
        此方法在_cleanup_and_init_browser中被调用，确保重新启动前所有旧资源被正确释放。
        由于在Worker线程中执行，可以安全地停止Playwright实例。
        """
        try:
            if hasattr(self, 'rpa_launcher') and self.rpa_launcher:
                try:
                    if hasattr(self.rpa_launcher, 'cleanup'):
                        self.rpa_launcher.cleanup()
                    elif hasattr(self.rpa_launcher, 'stop'):
                        self.rpa_launcher.stop()
                except Exception as e:
                    logger.debug(f"清理残留RPA启动器失败: {e}")
                self.rpa_launcher = None
        except Exception as e:
            logger.debug(f"清理RPA资源异常: {e}")
        
        self.page = None
        self.login_handler = None
        self.crawler = None
        self.sender = None
        self.message_monitor = None
        
        try:
            if self.browser_manager:
                self.browser_manager.close()
        except Exception as e:
            logger.debug(f"清理残留browser_manager失败: {e}")
        self.browser_manager = None
        
        self._login_status_cache = False
        self._cleanup_stale_managed_browser_processes(reason="cleanup_stale_browser_resources")
        logger.info("残留浏览器资源清理完成")
    
    def _cleanup_and_init_browser(self):
        """在Worker线程中安全地清理旧资源并初始化新浏览器
        
        修复：将清理和初始化合并为一个Worker任务，确保浏览器上下文的清理和重建
        在同一线程中完成。这解决了两个关键问题：
        1. 残留浏览器上下文阻止新实例接管现有 profile
        2. 在主线程中清理浏览器资源可能导致 Worker 侧状态错乱
        
        额外安全措施：即使on_page_close已清理资源，仍检查监控状态，
        防止因竞态条件导致监控未正确停止而影响新实例。
        """
        if self._get_runtime_state_snapshot()["is_monitoring_messages"]:
            logger.info("检测到消息回复仍在运行，先停止回复")
            try:
                self._update_runtime_state(
                    is_monitoring_messages=False,
                    monitor_active=False,
                    monitoring_state="stopping",
                )
                self._stop_monitoring_impl()
            except Exception as e:
                logger.warning(f"停止消息回复失败: {e}")
        
        logger.info("启动前执行浏览器残留体检")
        self._cleanup_stale_browser_resources()
        
        self._init_browser_instance()

    def _clear_closed_auxiliary_page_refs(self, page, page_name: str):
        """清理被关闭的辅助标签页引用，避免保留已关闭页面对象。"""
        try:
            if page_name == "爬取标签页":
                if self.page is page:
                    self.page = None
                self.login_handler = None
                self.crawler = None
                self.sender = None
                if self.browser_manager and getattr(self.browser_manager, "crawler_page", None) is page:
                    self.browser_manager.crawler_page = None
            elif page_name == "综合搜索标签页":
                if self.browser_manager and getattr(self.browser_manager, "search_page", None) is page:
                    self.browser_manager.search_page = None
        except Exception as e:
            logger.debug(f"清理{page_name}引用失败: {e}")

    def _collapse_monitoring_to_single_page(self, monitor_page) -> None:
        """监听模式下仅保留聊天页，关闭多余首页/历史页。"""
        if not self.browser_manager or not monitor_page:
            return

        context = getattr(self.browser_manager, "context", None)
        if not context:
            return

        if getattr(self.browser_manager, "crawler_page", None) or getattr(self.browser_manager, "search_page", None):
            return

        closed_count = 0
        try:
            for page in list(getattr(context, "pages", []) or []):
                if not page or page is monitor_page:
                    continue
                try:
                    if page.is_closed():
                        continue
                except Exception:
                    continue

                role = ""
                try:
                    role = str(self.browser_manager._get_page_role(page) or "").strip()
                except Exception:
                    role = ""

                if role and role not in {"main", "monitor"}:
                    continue

                try:
                    page.close()
                    closed_count += 1
                except Exception as close_exc:
                    logger.debug(f"关闭监听模式多余标签页失败: {close_exc}")

            self.browser_manager.page = monitor_page
            self.browser_manager.monitor_page = monitor_page
            if closed_count > 0:
                logger.info(f"监听模式已关闭 {closed_count} 个多余标签页，仅保留聊天页")
        except Exception as e:
            logger.debug(f"监听模式收敛标签页失败: {e}")

    def _recover_monitor_page_after_close(self, closed_page) -> bool:
        """监听标签页关闭后，仅重建自动回复页而非整浏览器停机。"""
        if not self.browser_manager:
            return False

        try:
            if getattr(self.browser_manager, "monitor_page", None) is closed_page:
                self.browser_manager.monitor_page = None

            new_monitor_page = self._get_chat_session_coordinator().recover_monitor_page(closed_page)
            if not new_monitor_page or new_monitor_page.is_closed():
                logger.warning("监听标签页关闭后重建新页面失败")
                return False

            self._setup_page_close_listener(new_monitor_page, "监听标签页")
            self.message_monitor = MessageMonitor(
                new_monitor_page,
                platform="douyin",
                browser_manager=self.browser_manager,
                chat_session_coordinator=self._get_chat_session_coordinator(),
            )

            old_launcher = getattr(self, "rpa_launcher", None)
            self.rpa_launcher = None
            if old_launcher:
                try:
                    if hasattr(old_launcher, "cleanup"):
                        old_launcher.cleanup()
                    elif hasattr(old_launcher, "stop"):
                        old_launcher.stop()
                except Exception as cleanup_e:
                    logger.debug(f"监听标签页恢复前清理旧RPA启动器失败: {cleanup_e}")

            if self._use_rpa_mode and not self._retry_rpa_init():
                logger.warning("监听标签页已恢复，但RPA启动器重建失败，将等待后续发送链路再尝试恢复")

            self._update_runtime_state(is_running=True, browser_state="running")
            logger.info("监听标签页关闭后已完成局部恢复，浏览器主运行态保持不变")
            return True
        except Exception as e:
            logger.warning(f"监听标签页关闭后局部恢复失败: {e}")
            return False

    def _setup_page_close_listener(self, page, page_name: str, cleanup_on_close: bool = True):
        """监听浏览器页面关闭事件，自动更新is_running状态并清理资源
        
        当用户手动关闭浏览器标签页时，Playwright会触发close事件。
        此方法注册事件监听器，在页面关闭时：
        1. 停止消息回复（直接调用_stop_monitoring_impl，避免队列竞态）
        2. 清理所有组件引用（RPA、爬虫、发送器等）
        3. 关闭 browser_manager 并释放浏览器上下文
        4. 将is_running设为False，允许用户通过"启动抖音"按钮重新启动
        
        关键修复：
        - 旧实现将browser_manager设为None但未调用close()，导致Playwright实例残留，
          再次启动时新Playwright实例与旧实例冲突（asyncio loop错误）
        - 旧实现通过stop_message_monitoring()提交_stop_monitoring_impl到队列，
          若用户快速重启，_stop_monitoring_impl可能在新实例创建后才执行，误停新实例
        - 现改为直接调用_stop_monitoring_impl()，确保同步完成后再清理browser_manager
        
        重入保护：关闭browser_manager时可能触发其他页面的close事件（同一线程重入），
        使用_page_close_cleanup_in_progress标志防止重复清理。
        """
        if not page:
            return
        try:
            def on_page_close():
                if not cleanup_on_close:
                    self._clear_closed_auxiliary_page_refs(page, page_name)
                    logger.info(f"{page_name}被关闭，但该页面仅为辅助标签页，已清理本页引用并跳过全局资源清理")
                    return
                if not self._get_runtime_state_snapshot()["is_running"] and not self.browser_manager:
                    logger.debug(f"{page_name}被关闭，但资源已清理，跳过重复处理")
                    return
                
                if getattr(self, '_page_close_cleanup_in_progress', False):
                    logger.debug(f"{page_name}被关闭，但清理正在进行中，跳过重复处理")
                    return
                    
                self._page_close_cleanup_in_progress = True
                try:
                    runtime_snapshot = self._get_runtime_state_snapshot()
                    logger.warning(f"{page_name}被关闭，执行资源清理并更新is_running状态")

                    if page_name == "监听标签页":
                        was_monitoring = bool(runtime_snapshot["is_monitoring_messages"])
                        if was_monitoring:
                            try:
                                self._update_runtime_state(
                                    is_monitoring_messages=False,
                                    monitor_active=False,
                                    monitoring_state="stopping",
                                )
                                self._stop_monitoring_impl()
                            except Exception as e:
                                logger.debug(f"监听标签页关闭时停止消息回复失败: {e}")

                        if self._recover_monitor_page_after_close(page):
                            if was_monitoring:
                                try:
                                    logger.info("监听标签页恢复成功，尝试自动恢复消息回复")
                                    self._start_monitoring_impl()
                                except Exception as restart_e:
                                    logger.warning(f"监听标签页恢复后自动恢复消息回复失败: {restart_e}")
                            return

                        logger.warning("监听标签页局部恢复失败，回退到全局资源清理")

                    if runtime_snapshot["is_monitoring_messages"]:
                        try:
                            self._update_runtime_state(
                                is_monitoring_messages=False,
                                monitor_active=False,
                                monitoring_state="stopping",
                            )
                            self._stop_monitoring_impl()
                        except Exception as e:
                            logger.debug(f"页面关闭时停止消息回复失败: {e}")
                    
                    try:
                        if hasattr(self, 'rpa_launcher') and self.rpa_launcher:
                            if hasattr(self.rpa_launcher, 'cleanup'):
                                self.rpa_launcher.cleanup()
                            elif hasattr(self.rpa_launcher, 'stop'):
                                self.rpa_launcher.stop()
                    except Exception as e:
                        logger.debug(f"页面关闭时清理RPA启动器失败: {e}")
                    self.rpa_launcher = None
                    
                    self.page = None
                    self.login_handler = None
                    self.crawler = None
                    self.sender = None
                    self.message_monitor = None
                    
                    try:
                        if self.browser_manager:
                            self.browser_manager.close()
                    except Exception as e:
                        logger.warning(f"页面关闭时关闭browser_manager失败(浏览器可能已关闭): {e}")
                    self.browser_manager = None
                    
                    self._update_runtime_state(
                        is_running=False,
                        browser_state="stopped",
                        current_task="Idle",
                        monitoring_state="stopped",
                    )
                    self._login_status_cache = False
                    logger.info(f"{page_name}关闭后的资源清理已完成，可以重新启动浏览器")
                finally:
                    self._page_close_cleanup_in_progress = False
                
            page.on("close", on_page_close)
            logger.info(f"已注册{page_name}关闭事件监听器")
        except Exception as e:
            logger.debug(f"注册{page_name}关闭事件监听器失败: {e}")

    def stop_browser(self, preserve_monitoring_intent: bool = False):
        """关闭浏览器（级联停止所有子模块，防止资源泄漏）

        停止顺序：
        1. 停止消息回复（含RPA引擎停止）
        2. 停止消息总线（等待队列排空）
        3. 停止会话管理器和配置管理器
        4. 保存状态
        5. 关闭浏览器管理器
        6. 清除所有组件引用
        7. 停止Worker线程
        8. 关闭线程池
        """
        runtime_snapshot = self._get_runtime_state_snapshot()
        monitoring_status = self._get_monitoring_status()
        should_preserve_monitoring = bool(
            preserve_monitoring_intent
            and (
                runtime_snapshot.get("is_monitoring_messages")
                or monitoring_status.get("effective_monitoring")
            )
        )
        preserved_monitor_active = bool(
            monitoring_status.get("rpa_monitoring")
            or monitoring_status.get("traditional_monitoring")
            or runtime_snapshot.get("monitor_active")
        )
        preserved_last_monitor_check_time = float(runtime_snapshot.get("last_monitor_check_time", 0) or 0)

        self._update_runtime_state(browser_state="stopping")
        self.stop_message_monitoring(reason="stop_browser")

        if hasattr(self, 'rpa_launcher') and self.rpa_launcher:
            try:
                if hasattr(self.rpa_launcher, 'cleanup'):
                    self.rpa_launcher.cleanup()
                elif hasattr(self.rpa_launcher, 'stop'):
                    self.rpa_launcher.stop()
            except Exception as e:
                logger.debug(f"清理RPA启动器失败: {e}")
            self.rpa_launcher = None

        if hasattr(self, 'message_bus') and self.message_bus:
            try:
                self.message_bus.stop()
            except Exception as e:
                logger.debug(f"停止消息总线失败: {e}")

        if hasattr(self, 'session_manager') and self.session_manager:
            try:
                if hasattr(self.session_manager, 'stop'):
                    self.session_manager.stop()
            except Exception as e:
                logger.debug(f"停止会话管理器失败: {e}")

        self._save_state_before_shutdown()
        if should_preserve_monitoring:
            try:
                self._state_persistor.save_monitoring_state(
                    is_monitoring=True,
                    use_rpa_mode=self._use_rpa_mode,
                    monitor_active=preserved_monitor_active,
                    last_monitor_check_time=preserved_last_monitor_check_time,
                )
                logger.info("关闭浏览器时已保留自动回复恢复意图，供下次启动自动恢复")
            except Exception as preserve_e:
                logger.warning(f"保留自动回复恢复意图失败: {preserve_e}")
        
        if self.browser_manager:
            try:
                self.browser_manager.close()
                logger.info("浏览器管理器已关闭")
            except Exception as e:
                logger.warning(f"关闭浏览器管理器失败: {e}")
        self.browser_manager = None
        self._cleanup_stale_managed_browser_processes(reason="stop_browser")
        self.page = None
        self.login_handler = None
        self.crawler = None
        self.sender = None
        self.message_monitor = None
        self._login_status_cache = False
        
        self._update_runtime_state(
            is_running=False,
            browser_state="stopped",
            is_monitoring_messages=False,
            monitor_active=False,
            monitoring_state="stopped",
            current_task="Idle",
            stop_flag=False,
        )
        
        if hasattr(self, 'worker_thread') and self.worker_thread and self.worker_thread.is_alive():
            self.task_queue.put(self._build_shutdown_task_item())
            try:
                self.worker_thread.join(timeout=5)
                if self.worker_thread.is_alive():
                    logger.warning("Worker线程未在超时内退出")
                else:
                    logger.info("Worker线程已正常退出")
            except Exception as e:
                logger.debug(f"等待Worker线程退出时异常: {e}")
        
        self._shutdown_thread_pools()
    
    def _shutdown_thread_pools(self):
        """安全关闭所有线程池，防止资源泄漏（带超时保护）"""
        logger.info("正在关闭线程池...")
        
        shutdown_timeout = 15
        
        if hasattr(self, '_reply_executor') and self._reply_executor:
            try:
                self._reply_executor.shutdown(wait=False)
                logger.info("回复线程池已发起关闭")
            except Exception as e:
                logger.warning(f"关闭回复线程池失败: {e}")
        
        if hasattr(self, '_llm_executor') and self._llm_executor:
            try:
                self._llm_executor.shutdown(wait=False)
                logger.info("LLM线程池已发起关闭")
            except Exception as e:
                logger.warning(f"关闭LLM线程池失败: {e}")

        if hasattr(self, '_post_send_executor') and self._post_send_executor:
            try:
                self._post_send_executor.shutdown(wait=False)
                logger.info("发送后处理线程池已发起关闭")
            except Exception as e:
                logger.warning(f"关闭发送后处理线程池失败: {e}")

        coordinator = getattr(self, "_outbound_dispatch_coordinator_instance", None)
        if coordinator is not None:
            try:
                coordinator.shutdown(wait=False)
                logger.info("出站调度器已发起关闭")
            except Exception as e:
                logger.warning(f"关闭出站调度器失败: {e}")
        
        deadline = time.time() + shutdown_timeout
        for name, executor in [('reply', self._reply_executor), ('llm', self._llm_executor), ('post_send', self._post_send_executor)]:
            if executor and hasattr(executor, '_threads'):
                for thread in list(executor._threads):
                    if thread.is_alive():
                        wait_time = max(0, deadline - time.time())
                        if wait_time > 0:
                            thread.join(timeout=wait_time)
                        if thread.is_alive():
                            logger.warning(f"{name}线程池worker仍在运行: {thread.name}")
        
        logger.info("所有线程池关闭完成")
    
    def __del__(self):
        """析构函数：仅记录警告，不执行阻塞操作（线程池关闭应在stop_browser中完成）"""
        try:
            if hasattr(self, '_reply_executor') and self._reply_executor:
                if not self._reply_executor._shutdown:
                    logger.warning("BotService析构时线程池未关闭，请确保调用stop_browser()")
            if hasattr(self, '_llm_executor') and self._llm_executor:
                if not self._llm_executor._shutdown:
                    logger.warning("BotService析构时LLM线程池未关闭，请确保调用stop_browser()")
        except Exception:
            pass

    def restart_browser(self):
        """重启浏览器（通过Worker线程安全执行）"""
        self._submit_task_no_wait(self._restart_impl)

    def _open_url_in_browser_impl(self, url: str) -> dict:
        if not self.browser_manager:
            raise RuntimeError("浏览器未启动")

        page = self.browser_manager.open_url_in_new_tab(url)
        return {
            "success": True,
            "url": str(getattr(page, "url", "") or url),
        }

    def _wait_for_browser_ready(self, timeout: int = 45) -> bool:
        """等待浏览器启动完成，供需要复用浏览器上下文的动作调用。"""
        deadline = time.time() + max(1, int(timeout or 45))
        while time.time() < deadline:
            browser_ready = self._sync_browser_runtime_state()
            if browser_ready and self.browser_manager:
                return True

            state = self._get_runtime_state_snapshot()
            if state.get("browser_state") == "faulted":
                return False

            time.sleep(1)
        return False

    def open_url_in_same_browser(self, url: str, auto_start: bool = True) -> dict:
        """在“启动抖音”使用的同一浏览器中新开标签页打开链接。"""
        browser_ready = self._sync_browser_runtime_state()
        if (not browser_ready or not self.browser_manager) and auto_start:
            state = self._get_runtime_state_snapshot()
            if state.get("browser_state") not in {"starting", "running", "recovering"}:
                self.start_browser()
            browser_ready = self._wait_for_browser_ready(timeout=60)

        if not browser_ready or not self.browser_manager:
            raise RuntimeError("浏览器启动失败，请稍后重试或手动点击启动抖音")

        future = self._submit_task(self._open_url_in_browser_impl, url)
        return future.result(timeout=max(float(REPLY_LLM_TIMEOUT_SECONDS), 20.0))

    def _restart_impl(self):
        """重启浏览器实现（先停止回复，再关闭浏览器，最后重新初始化）"""
        logger.info("正在重启浏览器...")
        self._update_runtime_state(browser_state="recovering")

        self._save_state_before_shutdown()

        if self._get_runtime_state_snapshot()["is_monitoring_messages"]:
            logger.info("重启前先停止消息回复...")
            try:
                self._stop_monitoring_impl()
            except Exception as e:
                logger.warning(f"停止消息回复失败: {e}")

        try:
            if self.browser_manager:
                self.browser_manager.close()
                time.sleep(2)
        except Exception as e:
            logger.warning(f"关闭浏览器时出错: {e}")

        self._update_runtime_state(
            is_running=False,
            is_monitoring_messages=False,
            monitor_active=False,
            browser_state="recovering",
            monitoring_state="stopped",
            current_task="Idle",
            stop_flag=False,
        )
        self.browser_manager = None
        self._login_status_cache = False
        self._last_login_check_time = 0
        self._rpa_retry_count = 0
        self._rpa_recovery_attempt_time = 0
        self._rpa_last_retry_time = 0
        logger.info("登录状态缓存已重置")
        self._init_browser_instance()

    def check_login(self):
        """检查登录状态，优先使用缓存的登录状态，避免频繁检测和超时问题"""
        if not self.is_running:
            return False

        if self.current_task != "Idle":
            return True

        if self._login_status_cache and (time.time() - self._last_login_check_time < 30):
            return True

        try:
            if not self.login_handler:
                return False
            future = self._submit_task(self.login_handler.check_login_status)
            result = future.result(timeout=5)
            self._login_status_cache = result
            self._last_login_check_time = time.time()
            return result
        except Exception as e:
            logger.warning(f"检查登录状态超时或失败: {e}")
            if self._login_status_cache and (time.time() - self._last_login_check_time < 60):
                logger.info("使用缓存的登录状态")
                return True
            return False

    def _is_recently_sent_by_us(self, customer_name: str, content: str) -> bool:
        """检查消息是否是我们最近发送的，防止自回复循环——委托到 OutboundIdempotencyService。"""
        target_meta = self._get_last_send_target_meta()
        return self._get_outbound_idempotency_service().is_echo(
            customer_name,
            content,
            conversation_id=target_meta.get("conversation_id", ""),
            customer_id=target_meta.get("customer_id", ""),
            platform=target_meta.get("platform", "douyin"),
        )

    def _is_self_reply_content(self, customer_name: str, content: str) -> bool:
        """检测内容是否是系统自回复的格式——委托到 OutboundIdempotencyService。"""
        target_meta = self._get_last_send_target_meta()
        return self._get_outbound_idempotency_service().is_self_reply(
            customer_name,
            content,
            conversation_id=target_meta.get("conversation_id", ""),
            customer_id=target_meta.get("customer_id", ""),
            platform=target_meta.get("platform", "douyin"),
        )

    def _mark_sent_by_us(
        self,
        customer_name: str,
        content: str,
        conversation_id: str = "",
        *,
        customer_id: str = "",
        platform: str = "douyin",
        logical_message_id: str = "",
        trace_id: str = "",
    ):
        """标记已发送消息——委托到 OutboundIdempotencyService + RPA状态机。"""
        self._get_outbound_idempotency_service().mark_sent(
            customer_name,
            content,
            conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        self._get_outbound_recent_reply_store().record_reply(
            conversation_id=conversation_id,
            content=content,
            logical_message_id=logical_message_id,
            trace_id=trace_id,
            platform=platform,
            customer_name=customer_name,
        )
        # 主动通知 RPA 状态机：我方已发送回复，标记 sent_by_us=True
        # DOM 轮询无法可靠识别出站方向，必须由发送侧主动通知
        try:
            if (
                getattr(self, "rpa_launcher", None)
                and getattr(self.rpa_launcher, "rpa_engine", None)
            ):
                self.rpa_launcher.rpa_engine.mark_outbound_sent(customer_name, content)
        except Exception as rpa_e:
            logger.debug(f"通知RPA状态机sent_by_us失败(非致命): {rpa_e}")

    def _notify_human_handoff(self, customer_name: str, content: str, reply_content: str, conversation_id: str = ""):
        """记录人工接管事件，作为自动发送闸门的配套通知。"""
        logger.warning(f"客户 {customer_name} 命中人工接管闸门，暂停自动发送")
        try:
            self.message_bus.publish_simple(
                msg_type="human_handoff_required",
                payload={
                    "customer_name": customer_name,
                    "conversation_id": conversation_id,
                    "content": content[:200] if content else "",
                    "suggested_reply": reply_content[:200] if reply_content else "",
                }
            )
        except Exception as e:
            logger.debug(f"发布人工接管事件失败: {e}")

    def _resume_monitor_inactive_inbound_workflows(self, limit: int = 50) -> int:
        """恢复仅因监听暂时失活而暂停的入站工作流。"""
        if not self._allow_background_delivery_without_new_inbound("monitor_resume"):
            return 0
        resumed = 0
        with contextlib.suppress(Exception):
            monitoring_status = self._get_monitoring_status()
            if not bool(monitoring_status.get("effective_monitoring")):
                return 0

        try:
            paused_runs = list(self.db.list_workflow_runs(statuses=["paused"], limit=max(int(limit or 0), 1) * 4) or [])
        except Exception as e:
            logger.debug(f"读取 monitor_inactive 工作流失败: {e}")
            return 0

        for run in paused_runs:
            paused_reason = str(run.get("paused_reason", "") or "").strip()
            if not paused_reason.startswith("monitor_inactive:"):
                continue

            workflow_run_id = str(run.get("workflow_run_id", "") or "").strip()
            logical_message_id = str(run.get("logical_message_id", "") or "").strip()
            conversation_id = str(run.get("conversation_id", "") or "").strip()
            customer_name = str(run.get("customer_name", "") or "").strip()
            content = str(run.get("content", "") or "").strip()
            if not workflow_run_id or not logical_message_id or not customer_name or not content:
                continue

            inbound_event = {}
            with contextlib.suppress(Exception):
                inbound_event = self.db.get_inbound_event(logical_message_id) or {}
            inbound_status = str(inbound_event.get("status", "") or "").strip()
            if inbound_status in {"done", "skipped", "failed"}:
                continue

            payload = {
                "customer_name": customer_name,
                "content": content,
                "direction": "inbound",
                "conversation_id": conversation_id,
                "platform": str(run.get("platform", "douyin") or "douyin"),
                "customer_id": str(run.get("customer_id", "") or ""),
                "is_new": True,
                "msg_id": str(
                    inbound_event.get("source_message_id", "")
                    or run.get("source_message_id", "")
                    or ""
                ),
                "logical_message_id": logical_message_id,
                "workflow_run_id": workflow_run_id,
                "_message_already_persisted": True,
                "_resumed_from_monitor_inactive": True,
                "enterprise_id": str(
                    inbound_event.get("enterprise_id", "")
                    or run.get("enterprise_id", "")
                    or ""
                ).strip(),
                "preferred_schema_id": str(
                    inbound_event.get("preferred_schema_id", "")
                    or run.get("preferred_schema_id", "")
                    or ""
                ).strip(),
                "schema_id": str(
                    inbound_event.get("schema_id", "")
                    or run.get("schema_id", "")
                    or inbound_event.get("preferred_schema_id", "")
                    or run.get("preferred_schema_id", "")
                    or ""
                ).strip(),
                "tenant_resolution_mode": str(
                    inbound_event.get("tenant_resolution_mode", "")
                    or run.get("tenant_resolution_mode", "")
                    or ""
                ).strip(),
            }

            try:
                self.message_workflow_manager.activate_run(
                    workflow_run_id,
                    node_id="ingest",
                    metadata={"trigger": "monitor_resume", "paused_reason": paused_reason},
                )
                queued = self._submit_inbound_reply_job(payload)
            except Exception as e:
                logger.warning(f"恢复 monitor_inactive 工作流失败: workflow_run_id={workflow_run_id}, error={e}")
                queued = False

            if queued:
                resumed += 1
                logger.info(
                    f"恢复 monitor_inactive 入站工作流: workflow_run_id={workflow_run_id}, "
                    f"logical_message_id={logical_message_id}, customer={customer_name}"
                )
                if resumed >= max(int(limit or 0), 1):
                    break
                continue

            with contextlib.suppress(Exception):
                self.db.upsert_workflow_run(
                    {
                        "workflow_run_id": workflow_run_id,
                        "status": "paused",
                        "paused_reason": paused_reason,
                        "current_node": "ingest",
                    }
                )
        return resumed

    def _finalize_monitor_start_success(self) -> bool:
        """启动成功后的统一收口，顺手恢复 monitor_inactive 待办。"""
        self._last_monitor_start_error = ""
        try:
            resumed = self._resume_monitor_inactive_inbound_workflows()
            if resumed:
                logger.info(f"消息回复启动后已恢复 monitor_inactive 待办: {resumed}")
            restored_retries = self._resume_persisted_retry_workflows()
            if restored_retries:
                logger.info(f"消息回复启动后已恢复持久化补偿重试: {restored_retries}")
        except Exception as e:
            logger.warning(f"恢复 monitor_inactive 待办失败: {e}")
        return True

    # ==================== 消息回复 ====================
    
    def _evaluate_start_prerequisites(self) -> str:
        """启动消息回复的前置评估。

        [REFACTOR-INST:convergence] 7 个分支的早期 if 收口为单值返回：
        - "already_running"：已有效监听，直接成功
        - "auto_restore_handled"：自动恢复正在处理，复用结果
        - "browser_not_running"：浏览器未启动
        - "residual_state_cleared"：残留状态已重置，可继续
        - "ready_to_start"：可进入 _start_monitoring_impl
        """
        state = self._get_runtime_state_snapshot()

        if getattr(self, "_auto_restore_monitoring_pending", False):
            monitoring_status = self._get_monitoring_status()
            if monitoring_status["effective_monitoring"]:
                return "auto_restore_handled"

        if not state["is_running"]:
            return "browser_not_running"

        monitoring_status = self._get_monitoring_status()
        if state["monitoring_state"] in {"starting", "running"} and not monitoring_status["effective_monitoring"]:
            # [P7 修复] 重置残留状态后继续启动
            self._update_runtime_state(
                is_monitoring_messages=False,
                monitor_active=False,
                monitoring_state="stopped",
            )
            return "residual_state_cleared"

        if state["monitoring_state"] in {"starting", "running"} and monitoring_status["effective_monitoring"]:
            return "already_running"

        return "ready_to_start"

    def start_message_monitoring(self, *, from_auto_restore: bool = False):
        """启动消息回复（同步等待启动结果，最多30秒超时）

        [REFACTOR-INST:convergence] 主方法只做"评估 → 执行 → 收敛"，所有 7 层 if
        折叠到 _evaluate_start_prerequisites()。
        """
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        decision = self._evaluate_start_prerequisites()
        if decision == "auto_restore_handled" and not from_auto_restore:
            logger.info("自动恢复回复任务已完成接管，复用当前有效监听态")
            return self._finalize_monitor_start_success()
        if decision == "auto_restore_handled":
            # from_auto_restore=True 时即使自动恢复也已接管，直接成功
            return self._finalize_monitor_start_success()
        if decision == "browser_not_running":
            logger.warning("浏览器未启动，无法启动消息回复")
            self._last_monitor_start_error = "浏览器未启动，无法启动消息回复"
            return False
        if decision == "already_running":
            return self._finalize_monitor_start_success()
        # decision in {"residual_state_cleared", "ready_to_start"}
        if decision == "residual_state_cleared":
            logger.warning("检测到自动回复状态残留但未真实运行，已重置后继续启动")

        self._last_monitor_start_error = ""
        self._update_runtime_state(monitoring_state="starting")

        # 避免在 Worker 线程内把启动任务再次提交到同一个队列后同步等待，
        # 否则会出现"当前任务阻塞自己后续任务"的启动假死。
        if threading.current_thread() is getattr(self, "worker_thread", None):
            try:
                self._start_monitoring_impl()
            except Exception as e:
                self._update_runtime_state(monitoring_state="faulted")
                logger.error(f"Worker线程内启动消息回复失败: {e}")
                self._last_monitor_start_error = f"Worker线程内启动消息回复失败: {e}"
                return False
            if self._get_runtime_state_snapshot()["is_monitoring_messages"]:
                return self._finalize_monitor_start_success()
            self._update_runtime_state(monitoring_state="faulted")
            if not self._last_monitor_start_error:
                self._last_monitor_start_error = "消息回复启动失败，Worker线程直启后未进入运行态"
            return False

        future = self._submit_task(self._start_monitoring_impl)
        try:
            future.result(timeout=30)
        except FuturesTimeoutError:
            logger.warning("启动消息回复等待超时，进入短暂宽限轮询检查实际运行态")
            for _ in range(10):
                runtime_state = self._get_runtime_state_snapshot()
                if runtime_state["is_monitoring_messages"]:
                    return self._finalize_monitor_start_success()
                if future.done():
                    break
                time.sleep(1)
            runtime_state = self._get_runtime_state_snapshot()
            if runtime_state["is_monitoring_messages"]:
                return self._finalize_monitor_start_success()
            self._update_runtime_state(monitoring_state="faulted")
            if future.done():
                try:
                    future.result()
                except Exception as inner_e:
                    logger.error(f"启动消息回复失败: {inner_e}")
                    self._last_monitor_start_error = f"启动消息回复失败: {inner_e}"
                    return False
            logger.error("启动消息回复超时且宽限期内未进入运行态")
            self._last_monitor_start_error = "启动消息回复超时且宽限期内未进入运行态"
            return False
        except Exception as e:
            self._update_runtime_state(monitoring_state="faulted")
            logger.error(f"启动消息回复超时或失败: {e}")
            self._last_monitor_start_error = f"启动消息回复超时或失败: {e}"
            return False

        monitoring_status = self._get_monitoring_status()
        if monitoring_status["effective_monitoring"]:
            return self._finalize_monitor_start_success()

        self._update_runtime_state(
            is_monitoring_messages=False,
            monitor_active=False,
            monitoring_state="faulted",
        )
        if not self._last_monitor_start_error:
            self._last_monitor_start_error = "消息回复未进入有效监听状态，请确认已登录且聊天页可见"
        return False

    def _refresh_rpa_monitoring_readiness(self) -> bool:
        """强制刷新 RPA 监听就绪态，避免启动成功与状态查询口径不一致。"""
        if not (self.rpa_launcher and self.rpa_launcher.rpa_engine):
            return False

        engine = self.rpa_launcher.rpa_engine
        try:
            if hasattr(engine, "_last_login_check"):
                engine._last_login_check = 0
            if hasattr(engine, "_check_login"):
                engine._check_login()
        except Exception as readiness_error:
            logger.debug(f"刷新RPA监听就绪态失败: {readiness_error}")

        return bool(self._get_monitoring_status()["effective_monitoring"])

    def _retry_rpa_init(self) -> bool:
        """尝试重新初始化RPA引擎（单次尝试，不阻塞Worker线程）
        
        Returns:
            bool: 重试是否成功
        """
        if not self._use_rpa_mode:
            return False

        if self.rpa_launcher and self.rpa_launcher.rpa_engine:
            return True

        if not hasattr(self, '_rpa_retry_count'):
            self._rpa_retry_count = 0
            self._rpa_last_retry_time = 0

        current_time = time.time()
        if current_time - self._rpa_last_retry_time < 30:
            return False

        if self._rpa_retry_count >= 3:
            logger.error(f"RPA引擎重试已达上限({self._rpa_retry_count}次)，不再重试")
            return False

        self._rpa_last_retry_time = current_time
        self._rpa_retry_count += 1

        try:
            logger.info(f"RPA引擎重试初始化 ({self._rpa_retry_count}/3)...")
            if self.rpa_launcher is None:
                if not self.browser_manager:
                    logger.warning("browser_manager未初始化，无法重试RPA")
                    return False
                # [DEBUG-INST:greeting-no-reply] 使用 browser_manager.get_monitor_page()
                # 该方法会自动创建新页面（如果不存在），而 ChatSessionCoordinator.get_monitor_page()
                # 只是被动读取引用，stop 后 monitor_page 已被清空就会返回 None
                monitor_page = self.browser_manager.get_monitor_page()
                if not monitor_page:
                    logger.warning("获取monitor_page失败，无法重试RPA")
                    return False
                self.rpa_launcher = RPALauncher(
                    page=monitor_page,
                    browser_manager=self.browser_manager,
                    database=self.db
                )
            else:
                old_launcher = self.rpa_launcher
                try:
                    if hasattr(old_launcher, 'cleanup'):
                        old_launcher.cleanup()
                except Exception as cleanup_e:
                    logger.debug(f"清理旧RPA启动器失败: {cleanup_e}")
            self.rpa_launcher.setup()
            if self.rpa_launcher.rpa_engine:
                self.rpa_launcher.rpa_engine.message_monitor = self.message_monitor
                logger.info(f"RPA引擎重试初始化成功 (第{self._rpa_retry_count}次)")
                self._rpa_retry_count = 0
                return True
            else:
                logger.warning(f"RPA引擎重试初始化后rpa_engine仍为None (第{self._rpa_retry_count}次)")
        except Exception as e:
            logger.warning(f"RPA引擎重试初始化失败 (第{self._rpa_retry_count}次): {e}")

        return False

    def _start_monitoring_impl(self):
        """启动消息回复实现

        [简化] 收敛前：started 为 False 时只设置 faulted 但不 return，后续 if started 保护；
        收敛后：started 为 False 时直接 return，避免冗余的 if 分支。
        状态自动保存的 lambda 从 4 层嵌套 hasattr 简化为本地函数。
        """
        # 启动前重置 RPA 重试计数，确保 stop->start 循环不会因旧重试计数耗尽而永久失败
        if hasattr(self, "_rpa_retry_count"):
            self._rpa_retry_count = 0
        if hasattr(self, "_rpa_last_retry_time"):
            self._rpa_last_retry_time = 0
        if self.browser_manager:
            self.browser_manager.set_monitoring_active(True)

        started = self._get_inbound_source_adapter().start_monitoring_sources()
        if not started:
            self._update_runtime_state(
                is_monitoring_messages=False,
                monitor_active=False,
                monitoring_state="faulted",
            )
            self._last_monitor_start_error = "消息回复启动失败，所有模式均未成功启动"
            logger.error("消息回复启动失败，所有模式均未成功启动")
            return

        # started 为 True：启动轮询线程和状态自动保存
        self._last_monitor_start_error = ""
        self._start_monitor_poll_thread()
        try:
            # [简化] 收敛 4 层嵌套 hasattr 为本地函数
            def _get_persist_state():
                engine = None
                guard = None
                launcher = getattr(self, "rpa_launcher", None)
                if launcher and getattr(launcher, "rpa_engine", None):
                    engine = launcher.rpa_engine
                    guard = getattr(engine, "_boundary_guard", None)
                return (self, engine, guard, self.session_manager)

            self._state_persistor.start_auto_save(
                interval=30,
                get_state_callback=_get_persist_state,
            )
        except Exception as auto_e:
            logger.warning(f"启动状态自动保存失败: {auto_e}")

    def stop_message_monitoring(self, reason: str = ""):
        """停止消息回复

        [简化] 收敛前：在 stop_message_monitoring 中提前清除 RPA 回调，
        但 _stop_monitoring_impl 是异步执行的，可能导致回调被清除后引擎仍在运行。
        收敛后：只设置状态标记，RPA 回调清除移到 _stop_monitoring_impl 中统一处理。
        """
        reason_text = str(reason or "").strip()
        if reason_text:
            logger.info(f"停止消息回复请求... source={reason_text}")
        else:
            logger.info("停止消息回复请求...")
        self._update_runtime_state(
            is_monitoring_messages=False,
            monitor_active=False,
            monitoring_state="stopping",
        )
        self._last_monitor_stop_reason = reason_text

        try:
            self._state_persistor.stop_auto_save()
        except Exception:
            pass

        self._submit_task_no_wait(self._stop_monitoring_impl)

    def _freeze_pending_auto_reply_work_items(self, reason: str = "") -> Dict[str, int]:
        """冻结已持久化的自动补偿待办，避免停止监听后仍继续自动发送。"""
        freeze_reason = f"auto_reply_stopped:{str(reason or '').strip()}".rstrip(":")
        frozen_outbox = 0
        frozen_workflow = 0
        cancelled_timers = self._cancel_all_pending_retry_timers()

        try:
            pending_outbox = list(self.db.list_outbox_events(statuses=["retry_pending"], limit=1000) or [])
        except Exception as outbox_e:
            logger.debug(f"读取 retry_pending 出站事件失败: {outbox_e}")
            pending_outbox = []

        for event in pending_outbox:
            outbox_id = str(event.get("outbox_id", "") or "").strip()
            if not outbox_id:
                continue
            try:
                self.db.update_outbox_event(
                    outbox_id,
                    status="cancelled",
                    reason=freeze_reason[:100],
                    next_retry_at="",
                )
                frozen_outbox += 1
            except Exception as update_e:
                logger.debug(f"冻结出站补偿事件失败: outbox_id={outbox_id}, error={update_e}")

        try:
            waiting_runs = list(self.db.list_workflow_runs(statuses=["waiting_retry"], limit=1000) or [])
        except Exception as workflow_e:
            logger.debug(f"读取 waiting_retry 工作流失败: {workflow_e}")
            waiting_runs = []

        for run in waiting_runs:
            workflow_run_id = str(run.get("workflow_run_id", "") or "").strip()
            if not workflow_run_id:
                continue
            try:
                self.db.upsert_workflow_run(
                    {
                        "workflow_run_id": workflow_run_id,
                        "status": "paused",
                        "paused_reason": freeze_reason[:200],
                    }
                )
                frozen_workflow += 1
            except Exception as update_e:
                logger.debug(f"冻结 waiting_retry 工作流失败: workflow_run_id={workflow_run_id}, error={update_e}")

        if frozen_outbox or frozen_workflow or cancelled_timers:
            logger.info(
                "停止自动回复时已冻结后台待办: "
                f"retry_pending={frozen_outbox} waiting_retry={frozen_workflow} "
                f"cancelled_retry_timers={cancelled_timers}"
            )

        return {"outbox": frozen_outbox, "workflow": frozen_workflow, "timers": cancelled_timers}

    def stop_message_monitoring_sync(self, reason: str = "", timeout_seconds: float = 8.0) -> bool:
        """同步停止消息回复，确保接口返回前尽量等到真实停下。"""
        self.stop_message_monitoring(reason=reason)

        deadline = time.monotonic() + max(float(timeout_seconds or 0), 0.5)
        while time.monotonic() < deadline:
            monitoring_status = self._get_monitoring_status()
            runtime_state = self._get_runtime_state_snapshot()
            if (
                not bool(monitoring_status.get("effective_monitoring"))
                and runtime_state.get("monitoring_state") == "stopped"
            ):
                return True
            time.sleep(0.1)

        logger.warning(
            "同步停止消息回复等待超时: "
            f"effective={self._get_monitoring_status().get('effective_monitoring')} "
            f"state={self._get_runtime_state_snapshot().get('monitoring_state')}"
        )
        return False

    def _stop_monitoring_impl(self):
        """停止消息回复实现（先设置状态标记阻止新消息，再停止消息源，最后清理缓存）"""
        logger.info("执行停止消息回复...")
        if self.browser_manager:
            self.browser_manager.set_monitoring_active(False)
        self._stop_monitor_poll_thread()
        self._update_runtime_state(
            monitor_active=False,
            is_monitoring_messages=False,
            monitoring_state="stopped",
        )

        # [简化] RPA 回调清除移到这里统一处理，避免 stop_message_monitoring 提前清除导致状态不一致
        if self._use_rpa_mode and self.rpa_launcher and self.rpa_launcher.rpa_engine:
            try:
                self.rpa_launcher.rpa_engine.register_message_callback(None)
                self.rpa_launcher.rpa_engine.register_state_callback(lambda *_: None)
                logger.info("RPA消息回调已清除")
            except Exception as e:
                logger.debug(f"清除RPA回调失败: {e}")

        self._get_inbound_idempotency_service().cleanup()
        self._get_outbound_idempotency_service()._evict_if_needed()
        with self._reply_locks_lock:
            keys_to_remove = []
            for k, entry in list(self._reply_locks.items()):
                # Python 3.10+ 的 threading.RLock 才有 .locked() 方法，之前的版本不存在。
                # 用 try/except 兼容：没有 .locked() 时假设锁未被持有（依赖 ref_count <= 0 已
                # 足够保证安全，且 _reply_locks_lock 持有期间其他线程不会修改 ref_count）。
                try:
                    is_locked = bool(entry['lock'].locked())  # type: ignore[attr-defined]
                except AttributeError:
                    is_locked = False
                if entry.get('ref_count', 0) <= 0 and not is_locked:
                    keys_to_remove.append(k)
            for k in keys_to_remove:
                del self._reply_locks[k]
            if self._reply_locks:
                logger.warning(f"停止回复时有 {len(self._reply_locks)} 个锁仍在使用中，已保留")
        self._rpa_retry_count = 0
        self._rpa_recovery_attempt_time = 0

        logger.info("消息回复已停止，缓存已清理")
        self._freeze_pending_auto_reply_work_items(
            reason=str(getattr(self, "_last_monitor_stop_reason", "") or "stop_monitoring")
        )

        self._get_inbound_source_adapter().stop_monitoring_sources()

    def _get_monitoring_status(self) -> Dict[str, bool]:
        """获取统一的自动回复状态
        
        修复：提供统一的状态查询接口，避免状态不一致
        """
        return self._get_inbound_source_adapter().get_monitoring_status()

    def _start_monitor_poll_thread(self):
        """启动消息轮询独立守护线程"""
        if self._monitor_poll_thread and self._monitor_poll_thread.is_alive():
            logger.info("消息轮询线程已在运行")
            return
        
        self._monitor_poll_running = True
        self._monitor_poll_stop_event.clear()
        self._monitor_poll_thread = threading.Thread(
            target=self._monitor_poll_loop,
            daemon=True,
            name="monitor_poll_thread"
        )
        self._monitor_poll_thread.start()
        logger.info("消息轮询独立守护线程已启动")

    def _stop_monitor_poll_thread(self):
        """停止消息轮询独立守护线程"""
        self._monitor_poll_running = False
        self._monitor_poll_stop_event.set()
        if self._monitor_poll_thread and self._monitor_poll_thread.is_alive():
            self._monitor_poll_thread.join(timeout=5)
        self._monitor_poll_thread = None
        logger.info("消息轮询独立守护线程已停止")

    def _monitor_poll_loop(self):
        """消息轮询定时器线程

        定时设置轮询信号，由Worker线程在安全上下文中执行实际的Playwright操作。
        此线程不直接调用任何Playwright API，避免greenlet线程切换错误。

        轮询频率：
        - RPA模式：2秒间隔
        - 传统模式（空闲）：3秒间隔
        - 传统模式（有爬取任务）：5秒间隔
        """
        logger.info("消息轮询定时器线程开始")
        
        while self._monitor_poll_running:
            try:
                if self._monitor_poll_stop_event.wait(timeout=0.5):
                    break
                
                state = self._get_runtime_state_snapshot()
                if not state["is_monitoring_messages"]:
                    continue
                
                current_time = time.time()

                if self._use_rpa_mode:
                    if current_time - state["last_monitor_check_time"] < 2:
                        continue
                else:
                    task_running = state["current_task"] != "Idle"
                    if task_running:
                        if current_time - state["last_monitor_check_time"] < 5:
                            continue
                    else:
                        if current_time - state["last_monitor_check_time"] < 3:
                            continue
                
                self._update_runtime_state(last_monitor_check_time=current_time)
                self._poll_signal.set()
                logger.debug("轮询定时器: 已设置轮询信号，等待Worker线程执行")
                
            except Exception as e:
                import traceback
                logger.error(f"消息轮询定时器线程异常: {e}\n{traceback.format_exc()}")
                time.sleep(1)
        
        logger.info("消息轮询定时器线程结束")

    def _poll_message_monitor(self):
        """轮询消息回复（在Worker线程中执行，保证Playwright线程安全）
        
        由轮询定时器线程通过_poll_signal触发，Worker线程检查信号后调用此方法。
        所有Playwright操作在此方法中执行，确保greenlet线程安全。
        """
        if not self._get_runtime_state_snapshot()["is_monitoring_messages"]:
            return
        self._get_inbound_source_adapter().poll_once()

    def _is_invalid_message(self, content: str) -> bool:
        """检查消息是否为无效消息（统一过滤规则）
        
        修复：统一RPA模式和传统模式的过滤规则
        增加：乱码/截断片段检测，过滤包含非正常字符的消息
        """
        if any(pattern in content for pattern in self.INVALID_MESSAGE_PATTERNS):
            return True
        stripped = content.strip()
        if stripped in ('在线', '离线', '点击'):
            return True
        if len(content) >= 2:
            non_cjk_printable = 0
            for ch in content:
                cp = ord(ch)
                if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                    0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF or
                    0x0020 <= cp <= 0x007E or
                    '\u4e00' <= ch <= '\u9fff'):
                    non_cjk_printable += 1
                elif ch in '，。！？、；：""''【】（）《》—…·～':
                    non_cjk_printable += 1
            ratio = non_cjk_printable / len(content)
            if ratio < 0.5:
                logger.info(f"乱码检测: 消息含大量非正常字符(可读率{ratio:.0%})，跳过: {content[:30]}")
                return True
        return False

    def _looks_like_bot_message(self, content: str) -> bool:
        """统一识别机器人/模板化自回复内容，避免多入口维护两套判断。"""
        is_likely_bot_message = any(pattern in content for pattern in self.BOT_MESSAGE_PATTERNS)
        if is_likely_bot_message:
            return True
        import re

        return any(re.search(pat, content) for pat in self.BOT_MESSAGE_REGEX_PATTERNS)

    def _route_live_inbound_message(
        self,
        *,
        source: str,
        customer_name: str,
        content: str,
        direction: Any = "inbound",
        is_new: bool = True,
        conversation_id: str = "",
        platform: str = "douyin",
        customer_id: str = "",
        msg_id: str = "",
        timestamp: Any = "",
        signal_source: str = "",
        apply_echo_guard: bool = False,
        apply_self_reply_guard: bool = False,
        direction_confidence: str = "high",
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        schema_id: str = "",
        tenant_resolution_mode: str = "",
    ) -> tuple[bool, str]:
        return self._get_inbound_pipeline_service().route_live_inbound_message(
            source=source,
            customer_name=customer_name,
            content=content,
            direction=direction,
            is_new=is_new,
            conversation_id=conversation_id,
            platform=platform,
            customer_id=customer_id,
            msg_id=msg_id,
            timestamp=timestamp,
            signal_source=signal_source,
            apply_echo_guard=apply_echo_guard,
            apply_self_reply_guard=apply_self_reply_guard,
            direction_confidence=direction_confidence,
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
            schema_id=schema_id,
            tenant_resolution_mode=tenant_resolution_mode,
        )

    def _extract_field(self, obj: Any, field_name: str, default: Any = None) -> Any:
        """安全提取对象或字典中的字段值"""
        if isinstance(obj, dict):
            return obj.get(field_name, default)
        return getattr(obj, field_name, default)

    @staticmethod
    def _normalize_direction(raw_direction: Any, default: str = "unknown") -> str:
        from src.common.utils import normalize_direction
        return normalize_direction(raw_direction, default)

    @staticmethod
    def _is_synthetic_message_id(message_id: str) -> bool:
        text = str(message_id or "").strip().lower()
        if not text:
            return True
        return text.startswith(("dom_", "msg_", "auto_", "synth_"))

    def _build_source_specific_inbound_logical_message_id(self, logical_message_id: str, source_message_id: str) -> str:
        source_hash = hashlib.md5(str(source_message_id or "").encode("utf-8")).hexdigest()[:8]
        return f"{logical_message_id}_src_{source_hash}"

    def _should_split_duplicate_inbound_event(
        self,
        *,
        existing_event: Optional[dict],
        incoming_source_message_id: str,
    ) -> bool:
        incoming_id = str(incoming_source_message_id or "").strip()
        if self._is_synthetic_message_id(incoming_id):
            return False

        existing_source_message_id = str((existing_event or {}).get("source_message_id", "") or "").strip()
        if self._is_synthetic_message_id(existing_source_message_id):
            return False

        return existing_source_message_id != incoming_id

    def _is_non_user_message(self, message: Any) -> bool:
        """识别系统/助手/己方消息，避免误入入站主链。"""
        role_candidates = [
            self._extract_field(message, "role", ""),
            self._extract_field(message, "sender_role", ""),
            self._extract_field(message, "sender_type", ""),
            self._extract_field(message, "message_type", ""),
            self._extract_field(message, "source", ""),
        ]
        for raw in role_candidates:
            if not raw:
                continue
            text = str(raw).strip().lower()
            if any(token in text for token in ("system", "notice", "notification", "通知")):
                return True
            if any(token in text for token in ("assistant", "agent", "bot", "客服", "官方")):
                return True
            if text in ("service", "auto_service", "auto_reply"):
                return True
        return False

    @staticmethod
    def _make_conversation_id(
        customer_name: str,
        platform: str = "douyin",
        customer_id: str = "",
    ) -> str:
        """生成稳定会话ID，优先使用平台客户ID，缺失时退回昵称哈希。"""
        return build_conversation_id(customer_name, platform, customer_id)

    def _resolve_conversation_id(
        self,
        customer_name: str,
        platform: str = "douyin",
        customer_id: str = "",
        explicit_conversation_id: str = "",
    ) -> str:
        """兼容历史数据，优先显式会话ID，其次沿用已有旧会话。"""
        return resolve_conversation_id(
            db=getattr(self, "db", None),
            customer_name=customer_name,
            platform=platform,
            customer_id=customer_id,
            explicit_conversation_id=explicit_conversation_id,
        )

    @staticmethod
    def _build_logical_message_id(**kwargs) -> str:
        return build_logical_message_id(**kwargs)

    def _refresh_follow_up_reminder(self, conversation_id: str, messages: list | None = None) -> None:
        """Refresh follow-up qualification and send Enterprise WeChat reminder if newly triggered."""
        try:
            conversation = next(
                (
                    item
                    for item in (self._get_chat_store().get_all_conversations_dicts() or [])
                    if str(item.get("conversation_id") or "").strip() == str(conversation_id or "").strip()
                ),
                None,
            )
            if not conversation:
                return

            if messages is None:
                messages = self._get_chat_store().get_recent_messages_dicts(conversation_id, limit=9999)
            sanitized_messages = self.db.sanitize_message_records(messages or [])

            from src.common.follow_up_reminder_service import get_follow_up_reminder_service

            get_follow_up_reminder_service().evaluate_and_notify_conversation(
                conversation,
                sanitized_messages,
            )
        except Exception as exc:
            logger.debug("刷新待跟进提醒失败: %s", exc)

    def submit_inbound_message(self, message: Any) -> tuple[bool, str]:
        return self._get_inbound_pipeline_service().submit_inbound_message(message)

    def _build_processed_message_cache_keys(
        self,
        *,
        customer_name: str,
        content: str,
        msg_id: str = "",
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
    ) -> List[str]:
        return self._get_inbound_idempotency_service().build_cache_keys(
            customer_name=customer_name,
            content=content,
            msg_id=msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
        )

    def _is_message_processed(
        self,
        customer_name: str,
        content: str,
        msg_id: str = "",
        *,
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
        direction: str = "",
    ) -> bool:
        return self._get_inbound_idempotency_service().is_message_processed(
            customer_name,
            content,
            msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
            direction=direction,
        )
    
    def _is_message_done(
        self,
        customer_name: str,
        content: str,
        msg_id: str = "",
        *,
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
    ) -> bool:
        return self._get_inbound_idempotency_service().is_message_done_status(
            customer_name,
            content,
            msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
        )
    
    def _mark_message_done(
        self,
        customer_name: str,
        content: str,
        msg_id: str = "",
        *,
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
    ):
        self._get_inbound_idempotency_service().mark_done(
            customer_name,
            content,
            msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
        )

    def _mark_message_skipped(
        self,
        customer_name: str,
        content: str,
        reason: str = "",
        msg_id: str = "",
        *,
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
    ):
        self._get_inbound_idempotency_service().mark_skipped(
            customer_name,
            content,
            reason,
            msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
        )

    def _mark_message_failed(
        self,
        customer_name: str,
        content: str,
        msg_id: str = "",
        *,
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
        retry_count: int = 0,
    ):
        self._get_inbound_idempotency_service().mark_failed(
            customer_name,
            content,
            msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
            retry_count=retry_count,
        )

    def _has_exhausted_outbox_retries(self, retry_count: int) -> bool:
        return int(retry_count or 0) > self.MAX_OUTBOX_RETRY_COUNT

    def clear_message_failed_status(
        self,
        customer_name: str,
        content: str,
        *,
        msg_id: str = "",
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
    ):
        """清除消息的失败状态，允许重新处理（支持failed_N和done状态）"""
        if not content:
            return
        cache_keys = self._build_processed_message_cache_keys(
            customer_name=customer_name,
            content=content,
            msg_id=msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
        )
        idempotency = self._get_inbound_idempotency_service()
        removed = 0
        with idempotency._lock:
            for key in cache_keys:
                if key not in idempotency._cache:
                    continue
                _, status = idempotency._cache[key]
                if status.startswith("failed_") or status == "done":
                    del idempotency._cache[key]
                    idempotency._source_content_hash_map.pop(key, None)
                    removed += 1
                else:
                    logger.debug(f"消息状态无需清除: {customer_name}, key={key}, 状态: {status}")
        if removed:
            logger.info(f"已清除消息状态: customer={customer_name}, removed={removed}")
        else:
            logger.debug(f"消息不存在于缓存中: {customer_name}")

    def clear_customer_all_cache(self, customer_name: str):
        """清除指定用户的所有消息处理缓存，允许重新处理"""
        prefix = f"{customer_name}:"
        cleared_count = 0
        idempotency = self._get_inbound_idempotency_service()
        with idempotency._lock:
            keys_to_remove = [
                k for k in idempotency._cache
                if k.startswith(prefix) or f":cust:{customer_name}:" in k
            ]
            for k in keys_to_remove:
                del idempotency._cache[k]
                idempotency._source_content_hash_map.pop(k, None)
                cleared_count += 1
        if cleared_count > 0:
            logger.info(f"已清除用户 {customer_name} 的所有消息缓存({cleared_count}条)")
        return cleared_count

    def _can_accept_rpa_callback_messages(self) -> tuple[bool, str]:
        """统一 RPA 回调入口的监听态判定口径。"""
        try:
            monitoring_status = self._get_monitoring_status()
            if bool(monitoring_status.get("effective_monitoring")):
                return True, "effective_monitoring"
        except Exception as e:
            logger.debug(f"读取回调监听状态失败: {e}")

        runtime_state = {}
        with contextlib.suppress(Exception):
            runtime_state = self._get_runtime_state_snapshot()

        monitoring_state = str(runtime_state.get("monitoring_state", "") or "").strip().lower()
        if bool(runtime_state.get("is_monitoring_messages")) and monitoring_state in {"starting", "running"}:
            return True, f"runtime_{monitoring_state}"
        return False, monitoring_state or "inactive"

    def _on_rpa_message(self, messages):
        """RPA引擎新消息回调（增强异常处理）
        
        增强异常处理机制：
        1. 外层异常捕获，防止整个回调链路中断
        2. 单条消息异常隔离，不影响其他消息处理
        3. 异常重试机制，提高系统容错性
        4. 详细错误日志，便于问题定位
        5. 服务停止时安全退出，防止线程池已关闭后提交任务
        """
        if not messages:
            return

        callback_allowed, callback_reason = self._can_accept_rpa_callback_messages()
        if not callback_allowed:
            logger.info(
                f"监听未处于有效回调态，RPA消息回调暂存待恢复: reason={callback_reason}, "
                f"count={len(messages)}"
            )
            for msg in messages:
                customer_name = getattr(msg, 'customer_name', None) or (msg.get('customer_name', '') if isinstance(msg, dict) else '')
                content = getattr(msg, 'content', None) or (msg.get('content', '') if isinstance(msg, dict) else '')
                conversation_id = getattr(msg, 'conversation_id', None) or (msg.get('conversation_id', '') if isinstance(msg, dict) else '')
                if customer_name and content:
                    try:
                        self._get_inbound_idempotency_service().mark_failed(
                            customer_name, content,
                            getattr(msg, 'msg_id', '') or (msg.get('msg_id', '') if isinstance(msg, dict) else ''),
                            conversation_id=conversation_id,
                            retry_count=0,
                        )
                    except Exception:
                        pass
                    try:
                        adapter = self._get_inbound_source_adapter()
                        normalized = adapter.collect_rpa_callback_inbound_events([msg])
                        for evt in normalized:
                            self._get_inbound_pipeline_service()._queue_monitor_inactive_inbound(
                                evt, monitor_reason=callback_reason
                            )
                    except Exception as persist_e:
                        logger.debug(f"暂存被拒RPA消息失败: {persist_e}")
            return
        
        if hasattr(self, '_reply_executor') and self._reply_executor:
            try:
                if getattr(self._reply_executor, '_shutdown', False):
                    logger.debug("回复线程池已关闭，忽略RPA消息回调")
                    return
            except Exception:
                pass
            
        logger.debug(f"开始处理RPA消息，数量: {len(messages)}")
        
        # 外层异常捕获，防止整个回调链路中断
        try:
            processed_count = 0
            error_count = 0

            adapter = self._get_inbound_source_adapter()
            normalized_events = adapter.collect_rpa_callback_inbound_events(messages)

            for event in normalized_events:
                try:
                    customer_name = self._extract_field(event, 'customer_name', '')
                    content = self._extract_field(event, 'content', '')
                    raw_direction = self._extract_field(event, 'direction', 'inbound')
                    msg_id = self._extract_field(event, 'msg_id', '')

                    logger.debug(f"收到RPA消息: {customer_name} direction={raw_direction} -> {(content or '')[:30]}...")

                    dispatch_results = adapter.dispatch_detected_inbound_events(
                        [event],
                        source="RPA消息回调",
                        apply_echo_guard=False,
                        apply_self_reply_guard=False,
                    )
                    submitted = bool(dispatch_results and self._extract_field(dispatch_results[0], "submitted", False))
                    reason = (
                        str(self._extract_field(dispatch_results[0], "reason", "") or "")
                        if dispatch_results
                        else "invalid"
                    )
                    if submitted:
                        processed_count += 1
                        logger.debug(f"消息处理成功: customer={customer_name}")
                    elif reason == "duplicate":
                        logger.debug(f"重复入站消息已跳过: customer={customer_name}")
                    else:
                        error_count += 1
                        logger.warning(f"消息处理失败: customer={customer_name}")
                        
                except Exception as e:
                    error_count += 1
                    logger.error(f"处理单条RPA消息失败: {e}", exc_info=True)
                    # 标记消息为失败，防止重复处理
                    try:
                        customer_name = self._extract_field(event, 'customer_name', '')
                        content = self._extract_field(event, 'content', '')
                        msg_id = self._extract_field(event, 'msg_id', '')
                        conversation_id = self._extract_field(event, 'conversation_id', '')
                        customer_id = self._extract_field(event, 'customer_id', self._extract_field(event, 'sender_id', ''))
                        logical_message_id = self._extract_field(event, 'logical_message_id', '')
                        if customer_name and content:
                            self._mark_message_failed(
                                customer_name,
                                content,
                                msg_id,
                                conversation_id=conversation_id,
                                customer_id=customer_id,
                                logical_message_id=logical_message_id,
                                source_message_id=msg_id,
                            )
                    except Exception as mark_e:
                        logger.warning(f"标记失败消息异常: {mark_e}")
            
            # 记录处理统计
            if processed_count > 0 or error_count > 0:
                logger.info(f"RPA消息处理完成: 成功{processed_count}条, 失败{error_count}条")
                
        except Exception as outer_e:
            logger.error(f"RPA消息回调外层异常: {outer_e}", exc_info=True)
    
    def _submit_inbound_reply_job(self, msg_dict, max_retries=3):
        return self._get_inbound_pipeline_service().submit_inbound_reply_job(
            msg_dict,
            max_retries=max_retries,
        )

    def _on_rpa_state(self, state, message):
        """RPA引擎状态变化回调"""
        try:
            state_value = getattr(state, 'value', state)

            if state_value == 'logged_out':
                logger.warning("RPA引擎检测到登录失效，停止消息回复并保留当前抖音页面")
                self._login_status_cache = False
                runtime_state = self._get_runtime_state_snapshot()
                if runtime_state.get("monitoring_state") not in {"stopping", "stopped"}:
                    try:
                        self.stop_message_monitoring(reason="rpa:logged_out")
                    except Exception as stop_e:
                        logger.error(f"停止监听失败，仍将状态置为stopped: {stop_e}")
                self._update_runtime_state(browser_state="running", monitoring_state="stopped")

            elif state_value == 'exception':
                logger.error(f"RPA引擎页面异常: {message}")

        except Exception as e:
            logger.error(f"处理RPA状态回调失败: {e}")
            try:
                self._update_runtime_state(monitoring_state="stopped")
            except Exception:
                pass

    def _persist_inbound_message(self, message):
        self._get_inbound_pipeline_service().persist_inbound_message(message)

    def _on_new_message(self, message):
        """兼容旧命名，统一转发到入站持久化节点。"""
        self._persist_inbound_message(message)


    def _execute_inbound_reply_job(self, message: dict, _retry_depth: int = 0):
        self._get_inbound_pipeline_service().execute_inbound_reply_job(
            message,
            _retry_depth=_retry_depth,
        )

    def _delayed_reply_retry(self, message: dict, delay: float, _retry_depth: int = 0):
        """延迟后重试自动回复（使用Timer异步延迟，避免阻塞线程池worker）"""
        monitoring_available, _ = self._can_accept_inbound_auto_reply()
        if not monitoring_available:
            logger.info(f"自动回复未运行，放弃重试: {message.get('customer_name', '')}")
            return
        retry_key = self._resolve_retry_timer_key_from_message(message)
        timer = threading.Timer(
            delay,
            self._execute_delayed_retry_if_allowed,
            kwargs={"message": dict(message), "_retry_depth": _retry_depth},
        )
        timer.daemon = True
        self._register_pending_retry_timer(retry_key, timer)
        timer.start()

    def _get_reply_lock(self, conversation_id: str) -> threading.RLock:
        """获取会话级回复锁，确保同一会话的回复串行执行
        
        修复TOCTOU竞态：使用引用计数代替locked()检查，避免检查与删除之间的竞态条件。
        每个锁维护一个使用计数，只有计数为0时才允许清理。
        """
        with self._reply_locks_lock:
            if conversation_id in self._reply_locks:
                lock_entry = self._reply_locks[conversation_id]
                lock_entry['ref_count'] += 1
                return lock_entry['lock']
            
            if len(self._reply_locks) > self._reply_locks_max_size:
                keys_to_remove = []
                for k, entry in list(self._reply_locks.items()):
                    # Python 3.10+ 的 threading.RLock 才有 .locked() 方法，之前的版本不存在。
                    # 用 try/except 兼容：没有 .locked() 时假设锁未被持有（依赖 ref_count <= 0 已
                    # 足够保证安全，且 _reply_locks_lock 持有期间其他线程不会修改 ref_count）。
                    try:
                        is_locked = bool(entry['lock'].locked())  # type: ignore[attr-defined]
                    except AttributeError:
                        is_locked = False
                    if entry['ref_count'] <= 0 and not is_locked:
                        keys_to_remove.append(k)
                        if len(keys_to_remove) >= self._reply_locks_max_size // 2:
                            break
                if not keys_to_remove and len(self._reply_locks) > self._reply_locks_max_size * 2:
                    logger.warning(f"回复锁数量严重超标({len(self._reply_locks)})，但所有锁均在使用中，无法安全清理")
                for k in keys_to_remove:
                    del self._reply_locks[k]
            
            new_lock = threading.RLock()
            self._reply_locks[conversation_id] = {
                'lock': new_lock,
                'ref_count': 1
            }
            return new_lock

    def _release_reply_lock_ref(self, conversation_id: str):
        """释放会话锁的引用计数，与_get_reply_lock配对使用"""
        with self._reply_locks_lock:
            if conversation_id in self._reply_locks:
                self._reply_locks[conversation_id]['ref_count'] -= 1

    def _sanitize_reply_content(self, reply_content: str) -> str:
        """清理回复内容，移除知识库原始格式标记和无效内容"""
        if not reply_content:
            return reply_content
        reply_content = str(reply_content).replace("[FALLBACK]", "")
        # [FIX-INST:empty-line] 增强 strip：去除所有 Unicode 空白字符
        # Python str.strip() 只去 ASCII 空白（\t\n\r ），但 LLM 返回的内容可能含：
        #   - 全角空格 U+3000 "　"（中文输入法常用）
        #   - 半角不间断空格 U+00A0
        #   - 零宽字符 U+200B / U+200C / U+200D / U+FEFF
        #   - 各种 Unicode 空格（U+1680/U+2000-U+200A/U+2028/U+2029/U+202F/U+205F）
        # 这些字符会被 contenteditable 视为"内容"写入 <div> 您好</div>，
        # 抖音 IM 渲染时显示为视觉空行（前导空白 + div 的 line-height）。
        # 收敛前：只 .strip()（去 ASCII 空白），全角/零宽字符残留导致消息前空一行
        # 收敛后：用正则去除所有 Unicode 空白字符前缀/后缀
        unicode_leading_pattern = (
            r"^[\s\u00A0\u1680\u2000-\u200A\u200B-\u200D\u2028\u2029\u202F\u205F\u3000\uFEFF]+"
        )
        unicode_trailing_pattern = (
            r"[\s\u00A0\u1680\u2000-\u200A\u200B-\u200D\u2028\u2029\u202F\u205F\u3000\uFEFF]+$"
        )
        reply_content = re.sub(unicode_leading_pattern, "", reply_content)
        reply_content = re.sub(unicode_trailing_pattern, "", reply_content)
        # 兼容老逻辑：再次做 ASCII strip
        reply_content = reply_content.strip()
        lowered_reply = reply_content.lower()
        hidden_reasoning_markers = [
            "thinking process",
            "analyze the request",
            "**role:**",
            "**task:**",
            "**industry:**",
            "**constraints:**",
            "professional sales consultant",
            "general service sales",
            "travel/tourism",
            "tourism/travel",
        ]
        matched_reasoning_markers = sum(1 for marker in hidden_reasoning_markers if marker in lowered_reply)
        if matched_reasoning_markers >= 2:
            logger.warning("回复包含提示词/思维链泄漏内容，已拦截")
            return ""
        reply_content = re.sub(r"^\s*(?:回复|答复|参考回复)[：:]\s*", "", reply_content).strip()
        reply_content = re.sub(r'序号[：:]\s*\d+\s*$', '', reply_content).strip()
        reply_content = re.sub(r'序号[：:]\s*\d+', '', reply_content).strip()
        patterns_to_strip = [
            r'^[\d]+[\.、]\s*',
            r'^Q[：:]\s*',
            r'^A[：:]\s*',
            r'^[-*]\s*',
        ]
        for pat in patterns_to_strip:
            reply_content = re.sub(pat, '', reply_content, count=1).strip()
        risk_categories = self._classify_reply_content_risks(reply_content)
        if "system_capability" in risk_categories:
            logger.warning("回复包含系统功能描述，已过滤")
            return ""
        if "math_hallucination" in risk_categories:
            logger.warning("回复包含数学幻觉内容，已过滤")
            return ""
        reply_content = self._rewrite_reply_content_by_risks(reply_content, risk_categories)
        reply_content = re.sub(r'^功能支持[：:]', '', reply_content).strip()
        reply_content = re.sub(r'^功能可以[：:]', '', reply_content).strip()
        strict_math_patterns = [
            r'无法给出数学计算结果',
            r'没有包含任何具体的数学问题',
            r'从未出现任何数学问题',
            r'不存在任何数学问题',
            r'\\frac\{',
            r'\$\$',
            r'LaTeX',
        ]
        math_match_count = 0
        for pat in strict_math_patterns:
            if re.search(pat, reply_content):
                math_match_count += 1
        if math_match_count >= 3:
            logger.warning(f"回复包含数学幻觉内容({math_match_count}个匹配)，已过滤")
            return ""

        math_context_detected = math_match_count > 0 or any(
            marker in reply_content for marker in ("数学", "计算", "表达式", "公式", "LaTeX")
        )
        soft_math_patterns = [
            (r'无数学问题', '无相关话题'),
            (r'无计算结果', '暂无相关信息'),
            (r'数学表达式或运算请求', '其他问题'),
            (r'化为小数', ''),
        ]
        if math_context_detected:
            for pat, replacement in soft_math_patterns:
                if re.search(pat, reply_content):
                    reply_content = re.sub(pat, replacement, reply_content)

        # 清理知识库答案中的尾部多余内容（关键词标签、分隔线等）
        # 匹配模式：以换行开头后跟关键词标签、分隔线等内容
        trailing_patterns = [
            r'\n\s*【关键词】[^\n]*',  # 关键词标签及后续内容
            r'\n\s*─{10,}[^\n]*',      # 分隔线及后续内容
            r'\n\s*-{10,}[^\n]*',      # 短分隔线及后续内容
            r'\n\s*#{5,}[^\n]*',       # 哈希分隔线
            r'\n\s*_\.{5,}[^\n]*',    # 下划线分隔
            r'\n\s*\*[\*\s]*',        # 星号分隔
        ]
        for pattern in trailing_patterns:
            reply_content = re.sub(pattern, '', reply_content)
        
        reply_content = re.sub(r'\n{3,}', '\n\n', reply_content).strip()
        
        DOUYIN_MAX_REPLY_LENGTH = 300
        if len(reply_content) > DOUYIN_MAX_REPLY_LENGTH:
            truncated = reply_content[:DOUYIN_MAX_REPLY_LENGTH]
            sentence_end = max(
                truncated.rfind('。'),
                truncated.rfind('！'),
                truncated.rfind('？'),
                truncated.rfind('；'),
                truncated.rfind('，'),
                truncated.rfind('\n'),
            )
            if sentence_end > DOUYIN_MAX_REPLY_LENGTH * 0.5:
                truncated = truncated[:sentence_end + 1]
            logger.warning(f"回复内容超长({len(reply_content)}字符)，截断至{len(truncated)}字符")
            reply_content = truncated
        
        return reply_content

    def _is_duplicate_reply_content(self, conversation_id: str, reply_content: str) -> bool:
        """检查回复内容是否与最近发送的内容重复（优化版：去除模板前缀后再比较）
        
        修复：先去除"客户名您好！"等模板前缀后再做相似度比较，
        避免因模板前缀相同导致不同内容的回复被误判为重复。
        仅防止30秒内网络延迟导致的完全重复发送。
        """
        try:
            return self._get_outbound_recent_reply_store().is_recent_duplicate(
                conversation_id=conversation_id,
                content=reply_content,
            )
        except Exception:
            return False

    @staticmethod
    def _has_echo_question_or_confirm_signal(content: str) -> bool:
        text = str(content or "").strip()
        if not text:
            return False
        has_question_signal = any(q in text for q in ('？', '?', '吗', '呢', '怎么', '如何', '什么', '为什么', '哪', '多少', '能否', '可以', '能不能', '好不好'))
        has_confirm_signal = any(q in text for q in ('好的', '确认', '没问题', '是的', '对', '行', '可以', '同意'))
        return has_question_signal or has_confirm_signal

    def _is_echo_candidate_match(self, inbound_content: str, outbound_content: str) -> bool:
        content = str(inbound_content or "").strip()
        sent_content = str(outbound_content or "").strip()
        if not content or not sent_content:
            return False
        if content == sent_content:
            return True
        if len(content) > 10 and len(sent_content) > 10:
            if content in sent_content and len(content) >= len(sent_content) * 0.7:
                if not self._has_echo_question_or_confirm_signal(content):
                    return True
            min_compare = min(len(content), len(sent_content))
            if min_compare > 20:
                common_prefix_len = 0
                for i in range(min_compare):
                    if content[i] != sent_content[i]:
                        break
                    common_prefix_len = i + 1
                prefix_ratio = common_prefix_len / max(len(content), len(sent_content))
                if prefix_ratio >= 0.6:
                    similarity = self._calculate_similarity(content, sent_content)
                    if similarity > 0.85:
                        return True
            if len(set(content) & set(sent_content)) / max(len(set(content) | set(sent_content)), 1) > 0.65:
                similarity = self._calculate_similarity(content, sent_content)
                if similarity > 0.92:
                    return True
        return False

    def _iter_persisted_echo_candidates(
        self,
        *,
        conversation_id: str = "",
        limit: int = 10,
    ) -> list[dict]:
        candidates: list[dict] = []
        if not conversation_id:
            return candidates
        try:
            last_outbound = self.db.get_last_outbound_message(conversation_id)
            if last_outbound:
                candidates.append(last_outbound)
        except Exception:
            pass
        try:
            outbox_events = list(getattr(self.db, "list_outbox_events")(limit=200) or [])
            outbox_events.sort(
                key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
                reverse=True,
            )
            for item in outbox_events:
                if str(item.get("conversation_id") or "").strip() != str(conversation_id or "").strip():
                    continue
                if not str(item.get("reply_content") or "").strip():
                    continue
                candidates.append(
                    {
                        "content": item.get("reply_content", ""),
                        "created_at": item.get("updated_at") or item.get("created_at") or "",
                        "message_id": item.get("message_id", ""),
                        "logical_message_id": item.get("logical_message_id", ""),
                        "status": item.get("status", ""),
                    }
                )
                if len(candidates) >= max(int(limit or 0), 1):
                    break
        except Exception:
            pass
        return candidates

    def _is_echo_message(
        self,
        customer_name: str,
        content: str,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        """检查入站消息是否是我们最近发送的出站消息的回显（ECHO检测）
        
        委托给 OutboundIdempotencyService.is_echo() 进行基础匹配，
        再补充子串/前缀/高相似度，以及持久化出站记录回查等高级检测策略。
        """
        outbound_service = self._get_outbound_idempotency_service()
        if outbound_service.is_echo(
            customer_name,
            content,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        ):
            logger.info(f"ECHO检测: 入站消息匹配出站幂等缓存，跳过: {customer_name}")
            return True

        current_time = time.time()
        echo_ttl = 120 if len(content) <= 10 else 600
        recent_entries = outbound_service.get_recent_entries(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        for sent_info in recent_entries:
            sent_time = sent_info.get("time", 0)
            sent_content = sent_info.get("content", "")
            if current_time - sent_time > echo_ttl:
                continue
            if not sent_content:
                continue
            if self._is_echo_candidate_match(content, sent_content):
                logger.info(f"ECHO检测: 入站消息匹配最近出站缓存(在{echo_ttl}s内)，跳过: {customer_name}")
                return True

        if conversation_id:
            persisted_echo_ttl = max(echo_ttl, 3600)
            for sent_info in self._iter_persisted_echo_candidates(conversation_id=conversation_id, limit=8):
                sent_content = str(sent_info.get("content", "") or "").strip()
                sent_at = str(sent_info.get("created_at", "") or "").strip()
                if not sent_content or not sent_at:
                    continue
                try:
                    sent_ts = datetime.fromisoformat(sent_at).timestamp()
                except ValueError:
                    continue
                if current_time - sent_ts > persisted_echo_ttl:
                    continue
                if self._is_echo_candidate_match(content, sent_content):
                    logger.info(
                        f"ECHO检测: 入站消息匹配持久化出站记录，跳过: customer={customer_name} "
                        f"conversation_id={conversation_id or '-'} sent_at={sent_at}"
                    )
                    return True
        return False

    def _run_inbound_reply_pipeline(self, message: dict):
        """统一入站主链的回复生成与发送节点。"""
        # 终极去重：如果入站消息内容与我方最近出站回复高度相似，跳过回复生成
        # 防止 DOM 误判我方回复为新入站消息导致的回复循环
        try:
            if isinstance(message, dict):
                sender_name = str(message.get("customer_name") or message.get("sender_name") or "")
                content = str(message.get("content") or "")
                conversation_id = str(message.get("conversation_id") or "")
                if sender_name and content and self._is_recently_sent_by_us(sender_name, content):
                    logger.info(
                        f"跳过回复生成: 入站消息与我方最近出站回复匹配 (sender={sender_name}, "
                        f"conversation={conversation_id}, content={content[:30]}...)"
                    )
                    return
        except Exception as e:
            logger.debug(f"终极去重检查异常(忽略): {e}")
        self._get_inbound_reply_orchestrator().run(message)

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算两个文本的字符级2-gram Jaccard相似度（支持中文）"""
        if not text1 or not text2:
            return 0.0
        t1 = text1.lower().strip()
        t2 = text2.lower().strip()
        if t1 == t2:
            return 1.0
        n = 2
        grams1 = set(t1[i:i+n] for i in range(len(t1) - n + 1))
        grams2 = set(t2[i:i+n] for i in range(len(t2) - n + 1))
        if not grams1 and not grams2:
            return 1.0
        if not grams1 or not grams2:
            return 0.0
        intersection = grams1 & grams2
        union = grams1 | grams2
        return len(intersection) / len(union) if union else 0.0

    def _unified_send_reply_result(
        self,
        customer_name: str,
        reply_content: str,
        conversation_id: str,
        reply_msg_id: str,
        reply_msg: dict,
        customer_id: str,
        platform: str,
        intent_level: str,
        intent_score: float,
        smart_result: dict,
        session=None,
        original_content: str = "",
        logical_message_id: str = "",
        outbox_id: str = "",
        outbound_source: str = "auto_reply",
        outbound_trigger: str = "reply",
    ) -> OutboundSendResult:
        """统一消息发送方法，返回规范化发送结果并收口后置状态。

        统一处理：
        1. 熔断器保护发送
        2. 预留机制确认/取消
        3. 自回复标记
        4. RPA引擎标记
        5. 会话管理器记录
        6. 数据库保存（消息+会话+客户状态）

        Args:
            customer_name: 客户名称
            reply_content: 回复内容
            conversation_id: 会话ID
            reply_msg_id: 回复消息ID
            reply_msg: 回复消息字典
            customer_id: 客户ID
            platform: 平台
            intent_level: 意向等级
            intent_score: 意向分数
            smart_result: 智能回复结果
            session: 会话对象

        Returns:
            OutboundSendResult: 规范化发送结果
        """
        from src.common.enterprise.circuit_breaker import CircuitOpenError
        from datetime import datetime
        send_success = False
        send_failure_reason = ""
        send_result = OutboundSendResult(success=False, channel="", reason="")
        state_repository = self._get_inbound_message_state_repository()
        trace_id = str(logical_message_id or reply_msg_id or conversation_id or "")[:12] or "unknown"
        perf_metrics: Dict[str, float] = {}
        total_started_at = time.perf_counter()
        # 兼容测试 mock：测试可能将 _unified_send_reply 作为实例属性注入 __dict__，
        # 此时走 legacy 路径，跳过 circuit_breaker 等运行时依赖。
        legacy_send = getattr(getattr(self, "__dict__", {}), "get", lambda *_, **__: None)("_unified_send_reply")
        if callable(legacy_send):
            success = bool(
                legacy_send(
                    customer_name=customer_name,
                    reply_content=reply_content,
                    conversation_id=conversation_id,
                    reply_msg_id=reply_msg_id,
                    reply_msg=reply_msg,
                    customer_id=customer_id,
                    platform=platform,
                    intent_level=intent_level,
                    intent_score=intent_score,
                    smart_result=smart_result,
                    session=session,
                    original_content=original_content,
                    logical_message_id=logical_message_id,
                    outbox_id=outbox_id,
                )
            )
            return OutboundSendResult(
                success=success,
                channel="legacy",
                reason="" if success else self._get_last_send_error("send_failed"),
            )
        self._ensure_outbox_event(
            outbox_id=outbox_id,
            logical_message_id=logical_message_id,
            conversation_id=conversation_id,
            customer_name=customer_name,
            reply_content=reply_content,
            platform=platform,
            source_message_id=reply_msg_id,
            outbound_source=outbound_source,
            outbound_trigger=outbound_trigger,
        )

        try:
            self._clear_last_send_error()
            # [FIX-INST:repeat-reply] 发送前预标记 sent_by_us=True
            # 收敛前：sent_by_us 在发送成功后才标记（约15秒后），期间 DOM 轮询持续运行，
            # 检测到"内容变化+unread=1"时 was_sent_by_us=False，路径1跳过失败，
            # 走到路径4 emit，导致用户消息被重复触发自动回复。
            # 收敛后：发送前就预标记，DOM 轮询检测到变化时走路径1跳过。
            # 发送失败时会在下方 rollback。
            try:
                if (
                    getattr(self, "rpa_launcher", None)
                    and getattr(self.rpa_launcher, "rpa_engine", None)
                ):
                    self.rpa_launcher.rpa_engine.mark_outbound_sent(customer_name, reply_content)
            except Exception as pre_mark_e:
                logger.debug(f"发送前预标记sent_by_us失败(非致命): {pre_mark_e}")
            try:
                stage_started_at = time.perf_counter()
                with self.circuit_breaker:
                    send_result = self._send_reply_to_target_result(
                        customer_name,
                        reply_content,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        platform=platform,
                        logical_message_id=logical_message_id,
                        outbound_source=outbound_source,
                        outbound_trigger=outbound_trigger,
                    )
                    send_success = bool(send_result.success)
                    if send_success:
                        logger.info(f"自动回复发送成功：{customer_name}")
                    else:
                        send_failure_reason = str(send_result.reason or self._get_last_send_error("send_failed"))
                        logger.error(f"自动回复发送失败：{customer_name}, reason={send_failure_reason}")
                self._record_reply_chain_metric(perf_metrics, "send_target", stage_started_at)
            except CircuitOpenError:
                logger.warning(f"熔断器已打开，跳过发送: {customer_name}")
                try:
                    self.db.cancel_reply_reservation(conversation_id, reply_content)
                except Exception as cancel_e:
                    logger.debug(f"熔断器打开时取消预留失败: {cancel_e}")
                try:
                    state_repository.update_outbox_event(
                        outbox_id,
                        status="cancelled",
                        message_id=reply_msg_id,
                        reason="circuit_open",
                        outbound_source=outbound_source,
                        outbound_trigger=outbound_trigger,
                        delivery_channel="circuit_breaker",
                    )
                except Exception:
                    pass
                self._mark_message_skipped(
                    customer_name,
                    original_content or reply_content,
                    "circuit_open",
                    reply_msg_id,
                    source_message_id=reply_msg_id or "",
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    logical_message_id=logical_message_id,
                )
                return OutboundSendResult(
                    success=False,
                    channel="circuit_breaker",
                    reason="circuit_open",
                )

            if send_success:
                sent_target_name = self._get_last_send_target_name(customer_name)
                self._mark_sent_by_us(
                    sent_target_name,
                    reply_content,
                    conversation_id,
                    customer_id=customer_id,
                    platform=platform,
                    logical_message_id=logical_message_id,
                    trace_id=trace_id,
                )
                if sent_target_name and sent_target_name != customer_name:
                    self._mark_sent_by_us(
                        customer_name,
                        reply_content,
                        conversation_id,
                        customer_id=customer_id,
                        platform=platform,
                        logical_message_id=logical_message_id,
                        trace_id=trace_id,
                    )
                stage_started_at = time.perf_counter()
                try:
                    self.db.confirm_reply_sent(conversation_id, reply_content, reply_msg_id)
                except Exception as confirm_e:
                    logger.debug(f"确认回复预留失败: {confirm_e}")
                self._record_reply_chain_metric(perf_metrics, "confirm_reply", stage_started_at)
                stage_started_at = time.perf_counter()
                try:
                    state_repository.update_outbox_event(
                        outbox_id,
                        status="sent",
                        message_id=reply_msg_id,
                        reason="",
                        next_retry_at="",
                        outbound_source=outbound_source,
                        outbound_trigger=outbound_trigger,
                        delivery_channel=str(send_result.channel or ""),
                    )
                except Exception:
                    pass
                self._record_reply_chain_metric(perf_metrics, "mark_outbox_sent", stage_started_at)
                conversation_data = {
                    "conversation_id": conversation_id,
                    "platform": platform,
                    "customer_id": customer_id,
                    "customer_name": sent_target_name or customer_name,
                    "last_message_content": reply_content,
                    "last_message_time": datetime.now().isoformat(),
                    "intent_level": intent_level,
                    "intent_score": intent_score,
                    "priority_level": smart_result.get("priority_level", "P2"),
                    "risk_level": (smart_result.get("risk_assessment", {}) or {}).get("overall_level", "low"),
                    "sentiment": smart_result.get("sentiment", "neutral"),
                    "suggested_action": smart_result.get("suggested_action", "")
                }
                stage_started_at = time.perf_counter()
                self._schedule_post_send_success_tasks(
                    trace_id=trace_id,
                    session=session,
                    reply_content=reply_content,
                    reply_msg=reply_msg,
                    conversation_data=conversation_data,
                    customer_id=customer_id,
                    platform=platform,
                    customer_name=sent_target_name or customer_name,
                    logical_message_id=logical_message_id,
                    reply_msg_id=reply_msg_id,
                )
                self._record_reply_chain_metric(perf_metrics, "schedule_post_send", stage_started_at)
            else:
                send_failure_reason = send_failure_reason or str(send_result.reason or self._get_last_send_error("send_failed"))
                stage_started_at = time.perf_counter()
                try:
                    self.db.cancel_reply_reservation(conversation_id, reply_content)
                except Exception as cancel_e:
                    logger.debug(f"取消回复预留失败: {cancel_e}")
                self._record_reply_chain_metric(perf_metrics, "cancel_reservation", stage_started_at)
                stage_started_at = time.perf_counter()
                try:
                    state_repository.update_outbox_event(
                        outbox_id,
                        status="failed",
                        message_id=reply_msg_id,
                        reason=send_failure_reason[:100],
                        outbound_source=outbound_source,
                        outbound_trigger=outbound_trigger,
                        delivery_channel=str(send_result.channel or ""),
                    )
                except Exception:
                    pass
                self._record_reply_chain_metric(perf_metrics, "mark_outbox_failed", stage_started_at)

        except Exception as e:
            send_success = False
            send_failure_reason = self._set_last_send_error(f"send_pipeline_exception:{str(e)[:160]}")
            send_result = OutboundSendResult(
                success=False,
                channel="pipeline",
                reason=send_failure_reason,
            )
            logger.error(f"统一发送回复时发生异常：{e}")
            try:
                self.db.cancel_reply_reservation(conversation_id, reply_content)
            except Exception:
                pass
            try:
                state_repository.update_outbox_event(
                    outbox_id,
                    status="failed",
                    message_id=reply_msg_id,
                    reason=send_failure_reason[:100],
                    outbound_source=outbound_source,
                    outbound_trigger=outbound_trigger,
                    delivery_channel="pipeline",
                )
            except Exception:
                pass

        self._record_reply_chain_metric(perf_metrics, "total", total_started_at)
        self._log_reply_chain_perf(
            "send_reply",
            trace_id,
            perf_metrics,
            customer=customer_name,
            success=send_success,
            reason=(send_failure_reason[:80] if send_failure_reason else ""),
        )
        if send_success:
            return OutboundSendResult(
                success=True,
                channel=str(send_result.channel or "unknown"),
                reason="",
                duplicate_guard_hit=bool(send_result.duplicate_guard_hit),
            )
        return OutboundSendResult(
            success=False,
            channel=str(send_result.channel or "unknown"),
            reason=str(send_failure_reason or send_result.reason or self._get_last_send_error("send_failed")),
            duplicate_guard_hit=bool(send_result.duplicate_guard_hit),
        )

    def _unified_send_reply(
        self,
        customer_name: str,
        reply_content: str,
        conversation_id: str,
        reply_msg_id: str,
        reply_msg: dict,
        customer_id: str,
        platform: str,
        intent_level: str,
        intent_score: float,
        smart_result: dict,
        session=None,
        original_content: str = "",
        logical_message_id: str = "",
        outbox_id: str = "",
        outbound_source: str = "auto_reply",
        outbound_trigger: str = "reply",
    ) -> bool:
        """兼容旧链路的统一消息发送入口，返回 bool。"""
        result = self._unified_send_reply_result(
            customer_name=customer_name,
            reply_content=reply_content,
            conversation_id=conversation_id,
            reply_msg_id=reply_msg_id,
            reply_msg=reply_msg,
            customer_id=customer_id,
            platform=platform,
            intent_level=intent_level,
            intent_score=intent_score,
            smart_result=smart_result,
            session=session,
            original_content=original_content,
            logical_message_id=logical_message_id,
            outbox_id=outbox_id,
            outbound_source=outbound_source,
            outbound_trigger=outbound_trigger,
        )
        return result.success

    def _schedule_post_send_success_tasks(
        self,
        *,
        trace_id: str,
        session: Any,
        reply_content: str,
        reply_msg: dict,
        conversation_data: dict,
        customer_id: str,
        platform: str,
        customer_name: str,
        logical_message_id: str,
        reply_msg_id: str,
    ) -> None:
        """发送成功后异步补齐非关键落库，降低主链尾延迟。"""
        executor = getattr(self, "_post_send_executor", None)
        if executor is None:
            self._run_post_send_success_tasks(
                trace_id=trace_id,
                session=session,
                reply_content=reply_content,
                reply_msg=reply_msg,
                conversation_data=conversation_data,
                customer_id=customer_id,
                platform=platform,
                customer_name=customer_name,
                logical_message_id=logical_message_id,
                reply_msg_id=reply_msg_id,
            )
            return

        executor.submit(
            self._run_post_send_success_tasks,
            trace_id=trace_id,
            session=session,
            reply_content=reply_content,
            reply_msg=reply_msg,
            conversation_data=conversation_data,
            customer_id=customer_id,
            platform=platform,
            customer_name=customer_name,
            logical_message_id=logical_message_id,
            reply_msg_id=reply_msg_id,
        )

    def _run_post_send_success_tasks(
        self,
        *,
        trace_id: str,
        session: Any,
        reply_content: str,
        reply_msg: dict,
        conversation_data: dict,
        customer_id: str,
        platform: str,
        customer_name: str,
        logical_message_id: str,
        reply_msg_id: str,
    ) -> None:
        """异步执行发送成功后的附加写库与 tracking。"""
        post_metrics: Dict[str, float] = {}
        total_started_at = time.perf_counter()

        if session:
            stage_started_at = time.perf_counter()
            try:
                self.session_manager.add_message(session.session_id, "outbound", reply_content)
            except Exception as sess_e:
                logger.debug(f"保存出站消息到会话管理器失败: {sess_e}")
            self._record_reply_chain_metric(post_metrics, "session_append", stage_started_at)

        stage_started_at = time.perf_counter()
        try:
            self.db.save_message(reply_msg)
            logger.info("自动回复已保存到数据库")
        except Exception as db_msg_e:
            logger.error(f"保存回复消息到数据库失败: {db_msg_e}")
        self._record_reply_chain_metric(post_metrics, "save_message", stage_started_at)

        merged_conversation_id = str(conversation_data.get("conversation_id") or "")
        stage_started_at = time.perf_counter()
        try:
            self.db.save_conversation(conversation_data)
            merged_conversation_id = self._maybe_merge_identity_conversations(
                customer_name=customer_name,
                customer_id=customer_id,
                platform=platform,
                conversation_id=merged_conversation_id,
            ) or merged_conversation_id
        except Exception as db_conv_e:
            logger.error(f"保存会话到数据库失败: {db_conv_e}")
        self._record_reply_chain_metric(post_metrics, "save_conversation", stage_started_at)

        if customer_id and not customer_id.startswith("temp_"):
            stage_started_at = time.perf_counter()
            try:
                self.db.update_customer_status(customer_id, platform, 'replied')
            except Exception as status_e:
                logger.debug(f"更新客户状态失败: {status_e}")
            self._record_reply_chain_metric(post_metrics, "update_customer_status", stage_started_at)

        stage_started_at = time.perf_counter()
        self._update_marketing_tracking_status(
            status="replied",
            customer_name=customer_name,
            conversation_id=merged_conversation_id,
            allowed_current_statuses=["sent", "delivered", "read"],
        )
        self._record_reply_chain_metric(post_metrics, "update_tracking", stage_started_at)

        stage_started_at = time.perf_counter()
        self._record_marketing_tracking_message(
            customer_name=customer_name,
            conversation_id=merged_conversation_id,
            reply_content=reply_content,
            platform=platform,
            reply_msg_id=reply_msg_id,
            logical_message_id=logical_message_id,
        )
        self._record_reply_chain_metric(post_metrics, "record_tracking_message", stage_started_at)

        self._record_reply_chain_metric(post_metrics, "total", total_started_at)
        self._log_reply_chain_perf(
            "post_send_success",
            trace_id,
            post_metrics,
            customer=customer_name,
            conversation_id=merged_conversation_id,
        )

    def send_outbound_message(
        self,
        *,
        customer_name: str,
        reply_content: str,
        conversation_id: str,
        customer_id: str = "",
        platform: str = "douyin",
        intent_level: str = "E",
        intent_score: float = 0.0,
        smart_result: Optional[dict] = None,
        session=None,
        original_content: str = "",
    ) -> tuple[bool, str]:
        """统一的主动/手工/旁路发送包装，复用主发送状态机。"""
        from datetime import datetime

        reply_content = (reply_content or "").strip()
        if not reply_content:
            return False, ""

        if self._is_duplicate_reply_content(conversation_id, reply_content):
            logger.info(f"跳过重复出站消息 [{customer_name}]: 内容重复")
            return False, ""

        reply_msg_id = f"manual_{int(datetime.now().timestamp() * 1000)}_{hashlib.md5(reply_content.encode('utf-8')).hexdigest()[:6]}"
        logical_message_id = build_logical_message_id(
            platform=platform,
            conversation_id=conversation_id,
            customer_name=customer_name,
            direction="outbound",
            content=reply_content,
            source_message_id=reply_msg_id,
        )
        outbox_id = f"outbox_{logical_message_id}"

        self._ensure_outbox_event(
            outbox_id=outbox_id,
            logical_message_id=logical_message_id,
            conversation_id=conversation_id,
            customer_name=customer_name,
            reply_content=reply_content,
            platform=platform,
            source_message_id=reply_msg_id,
            outbound_source="manual_outbound",
            outbound_trigger="manual_send",
        )

        can_send, reason = self.db.can_send_reply(
            conversation_id,
            min_interval_seconds=1,
            reply_content=reply_content,
            logical_message_id=logical_message_id,
            source_message_id=reply_msg_id,
        )
        if not can_send:
            logger.info(f"主动/手工发送被冷却拦截 [{customer_name}]: {reason}")
            return False, ""

        reply_msg = {
            "message_id": reply_msg_id,
            "logical_message_id": logical_message_id,
            "source_message_id": reply_msg_id,
            "conversation_id": conversation_id,
            "customer_id": customer_id,
            "platform": platform,
            "direction": "outbound",
            "message_type": "text",
            "content": reply_content,
            "sender_id": "self",
            "sender_name": "我",
            "is_read": True,
            "is_processed": True,
            "created_at": datetime.now().isoformat(),
        }
        payload = smart_result or {
            "priority_level": "P2",
            "risk_assessment": {"overall_level": "low"},
            "sentiment": "neutral",
            "suggested_action": "",
        }
        success = self._unified_send_reply(
            customer_name=customer_name,
            reply_content=reply_content,
            conversation_id=conversation_id,
            reply_msg_id=reply_msg_id,
            reply_msg=reply_msg,
            customer_id=customer_id,
            platform=platform,
            intent_level=intent_level,
            intent_score=float(intent_score or 0.0),
            smart_result=payload,
            session=session,
            original_content=original_content,
            logical_message_id=logical_message_id,
            outbox_id=outbox_id,
            outbound_source="manual_outbound",
            outbound_trigger="manual_send",
        )
        return success, reply_msg_id if success else ""

    def _send_reply_to_target_result(
        self,
        customer_name: str,
        content: str,
        max_retries: int = 3,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
        logical_message_id: str = "",
        outbound_source: str = "",
        outbound_trigger: str = "",
    ) -> OutboundSendResult:
        """统一消息发送入口——委托到 OutboundSendGateway.send()。"""
        gateway = self._get_outbound_send_gateway()
        return gateway.send(
            customer_name=customer_name,
            content=content,
            max_retries=max_retries,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
            external_trace_id=logical_message_id,
            outbound_source=outbound_source,
            outbound_trigger=outbound_trigger,
        )

    def _send_reply_to_target(
        self,
        customer_name: str,
        content: str,
        max_retries: int = 3,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        """兼容旧链路的超薄发送包装，统一映射到结果对象入口。"""
        result = self._send_reply_to_target_result(
            customer_name,
            content,
            max_retries=max_retries,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        return result.success

    def _get_monitor_message_sender(self) -> Optional[MessageSender]:
        monitor = getattr(self, "message_monitor", None)
        target_page = getattr(monitor, "page", None) if monitor else None
        if target_page is None:
            return None

        api_interceptor = getattr(monitor, "api_interceptor", None)
        sender = getattr(self, "sender", None)
        if not isinstance(sender, MessageSender) or getattr(sender, "page", None) is not target_page:
            sender = MessageSender(target_page, self.db, api_interceptor=api_interceptor)
            self.sender = sender
        else:
            sender.api_interceptor = api_interceptor
            sender.page = target_page
        return sender

    def _send_message_to_active_monitor_conversation(
        self,
        customer_name: str,
        content: str,
    ) -> OutboundSendResult:
        monitor = getattr(self, "message_monitor", None)
        if monitor is None:
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason="message_monitor_uninitialized",
            )

        sender = self._get_monitor_message_sender()
        if sender is None:
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason="message_monitor_page_uninitialized",
            )

        previous_identity_context = dict(getattr(sender, "_active_target_identity_context", {}) or {})
        try:
            sender._active_target_identity_context = {
                "expected_identities": [customer_name] if customer_name else [],
                "identity_level": "exact_name",
            }
            pre_send_snapshot = sender._collect_chat_target_snapshot()
            input_locator = sender._find_message_input_locator()
            if input_locator is None:
                return OutboundSendResult(
                    success=False,
                    channel="monitor",
                    reason=f"input_box_not_found:{customer_name}",
                    resolved_name=customer_name,
                )

            if not sender._human_helper().clear_and_type(input_locator, content):
                with contextlib.suppress(Exception):
                    input_locator.click(timeout=1500)
                sender.page.keyboard.type(content, delay=100)
            sender.page.wait_for_timeout(180)
            success = sender._dispatch_send_action(
                input_locator=input_locator,
                message=content,
                expected_identities=[customer_name] if customer_name else [],
                previous_message_tail=list(pre_send_snapshot.get("messageTail") or []),
                allow_profile_dialog_fallback=False,
            )
            reason = "" if success else (
                getattr(monitor, "last_send_error", "")
                or f"monitor_send_failed:{customer_name}"
            )
            return OutboundSendResult(
                success=success,
                channel="monitor",
                reason=reason,
                resolved_name=customer_name,
            )
        except Exception as exc:
            logger.error(f"基于当前会话的 monitor 直发失败: {exc}")
            return OutboundSendResult(
                success=False,
                channel="monitor",
                reason=f"monitor_exception:{str(exc)[:160]}",
                resolved_name=customer_name,
            )
        finally:
            sender._active_target_identity_context = previous_identity_context

    def send_message_task_result(
        self,
        customer_name: str,
        content: str,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> OutboundSendResult:
        """在 BotService worker 线程中执行发送，返回统一结果对象。

        当前职责：在“目标会话已准备好”的前提下，直接执行 monitor DOM 发送。
        这一层不能再回调 OutboundSendGateway.send()，否则会形成
        Gateway -> BotService -> Gateway 的 monitor fallback 循环。
        """
        self._clear_last_send_error()
        if not self.message_monitor:
            self._set_last_send_error("message_monitor_uninitialized")
            logger.error("message_monitor 未初始化")
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason="message_monitor_uninitialized",
            )

        if not self.message_monitor.page:
            self._set_last_send_error("message_monitor_page_uninitialized")
            logger.error("message_monitor.page 未初始化")
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason="message_monitor_page_uninitialized",
            )

        if self.message_monitor.page.is_closed():
            self._set_last_send_error("message_monitor_page_closed")
            logger.error("message_monitor.page 已关闭")
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason="message_monitor_page_closed",
            )

        if not self._get_conversation_switch_service().ensure_monitor_send_target_with_identity(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        ):
            self._set_last_send_error(f"conversation_not_found:{customer_name}")
            logger.error(f"无法确保目标会话就绪: {customer_name}")
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason=f"conversation_not_found:{customer_name}",
            )

        logger.info(f"开始发送消息给 {customer_name}: {content[:30]}...")
        result_obj = self._send_message_to_active_monitor_conversation(customer_name, content)
        result = bool(result_obj.success) if result_obj is not None else False
        if result and customer_name and hasattr(self.message_monitor, "mark_message_sent"):
            try:
                self.message_monitor.mark_message_sent(customer_name, content)
            except Exception:
                pass
        if result:
            logger.info(f"消息发送成功: {customer_name}")
            self._clear_last_send_error()
            return result_obj

        failure_reason = (
            getattr(self.message_monitor, "last_send_error", "")
            or (result_obj.reason if result_obj is not None else "")
            or f"monitor_send_failed:{customer_name}"
        )
        self._set_last_send_error(failure_reason)
        logger.warning(f"消息发送失败: {customer_name}")
        return result_obj or OutboundSendResult(
            success=False,
            channel="monitor",
            reason=failure_reason,
        )

    def send_message_task(self, customer_name: str, content: str) -> bool:
        """兼容旧 worker 发送接口，统一映射到结果对象入口。"""
        return bool(self.send_message_task_result(customer_name, content).success)

    def _send_via_monitor(
        self,
        customer_name: str,
        content: str,
        max_retries: int = 3,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> OutboundSendResult:
        """通过message_monitor发送消息（带重试机制，Event.wait避免线程阻塞）

        Args:
            customer_name: 客户名称
            content: 回复内容
            max_retries: 最大重试次数

        Returns:
            统一发送结果对象
        """
        for attempt in range(1, max_retries + 1):
            try:
                if threading.current_thread() is getattr(self, "worker_thread", None):
                    result = self.send_message_task_result(
                        customer_name,
                        content,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        platform=platform,
                    )
                else:
                    future = self._submit_task(
                        self.send_message_task_result,
                        customer_name,
                        content,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        platform=platform,
                    )
                    result = future.result(timeout=30)

                if result.success:
                    return result

                if attempt < max_retries:
                    logger.warning(f"发送失败，{2 * attempt}秒后重试 ({attempt}/{max_retries})")
                    threading.Event().wait(timeout=2 * attempt)

            except Exception as e:
                self._set_last_send_error(f"monitor_exception:{str(e)[:160]}")
                logger.error(f"发送消息失败 ({attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    threading.Event().wait(timeout=2 * attempt)

        failure_reason = getattr(self, "_last_send_error", "") or f"monitor_send_failed:{customer_name}"
        if not getattr(self, "_last_send_error", ""):
            self._set_last_send_error(failure_reason)
        logger.error(f"消息发送失败，已达到最大重试次数 ({max_retries}次): {customer_name}")
        return OutboundSendResult(
            success=False,
            channel="monitor",
            reason=failure_reason,
            resolved_name=customer_name,
        )

    # ==================== 消息同步 ====================

    def sync_conversations_from_douyin(self) -> dict:
        """从抖音同步会话列表到本地数据库"""
        from datetime import datetime
        if not self.is_running or not self.message_monitor:
            return {"success": False, "count": 0, "message": "浏览器未启动或消息监听器未初始化"}
        
        try:
            # 调用消息监听器的同步方法
            conversations = self.message_monitor.sync_conversations()
            
            if not conversations:
                return {"success": True, "count": 0, "message": "没有需要同步的会话"}
            
            existing_conversations = {
                str(item.get("conversation_id", "") or "").strip(): item
                for item in (self.db.get_all_conversations() or [])
                if str(item.get("conversation_id", "") or "").strip()
            }
            sync_count = 0
            for conv in conversations:
                customer_id = str(conv.get("customer_id", "") or "").strip()
                customer_name = str(conv.get("customer_name", "") or "").strip()
                conversation_id = str(conv.get("conversation_id", "") or "").strip() or self._resolve_conversation_id(
                    customer_name,
                    platform="douyin",
                    customer_id=customer_id,
                )
                last_message = conv.get("last_message_content", "")
                existing_conv = existing_conversations.get(conversation_id, {})
                preserved_customer_name = str(existing_conv.get("customer_name", "") or "").strip()
                preserved_customer_id = str(existing_conv.get("customer_id", "") or "").strip()
                if preserved_customer_name and preserved_customer_name != customer_name:
                    inbound_names = {
                        str(msg.get("sender_name", "") or "").strip()
                        for msg in (self.db.get_conversation_messages(conversation_id, limit=20) or [])
                        if str(msg.get("direction", "")).strip() == "inbound"
                        and str(msg.get("sender_name", "") or "").strip()
                    }
                    if preserved_customer_name in inbound_names or preserved_customer_id:
                        customer_name = preserved_customer_name
                
                conversation_data = {
                    "conversation_id": conversation_id,
                    "platform": "douyin",
                    "customer_id": customer_id or preserved_customer_id,
                    "customer_name": customer_name,
                    "last_message_content": last_message,
                    "last_message_time": conv.get("last_message_time", datetime.now().isoformat()),
                    "status": "active"
                }
                self.db.save_conversation(conversation_data)
                self._maybe_merge_identity_conversations(
                    customer_name=customer_name,
                    customer_id=customer_id or preserved_customer_id,
                    platform="douyin",
                    conversation_id=conversation_id,
                )
                existing_conversations[conversation_id] = {
                    **existing_conv,
                    **conversation_data,
                }
                sync_count += 1
            
            logger.info(f"同步完成: {sync_count} 个会话")
            return {"success": True, "count": sync_count, "message": f"成功同步 {sync_count} 个会话"}
            
        except Exception as e:
            logger.error(f"同步会话列表失败: {e}")
            return {"success": False, "count": 0, "message": f"同步失败: {str(e)}"}
    
    # [REFACTOR-INST:convergence] 历史消息回溯拉取链路已废弃。
    # 原因：底层 message_monitor.get_conversation_detail / get_conversation_messages_via_api /
    # get_conversation_detail_batch / _parse_current_chat_messages / _parse_message_time /
    # _scroll_chat_history 已删除（依赖已删除的 smart_finder/mode_selector 或不再被核心
    # 自动回复链路使用）。当前仅保留占位实现，向后兼容可能的旧调用方（无外部调用，
    # 但对外暴露的方法签名保留），返回空成功响应避免业务中断。
    def sync_conversation_detail(self, customer_name: str, _max_scroll: int = 100, _use_api: bool = False, _use_batch: bool = False) -> dict:
        """同步指定会话的详细消息记录 - 已废弃占位实现

        历史：曾通过 DOM 滚动/API 拦截回溯拉取会话历史消息，用于全量同步功能。
        现状：历史消息拉取链路（get_conversation_detail 等）已删除，会话列表同步
        （sync_conversations_from_douyin）已能覆盖核心需求。本方法保留为占位，
        兼容可能的旧调用方，统一返回空成功响应。
        """
        logger.debug(f"sync_conversation_detail 已被废弃: customer={customer_name}")
        return {
            "success": True,
            "count": 0,
            "new_count": 0,
            "message": "历史消息回溯同步已废弃，会话列表已覆盖核心需求",
            "customer_name": customer_name,
        }

    def sync_all_conversation_details(self, _max_scroll_per_conversation: int = 100) -> dict:
        """全量同步所有会话的消息记录 - 已废弃占位实现

        原因同 sync_conversation_detail：依赖的 get_conversation_detail 等方法已删除。
        全量同步请使用 sync_conversations_from_douyin。
        """
        logger.debug("sync_all_conversation_details 已被废弃")
        return {
            "success": True,
            "total_conversations": 0,
            "total_messages": 0,
            "success_count": 0,
            "failed_count": 0,
            "details": [],
            "message": "历史消息全量同步已废弃，请使用 sync_conversations_from_douyin",
        }

    # ==================== 搜索任务 ====================
    
    def _get_independent_crawler_snapshot(self) -> Dict[str, Any]:
        """读取独立爬取/发送进程状态，兼容旧 BotService 入口转发。"""
        try:
            from src.common.process_manager import get_process_manager

            return (get_process_manager().get_status() or {}).get("crawler", {}) or {}
        except Exception as e:
            logger.debug(f"读取独立爬取/发送进程状态失败: {e}")
            return {}

    def _dispatch_independent_crawler_command(
        self,
        cmd_type: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        success_message: str,
    ) -> Dict[str, Any]:
        """旧 BotService 搜索/发送入口统一转发到独立爬取进程。"""
        try:
            from src.common.process_manager import get_process_manager

            process_manager = get_process_manager()
            if not process_manager.start_crawler_process():
                return {"success": False, "message": "独立爬取/发送进程启动失败"}
            if not process_manager.send_crawler_command("init_browser"):
                return {"success": False, "message": "独立爬取/发送进程浏览器初始化失败"}

            crawler_status = (process_manager.get_status() or {}).get("crawler", {}) or {}
            crawler_task = str(crawler_status.get("current_task") or "Idle")
            crawler_busy = bool(crawler_status.get("busy", False))
            if bool(crawler_status.get("alive", False)) and (crawler_busy or crawler_task != "Idle"):
                return {"success": False, "message": "独立爬取/发送进程正在运行中，请先停止当前任务"}

            if not process_manager.send_crawler_command(cmd_type, data or {}):
                return {"success": False, "message": "独立爬取/发送进程未接收命令"}
            return {"success": True, "message": success_message}
        except Exception as e:
            logger.error(f"转发独立爬取/发送进程命令失败: cmd={cmd_type}, error={e}")
            return {"success": False, "message": f"转发独立爬取/发送进程命令失败: {e}"}

    def stop_task(self):
        """
        停止当前任务
        
        同时设置 BotService 本地停止标志，并兼容转发独立爬取/发送进程停止信号。
        """
        self._update_runtime_state(stop_flag=True)
        logger.info("Task stop signal received")

        crawler_status = self._get_independent_crawler_snapshot()
        crawler_task = str(crawler_status.get("current_task") or "Idle")
        crawler_busy = bool(crawler_status.get("busy", False))
        if bool(crawler_status.get("alive", False)) and (crawler_busy or crawler_task != "Idle"):
            try:
                from src.common.process_manager import get_process_manager

                if get_process_manager().send_crawler_command("stop_task"):
                    logger.info("Independent crawler stop flag sent")
            except Exception as e:
                logger.debug(f"发送独立爬取/发送停止信号失败: {e}")
        
        # 同时设置爬虫的停止标志
        if self.crawler:
            self.crawler.set_stop_flag(True)
            logger.info("Crawler stop flag set")

    def run_search_task(
        self,
        keyword: str,
        max_videos: int,
        comment_keywords: str = "",
        auto_reply_enabled: bool = False,
        reply_quota: int | None = None,
        reply_templates: list[str] | None = None,
        auto_comment_enabled: bool = False,
        comment_templates: list[str] | None = None,
        _platform: str = "douyin",
        comment_time_preset: str = "",
        comment_time_start: str = "",
        comment_time_end: str = "",
        skip_crawled: bool = True,
        skip_existing_videos: bool = CRAWLER_SKIP_EXISTING_VIDEOS_DEFAULT,
        crawl_priority: str = CRAWLER_QUEUE_PRIORITY_DEFAULT,
        worker_threads: int = CRAWLER_WORKER_THREADS_DEFAULT,
        lead_quota: int | None = None,
    ):
        """
        运行搜索任务
        
        Args:
            keyword: 搜索关键词
            max_videos: 每轮搜索优先处理的视频数量；0 表示自动扩展
            comment_keywords: 评论内容关键词，用于精准筛选客户（多个关键词用逗号分隔）
            comment_time_preset: 评论时间快捷范围
            comment_time_start: 评论时间起始范围
            comment_time_end: 评论时间结束范围
            skip_crawled: 是否跳过历史已爬取评论
        """
        logger.warning("BotService.run_search_task 已切换为独立进程兼容入口")
        resolved_lead_quota = max(int(lead_quota or max_videos or 1), 1)
        resolved_max_videos = max(int(max_videos or 0), 0)
        resolved_reply_quota = max(int(reply_quota or 10), 1)
        resolved_reply_templates = [
            str(item or "").strip()
            for item in (reply_templates or comment_templates or [])
            if str(item or "").strip()
        ]
        return self._dispatch_independent_crawler_command(
            "search",
            {
                "keyword": keyword,
                "lead_quota": resolved_lead_quota,
                "max_videos": resolved_max_videos,
                "comment_keywords": comment_keywords,
                "auto_reply_enabled": bool((auto_reply_enabled or auto_comment_enabled) and resolved_reply_templates),
                "reply_quota": resolved_reply_quota if resolved_reply_templates else 0,
                "reply_templates": resolved_reply_templates,
                "auto_comment_enabled": bool((auto_reply_enabled or auto_comment_enabled) and resolved_reply_templates),
                "comment_templates": resolved_reply_templates,
                "platform": _platform,
                "comment_time_preset": comment_time_preset,
                "comment_time_start": comment_time_start,
                "comment_time_end": comment_time_end,
                "skip_crawled": skip_crawled,
                "skip_existing_videos": skip_existing_videos,
                "cleanup_unfinished_videos": True,
                "crawl_priority": crawl_priority,
                "worker_threads": worker_threads,
            },
            success_message="搜索任务已在独立进程中开始",
        )

        search_task_id = self._reserve_search_task(keyword, max_videos)
        if not search_task_id:
            return {"success": False, "message": "搜索任务已在运行或排队中，请稍后重试"}

        def _search_impl(
            search_task_id,
            keyword,
            max_videos,
            comment_keywords,
            platform,
            comment_time_preset,
            comment_time_start,
            comment_time_end,
            skip_crawled,
            skip_existing_videos,
            crawl_priority,
            worker_threads,
        ):
            import random

            effective_comment_time_start = comment_time_start or ""
            effective_comment_time_end = comment_time_end or ""
            preset_days = 0
            preset_key = str(comment_time_preset or "").strip()
            if preset_key:
                try:
                    preset_days = max(int(preset_key), 0)
                except ValueError:
                    preset_days = 0
                if preset_days > 0 and not effective_comment_time_start:
                    effective_comment_time_start = (
                        datetime.now() - timedelta(days=preset_days)
                    ).replace(microsecond=0).isoformat(sep="T")
            
            try:
                if self.login_handler:
                    self._login_status_cache = self.login_handler.check_login_status()
            except Exception as e:
                logger.warning(f"检查登录状态失败: {e}")
            
            self._update_runtime_state(current_task=f"Searching: {keyword}", stop_flag=False)
            normalized_priority = self._normalize_video_queue_priority(crawl_priority)
            requested_worker_threads = max(int(worker_threads or 1), 1)
            effective_worker_threads = 1
            if requested_worker_threads > 1:
                logger.warning(
                    f"当前视频评论抓取链基于共享浏览器页串行执行，worker_threads={requested_worker_threads} 已自动降级为 1"
                )
            
            # 重置爬虫停止标志
            if self.crawler:
                self.crawler.set_stop_flag(False)
            
            self.progress_info = {
                "total": max_videos,
                "current": 0,
                "detail": "正在搜索视频列表..."
            }
            self.last_search_summary = {
                "task_id": search_task_id,
                "status": "running",
                "platform": platform,
                "keyword": keyword,
                "comment_keywords": [],
                "comment_time_preset_days": preset_days,
                "comment_time_preset_label": self.COMMENT_TIME_PRESET_LABELS.get(
                    preset_key,
                    f"{preset_days}天内" if preset_days else "不限",
                ),
                "comment_time_start": effective_comment_time_start,
                "comment_time_end": effective_comment_time_end,
                "skip_crawled": bool(skip_crawled),
                "skip_existing_videos": bool(skip_existing_videos),
                "videos_discovered": 0,
                "videos_planned": max_videos,
                "videos_processed": 0,
                "videos_inserted": 0,
                "videos_existing": 0,
                "videos_skipped_existing": 0,
                "videos_queued": 0,
                "videos_failed": 0,
                "pending_videos": 0,
                "in_progress_videos": 0,
                "completed_videos": 0,
                "failed_videos": 0,
                "comments_crawled": 0,
                "total_comments": 0,
                "matched_comments": 0,
                "saved_customers": 0,
                "top_level_comments": 0,
                "reply_comments": 0,
                "duplicate_comments": 0,
                "time_filtered_comments": 0,
                "keyword_filtered_comments": 0,
                "history_recorded_comments": 0,
                "risk_control_detected": False,
                "completeness_warnings": [],
                "videos": [],
                "started_at": datetime.now().isoformat(),
            }
            
            try:
                logger.info(f"Starting search task: {keyword}, comment_keywords: {comment_keywords}")
                if not self.crawler:
                    logger.error("爬虫未初始化，无法执行搜索任务")
                    return
                search_crawler = self.crawler
                search_page = None
                if self.browser_manager and getattr(self.browser_manager, "context", None):
                    try:
                        search_page = self.browser_manager.create_search_page(force_new=True)
                        self._setup_page_close_listener(search_page, "综合搜索标签页", cleanup_on_close=False)
                        search_crawler = Crawler(search_page, self.db)
                    except Exception as search_page_e:
                        logger.warning(f"创建综合搜索标签页失败，回退到当前爬取页搜索: {search_page_e}")
                        search_crawler = self.crawler

                self.progress_info["total"] = max_videos
                self.last_search_summary["videos_discovered"] = 0
                self.last_search_summary["videos_planned"] = max_videos
                
                # 解析评论关键词
                target_keywords = []
                if comment_keywords and comment_keywords.strip():
                    normalized_keywords = comment_keywords.replace("，", ",")
                    target_keywords = [kw.strip() for kw in normalized_keywords.split(',') if kw.strip()]
                    logger.info(f"评论关键词过滤: {target_keywords}")
                self.last_search_summary["comment_keywords"] = target_keywords

                queue_items = self._discover_videos_until_queue_ready(
                    search_crawler=search_crawler,
                    keyword=keyword,
                    platform=platform,
                    max_videos=max_videos,
                    normalized_priority=normalized_priority,
                    skip_existing_videos=bool(skip_existing_videos),
                )
                logger.info(
                    "视频爬取队列已生成: "
                    f"keyword={keyword}, discovered={self.last_search_summary['videos_discovered']}, "
                    f"inserted={self.last_search_summary['videos_inserted']}, existing={self.last_search_summary['videos_existing']}, "
                    f"completed={self.last_search_summary['completed_videos']}, "
                    f"pending={self.last_search_summary['pending_videos']}, "
                    f"in_progress={self.last_search_summary['in_progress_videos']}, "
                    f"failed={self.last_search_summary['failed_videos']}, "
                    f"queue={len(queue_items)}, skipped_existing={self.last_search_summary['videos_skipped_existing']}, "
                    f"skip_existing_videos={bool(skip_existing_videos)}, "
                    f"priority={normalized_priority}, requested_workers={requested_worker_threads}, effective_workers={effective_worker_threads}"
                )

                count = 0
                for queue_index, video_item in enumerate(queue_items, start=1):
                    if self._get_runtime_state_snapshot()["stop_flag"]:
                        logger.info("收到停止信号，终止评论抓取队列")
                        break

                    aweme_id = str(video_item.get("aweme_id", "") or "").strip()
                    url = str(video_item.get("video_url", "") or "").strip()
                    if not aweme_id or not url:
                        continue

                    self._update_runtime_state(current_task=f"Crawling video {queue_index}/{len(queue_items)}")
                    self.progress_info["total"] = len(queue_items)
                    self.progress_info["current"] = queue_index
                    self.progress_info["detail"] = f"正在抓取第 {queue_index} 个待处理视频评论..."
                    self.db.mark_video_comment_crawl_started(platform, aweme_id)

                    try:
                        crawl_result = self.crawler.crawl_comments(
                            url,
                            target_keywords=target_keywords,
                            comment_time_start=effective_comment_time_start,
                            comment_time_end=effective_comment_time_end,
                            skip_crawled=skip_crawled,
                            video_title=video_item.get("title", ""),
                            author_name=video_item.get("author_name", ""),
                            search_keyword=video_item.get("search_keyword", "") or keyword,
                            search_task_id=search_task_id,
                        )
                        final_status, error_message = self._resolve_video_crawl_status(crawl_result)
                        self.db.finalize_video_comment_crawl(
                            platform=platform,
                            aweme_id=aweme_id,
                            status=final_status,
                            result=crawl_result,
                            error_message=error_message,
                        )
                    except Exception as video_error:
                        crawl_result = {
                            "aweme_id": aweme_id,
                            "total_comments": 0,
                            "matched_comments": 0,
                            "saved_customers": 0,
                            "top_level_comments": 0,
                            "reply_comments": 0,
                            "duplicate_comments": 0,
                            "time_filtered_comments": 0,
                            "keyword_filtered_comments": 0,
                            "history_recorded_comments": 0,
                            "termination_reason": "exception",
                            "risk_control_detected": False,
                            "reached_comment_end": False,
                            "completeness_warning": str(video_error),
                        }
                        self.db.finalize_video_comment_crawl(
                            platform=platform,
                            aweme_id=aweme_id,
                            status=DatabaseManager.VIDEO_STATUS_FAILED,
                            result=crawl_result,
                            error_message=str(video_error),
                        )
                        logger.error(f"视频评论抓取失败 aweme_id={aweme_id}: {video_error}")

                    final_status = str(
                        crawl_result.get("video_status")
                        or self._resolve_video_crawl_status(crawl_result)[0]
                    )
                    self.last_search_summary["videos_processed"] = count + 1
                    self.last_search_summary["comments_crawled"] += int(crawl_result.get("total_comments", 0) or 0)
                    self.last_search_summary["total_comments"] = int(
                        self.last_search_summary.get("comments_crawled", 0) or 0
                    )
                    self.last_search_summary["matched_comments"] += int(crawl_result.get("matched_comments", 0) or 0)
                    self.last_search_summary["saved_customers"] += int(crawl_result.get("saved_customers", 0) or 0)
                    self.last_search_summary["top_level_comments"] += int(crawl_result.get("top_level_comments", 0) or 0)
                    self.last_search_summary["reply_comments"] += int(crawl_result.get("reply_comments", 0) or 0)
                    self.last_search_summary["duplicate_comments"] += int(crawl_result.get("duplicate_comments", 0) or 0)
                    self.last_search_summary["time_filtered_comments"] += int(crawl_result.get("time_filtered_comments", 0) or 0)
                    self.last_search_summary["keyword_filtered_comments"] += int(crawl_result.get("keyword_filtered_comments", 0) or 0)
                    self.last_search_summary["history_recorded_comments"] += int(crawl_result.get("history_recorded_comments", 0) or 0)
                    if final_status == DatabaseManager.VIDEO_STATUS_FAILED:
                        self.last_search_summary["videos_failed"] += 1
                    self.last_search_summary["risk_control_detected"] = bool(
                        self.last_search_summary["risk_control_detected"] or crawl_result.get("risk_control_detected", False)
                    )
                    completeness_warning = str(crawl_result.get("completeness_warning", "") or "").strip()
                    if completeness_warning:
                        self.last_search_summary["completeness_warnings"].append(completeness_warning)
                        self.last_search_summary["completeness_warnings"] = self.last_search_summary["completeness_warnings"][-12:]
                    self.last_search_summary["videos"].append({
                        "url": url,
                        "aweme_id": aweme_id,
                        "title": video_item.get("title", ""),
                        "author": video_item.get("author_name", "") or video_item.get("author", ""),
                        "comment_count": video_item.get("comment_count", 0),
                        "like_count": video_item.get("like_count", 0),
                        "publish_timestamp": video_item.get("publish_timestamp", 0),
                        "comment_crawl_status": final_status,
                        "total_comments": crawl_result.get("total_comments", 0),
                        "top_level_comments": crawl_result.get("top_level_comments", 0),
                        "reply_comments": crawl_result.get("reply_comments", 0),
                        "expected_comment_count": crawl_result.get("expected_comment_count", 0),
                        "matched_comments": crawl_result.get("matched_comments", 0),
                        "saved_customers": crawl_result.get("saved_customers", 0),
                        "duplicate_comments": crawl_result.get("duplicate_comments", 0),
                        "time_filtered_comments": crawl_result.get("time_filtered_comments", 0),
                        "reached_comment_end": crawl_result.get("reached_comment_end", False),
                        "risk_control_detected": crawl_result.get("risk_control_detected", False),
                        "risk_control_reason": crawl_result.get("risk_control_reason", ""),
                        "completeness_warning": crawl_result.get("completeness_warning", ""),
                        "history_cutoff_time": crawl_result.get("history_cutoff_time", ""),
                    })
                    self.last_search_summary["videos"] = self.last_search_summary["videos"][-8:]
                    self.progress_info["detail"] = (
                        f"第 {queue_index} 个视频已完成: 新入库 {crawl_result.get('saved_customers', 0)} 条, "
                        f"跳过历史 {crawl_result.get('duplicate_comments', 0)} 条, 状态 {final_status}"
                    )

                    if queue_index < len(queue_items):
                        if crawl_result.get("risk_control_detected"):
                            interval = random.uniform(18, 28)
                            logger.info(f"评论区疑似触发风控，延长冷却 {interval:.1f} 秒后处理下一个视频...")
                        elif str(crawl_result.get("termination_reason") or "") == "history_cutoff_reached":
                            interval = random.uniform(4, 8)
                            logger.info(f"当前视频仅做增量复查，短暂等待 {interval:.1f} 秒后处理下一个视频...")
                        else:
                            interval = random.uniform(8, 15)  # 8-15秒随机间隔
                            logger.info(f"视频处理完成，等待 {interval:.1f} 秒后处理下一个视频...")
                        chunk_interval = 3
                        elapsed = 0
                        while elapsed < interval:
                            sleep_time = min(chunk_interval, interval - elapsed)
                            time.sleep(sleep_time)
                            elapsed += sleep_time
                            # 处理队列中的排队任务（如启动监听、自动回复等）
                            try:
                                self._process_pending_tasks_during_wait()
                            except Exception as task_e:
                                logger.debug(f"爬取间隔中处理排队任务异常: {task_e}")
                            # 主动触发消息轮询
                            try:
                                self._poll_message_monitor()
                            except Exception as poll_e:
                                logger.debug(f"爬取间隔中消息轮询异常: {poll_e}")

                    count += 1

                final_status_counts = self.db.get_video_comment_status_counts(platform=platform, search_keyword=keyword)
                self.last_search_summary["pending_videos"] = int(final_status_counts.get(DatabaseManager.VIDEO_STATUS_PENDING, 0) or 0)
                self.last_search_summary["in_progress_videos"] = int(final_status_counts.get(DatabaseManager.VIDEO_STATUS_IN_PROGRESS, 0) or 0)
                self.last_search_summary["completed_videos"] = int(final_status_counts.get(DatabaseManager.VIDEO_STATUS_COMPLETED, 0) or 0)
                self.last_search_summary["failed_videos"] = int(final_status_counts.get(DatabaseManager.VIDEO_STATUS_FAILED, 0) or 0)
                self.last_search_summary["status"] = (
                    "stopped" if self._get_runtime_state_snapshot()["stop_flag"] else "completed"
                )
                if search_page and search_page != self.page:
                    try:
                        if not search_page.is_closed():
                            search_page.close()
                    except Exception:
                        pass
                    try:
                        if getattr(self.browser_manager, "search_page", None) == search_page:
                            self.browser_manager.search_page = None
                    except Exception:
                        pass
                self.last_search_summary["finished_at"] = datetime.now().isoformat()
                logger.info(
                    "Search task completed: "
                    f"processed={count}, discovered={self.last_search_summary['videos_discovered']}, "
                    f"inserted={self.last_search_summary['videos_inserted']}, existing={self.last_search_summary['videos_existing']}, "
                    f"skipped_existing={self.last_search_summary['videos_skipped_existing']}, "
                    f"queue={self.last_search_summary['videos_queued']}, failed={self.last_search_summary['videos_failed']}, "
                    f"matched_comments={self.last_search_summary['matched_comments']}, "
                    f"saved_customers={self.last_search_summary['saved_customers']}, "
                    f"duplicate_comments={self.last_search_summary['duplicate_comments']}, "
                    f"time_filtered={self.last_search_summary['time_filtered_comments']}, "
                    f"history_recorded={self.last_search_summary['history_recorded_comments']}, "
                    f"completed={self.last_search_summary['completed_videos']}, "
                    f"pending={self.last_search_summary['pending_videos']}, "
                    f"in_progress={self.last_search_summary['in_progress_videos']}, "
                    f"failed_status={self.last_search_summary['failed_videos']}"
                )
            except Exception as e:
                logger.error(f"Search task failed: {e}")
                if self.last_search_summary is not None:
                    self.last_search_summary["status"] = "failed"
                    self.last_search_summary["error"] = str(e)
                    self.last_search_summary["finished_at"] = datetime.now().isoformat()
            finally:
                self._release_search_task(search_task_id)
                self._update_runtime_state(current_task="Idle", stop_flag=False)
                self.progress_info = {"total": 0, "current": 0, "detail": ""}
                if self.crawler:
                    self.crawler.set_stop_flag(False)
        
        accepted = self._submit_task_no_wait(
            _search_impl,
            search_task_id,
            keyword,
            max_videos,
            comment_keywords,
            _platform,
            comment_time_preset,
            comment_time_start,
            comment_time_end,
            skip_crawled,
            skip_existing_videos,
            crawl_priority,
            worker_threads,
        )
        if not accepted:
            self._release_search_task(search_task_id)
            return {"success": False, "message": "任务队列已满，请稍后重试"}
        return {"success": True, "message": "搜索任务已开始", "task_id": search_task_id}

    def run_send_task(self, message: str, count: int):
        """运行私信发送任务"""
        logger.warning("BotService.run_send_task 已切换为独立进程兼容入口")
        return self._dispatch_independent_crawler_command(
            "send_messages",
            {
                "message": message,
                "max_count": count,
            },
            success_message="发送任务已在独立进程中开始",
        )

        def _send_impl(message, count):
            from src.common.private_message_limit_service import get_private_message_limit_service

            limit_service = get_private_message_limit_service()
            try:
                if self.login_handler:
                    self._login_status_cache = self.login_handler.check_login_status()
            except Exception as e:
                logger.warning(f"检查登录状态失败: {e}")
            
            self._update_runtime_state(current_task="Sending messages", stop_flag=False)
            
            # 获取待发送的客户列表
            pending_customers = self.db.get_pending_customers("douyin", limit=count)
            
            if not pending_customers:
                logger.info("没有待发送的客户")
                self._update_runtime_state(current_task="Idle")
                return
            
            self.progress_info = {
                "total": len(pending_customers),
                "current": 0,
                "detail": "准备发送私信..."
            }
            
            try:
                logger.info(f"Starting send task: {len(pending_customers)} customers")
                
                sent_count = 0
                for i, customer in enumerate(pending_customers):
                    if self._get_runtime_state_snapshot()["stop_flag"]:
                        logger.info("Send task stopped by user")
                        break
                    
                    self.progress_info["current"] = i + 1
                    self.progress_info["detail"] = f"正在发送给 {customer.get('nickname', '未知')}..."
                    
                    # 发送私信
                    sec_uid = customer.get("sec_uid")
                    nickname = customer.get("nickname", "")
                    
                    if sec_uid and self.sender:
                        account_scope = self.sender.resolve_current_account_scope(force_refresh=(i == 0))
                        account_id = str(account_scope.get("account_id") or "").strip() or "__unresolved_current_login__"
                        reservation_token = ""
                        decision = limit_service.acquire_send_permit(
                            count=1,
                            message=message,
                            user_id=sec_uid,
                            account_id=account_id,
                            source="bot_service",
                        )
                        if not decision.allowed:
                            logger.warning(f"自动私信发送已停止，原因: {decision.message}")
                            self.progress_info["detail"] = decision.message
                            break
                        reservation_token = str(decision.details.get("reservation_token") or "")
                        success = False
                        try:
                            success = self.sender.send_private_message(sec_uid, message)
                        finally:
                            limit_service.finalize_send_attempt(
                                reservation_token=reservation_token,
                                success=success,
                                count=1,
                                message=message,
                                user_id=sec_uid,
                                account_id=account_id,
                                source="bot_service",
                            )
                        if success:
                            self.db.update_customer_status(sec_uid, "douyin", "sent")
                            sent_count += 1
                            logger.info(f"发送成功: {nickname}")
                        else:
                            logger.warning(f"发送失败: {nickname}")
                    
                    # 避免发送过快
                    time.sleep(3)
                
                logger.info(f"Send task completed: {sent_count}/{len(pending_customers)} sent")
            except Exception as e:
                logger.error(f"Send task failed: {e}")
            finally:
                self._update_runtime_state(current_task="Idle", stop_flag=False)
                self.progress_info = {"total": 0, "current": 0, "detail": ""}
        
        self._submit_task_no_wait(_send_impl, message, count)
    
    # ==================== 购买意向分析 ====================
    
    def analyze_purchase_intent(self, customer_name: str) -> dict:
        """
        分析客户购买意向
        
        Args:
            customer_name: 客户名称
            
        Returns:
            dict: 分析结果
        """
        try:
            conversation_id = self._make_conversation_id(customer_name)
            
            # 获取消息历史
            messages = self._get_chat_store().get_recent_messages_dicts(conversation_id, limit=9999)
            
            if not messages:
                return {
                    "success": False,
                    "message": "没有找到对话记录",
                    "customer_name": customer_name
                }
            
            # 获取客户数据
            customer_data = {
                "customer_id": customer_name,
                "customer_name": customer_name,
                "platform": "douyin"
            }
            
            # 使用购买意向分析器
            result = self.purchase_intent_analyzer.analyze(customer_data, messages)
            
            return {
                "success": True,
                "customer_name": customer_name,
                "score": {
                    "total_score": result.score.total_score,
                    "confidence": result.score.confidence,
                    "purchase_probability": result.score.purchase_probability,
                    "estimated_deal_size": result.score.estimated_deal_size,
                    "estimated_close_days": result.score.estimated_close_days,
                    "churn_risk": result.score.churn_risk,
                    "engagement_score": result.score.engagement_score,
                    "interest_score": result.score.interest_score,
                    "urgency_score": result.score.urgency_score,
                    "budget_score": result.score.budget_score,
                    "authority_score": result.score.authority_score,
                    "need_score": result.score.need_score,
                    "timeline_score": result.score.timeline_score
                },
                "lifecycle_stage": result.lifecycle_stage.value,
                "buying_role": result.buying_role.value,
                "signals_detected": [s.value for s in result.score.signals_detected],
                "recommended_actions": result.recommended_actions,
                "risk_factors": result.risk_factors,
                "opportunity_factors": result.opportunity_factors,
                "best_contact_time": result.best_contact_time,
                "analysis_details": result.analysis_details
            }
            
        except Exception as e:
            logger.error(f"分析购买意向失败: {e}")
            return {
                "success": False,
                "message": f"分析失败: {str(e)}",
                "customer_name": customer_name
            }
    
    def score_lead(self, customer_name: str) -> dict:
        """
        对线索进行评分
        
        Args:
            customer_name: 客户名称
            
        Returns:
            dict: 评分结果
        """
        try:
            conversation_id = self._make_conversation_id(customer_name)
            
            # 获取消息历史
            messages = self._get_chat_store().get_recent_messages_dicts(conversation_id, limit=9999)
            
            # 获取客户数据
            customer_data = {
                "customer_id": customer_name,
                "customer_name": customer_name,
                "platform": "douyin"
            }
            
            # 使用获客服务进行评分
            result = self.customer_acquisition_service.score_lead(customer_data, messages)
            
            return {
                "success": True,
                "customer_name": customer_name,
                "lead_score": result.lead_score.value,
                "follow_up_priority": result.follow_up_priority.value,
                "scores": {
                    "intent_score": result.intent_score,
                    "engagement_score": result.engagement_score,
                    "fit_score": result.fit_score,
                    "timing_score": result.timing_score
                },
                "recommended_actions": result.recommended_actions,
                "suggested_follow_up_time": result.suggested_follow_up_time.isoformat() if result.suggested_follow_up_time else None,
                "suggested_channel": result.suggested_channel,
                "suggested_content": result.suggested_content,
                "risk_alerts": result.risk_alerts,
                "conversion_probability": result.conversion_probability,
                "estimated_value": result.estimated_value
            }
            
        except Exception as e:
            logger.error(f"线索评分失败: {e}")
            return {
                "success": False,
                "message": f"评分失败: {str(e)}",
                "customer_name": customer_name
            }
    
    def get_hot_leads(self) -> dict:
        """
        获取所有热线索
        
        Returns:
            dict: 热线索列表
        """
        try:
            hot_leads = self.customer_acquisition_service.get_hot_leads()
            
            return {
                "success": True,
                "count": len(hot_leads),
                "leads": [
                    {
                        "customer_id": lead.customer_id,
                        "lead_score": lead.lead_score.value,
                        "follow_up_priority": lead.follow_up_priority.value,
                        "intent_score": lead.intent_score,
                        "conversion_probability": lead.conversion_probability,
                        "estimated_value": lead.estimated_value,
                        "suggested_follow_up_time": lead.suggested_follow_up_time.isoformat() if lead.suggested_follow_up_time else None
                    }
                    for lead in hot_leads
                ]
            }
            
        except Exception as e:
            logger.error(f"获取热线索失败: {e}")
            return {
                "success": False,
                "message": f"获取失败: {str(e)}"
            }
    
    def get_conversion_funnel(self) -> dict:
        """
        获取转化漏斗分析
        
        Returns:
            dict: 漏斗分析结果
        """
        try:
            funnel_result = self.customer_acquisition_service.analyze_conversion_funnel()
            
            return {
                "success": True,
                "data": funnel_result
            }
            
        except Exception as e:
            logger.error(f"获取转化漏斗失败: {e}")
            return {
                "success": False,
                "message": f"获取失败: {str(e)}"
            }
    
    def start_nurture_campaign(self, customer_name: str) -> dict:
        """
        启动客户培育活动
        
        Args:
            customer_name: 客户名称
            
        Returns:
            dict: 培育活动配置
        """
        try:
            customer = self.db.get_customer_by_nickname(customer_name, "douyin") if self.db else None
            customer_id = customer.get("sec_uid", customer_name) if customer else customer_name
            result = self.customer_acquisition_service.start_nurture_campaign(customer_id)
            
            return {
                "success": True,
                "customer_name": customer_name,
                "nurture_config": result
            }
            
        except Exception as e:
            logger.error(f"启动培育活动失败: {e}")
            return {
                "success": False,
                "message": f"启动失败: {str(e)}",
                "customer_name": customer_name
            }
    
    def get_nurture_message(self, customer_name: str) -> dict:
        """
        获取培育消息
        
        Args:
            customer_name: 客户名称
            
        Returns:
            dict: 培育消息
        """
        try:
            customer = self.db.get_customer_by_nickname(customer_name, "douyin") if self.db else None
            customer_id = customer.get("sec_uid", customer_name) if customer else customer_name
            content = self.customer_acquisition_service.get_nurture_message(customer_id)
            
            if content:
                return {
                    "success": True,
                    "customer_name": customer_name,
                    "content": content
                }
            else:
                return {
                    "success": False,
                    "message": "没有更多培育消息或培育已完成",
                    "customer_name": customer_name
                }
            
        except Exception as e:
            logger.error(f"获取培育消息失败: {e}")
            return {
                "success": False,
                "message": f"获取失败: {str(e)}",
                "customer_name": customer_name
            }
    
    def analyze_all_customers_intent(self) -> dict:
        """
        分析所有客户的购买意向
        
        Returns:
            dict: 批量分析结果
        """
        try:
            conversations = self._get_chat_store().get_all_conversations_dicts()
            
            results = []
            high_intent_count = 0
            medium_intent_count = 0
            low_intent_count = 0
            
            for conv in conversations:
                customer_name = conv.get("customer_name", "")
                if not customer_name:
                    continue
                
                analysis = self.analyze_purchase_intent(customer_name)
                
                if analysis.get("success"):
                    score = analysis.get("score", {}).get("total_score", 0)
                    
                    if score >= 70:
                        high_intent_count += 1
                    elif score >= 40:
                        medium_intent_count += 1
                    else:
                        low_intent_count += 1
                    
                    results.append({
                        "customer_name": customer_name,
                        "total_score": score,
                        "lifecycle_stage": analysis.get("lifecycle_stage"),
                        "buying_role": analysis.get("buying_role"),
                        "purchase_probability": analysis.get("score", {}).get("purchase_probability", 0)
                    })
            
            # 按分数排序
            results.sort(key=lambda x: x["total_score"], reverse=True)
            
            return {
                "success": True,
                "total_customers": len(results),
                "summary": {
                    "high_intent": high_intent_count,
                    "medium_intent": medium_intent_count,
                    "low_intent": low_intent_count
                },
                "results": results
            }
            
        except Exception as e:
            logger.error(f"批量分析购买意向失败: {e}")
            return {
                "success": False,
                "message": f"分析失败: {str(e)}"
            }



_bot_service_instance = None
_bot_service_lock = threading.Lock()


def get_bot_service():
    """获取BotService单例"""
    global _bot_service_instance
    if _bot_service_instance is None:
        with _bot_service_lock:
            if _bot_service_instance is None:
                _bot_service_instance = BotService()
    return _bot_service_instance
