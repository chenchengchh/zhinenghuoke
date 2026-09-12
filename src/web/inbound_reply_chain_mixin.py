"""
InboundReplyChainMixin - 入站回复链相关方法

从 BotService 中提取的入站回复链方法集合，包含：
1. 入站管道控制（跳过/失败/执行）
2. 回复上下文准备与智能回复生成
3. 回复候选构造与资格判定
4. 投递计划与执行
5. 冷却重试与异常处理
6. RPA 回调与消息提交
7. 工厂/装配方法
"""
import contextlib
import hashlib
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, TypedDict

from loguru import logger

from src.common.enhanced_customer_service import resolve_context_session_id
from src.common.industry_schema_service import get_industry_schema_service
from src.common.logical_message import build_logical_message_id
from src.common.monitoring import get_metrics
from src.common.reply_eligibility_service import get_reply_eligibility_service
from src.common.types.reply_eligibility import (
    EligibilityAction,
    ReplyEligibilityDecision,
    ReplyEligibilityInput,
)
from src.web.inbound_reply_components import (
    _CustomerIntentGateway,
    _InboundDeliveryExecutionResult,
    _InboundDeliveryPlan,
    _InboundEventGateway,
    _InboundReplyComponentFactory,
    _InboundReplyExecutionDeps,
    _InboundReplyExecutionProvider,
    _InboundReplyExecutor,
    _InboundReplyExecutorAccessBundle,
    _InboundReplyExecutorFailureAccessBundle,
    _InboundReplyExecutorFailureInboundStateAccessBundle,
    _InboundReplyExecutorFailureOutboxStateAccessBundle,
    _InboundReplyExecutorFailureStateAccessBundle,
    _InboundReplyExecutorFailureWorkflowAccessBundle,
    _InboundReplyExecutorFailureWorkflowBindingAccessBundle,
    _InboundReplyExecutorFailureWorkflowFailureAccessBundle,
    _InboundReplyExecutorIntentAccessBundle,
    _InboundReplyFailureProvider,
    _InboundReplyFactoryDeps,
    _InboundReplyOrchestrator,
    _InboundReplyPlanDeps,
    _InboundReplyPlanProvider,
    _InboundReplyPlanner,
    _InboundReplyPlannerAccessBundle,
    _InboundReplyRequest,
    _InboundReplyRequestDeps,
    _InboundReplyRequestProvider,
    _MessageStateRepository,
    _OutboxEventGateway,
    _ReplyGenerator,
    _ReplySender,
    _WorkflowRunGateway,
)


def get_enhanced_customer_service():
    """提供可 monkeypatch 的增强客服服务入口。"""
    from src.common.enhanced_customer_service import get_enhanced_customer_service as _get_service
    return _get_service()


# --- 从 bot_service.py 迁移的辅助类型 ---

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


# --- 辅助类 ---


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


class InboundReplyChainMixin:
    """入站回复链 Mixin —— 从 BotService 提取的入站回复相关方法。"""

    # ---- 入站管道控制 ----

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

    # ---- 回复上下文准备 ----

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

    # ---- 智能回复生成 ----

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
        import concurrent.futures

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

    # ---- 投递结果处理 ----

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
        from src.common.types.reply_eligibility import EligibilityAction

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

    # ---- 回复候选构造 ----

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

    # ---- 运行时标志与资格判定辅助 ----

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

    # ---- 工厂/装配方法 ----

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

    # ---- 投递计划与执行 ----

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

    # ---- 路由与消息提交 ----

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

    def submit_inbound_message(self, message: Any) -> tuple[bool, str]:
        return self._get_inbound_pipeline_service().submit_inbound_message(message)

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

    # ---- 主链运行 ----

    def _run_inbound_reply_pipeline(self, message: dict):
        """统一入站主链的回复生成与发送节点。"""
        self._get_inbound_reply_orchestrator().run(message)
