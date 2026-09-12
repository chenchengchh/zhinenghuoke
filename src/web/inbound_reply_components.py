from __future__ import annotations

import hashlib
import inspect
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, TypedDict

from loguru import logger
from src.common.tracing_support import append_trace_span, build_trace_snapshot


def _supports_keyword_args(handler: Callable[..., Any], kwargs: dict[str, Any]) -> bool:
    if not kwargs:
        return False
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return True
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    accepted = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    return set(kwargs).issubset(accepted)


def _call_with_optional_kwargs(
    handler: Callable[..., Any],
    *args,
    **kwargs,
) -> Any:
    if _supports_keyword_args(handler, kwargs):
        return handler(*args, **kwargs)
    return handler(*args)


class _InboundReplyRequest(TypedDict):
    """主回复链入口的标准化请求数据。"""

    reply_msg_id: str
    content: str
    conversation_id: str
    customer_name: str
    msg_id: str
    logical_message_id: str
    workflow_run_id: str
    trace_id: str
    customer_id: str
    platform: str
    enterprise_id: str
    preferred_schema_id: str
    tenant_resolution_mode: str
    cached_smart_result: Any
    cached_reply_content: str


class _InboundDeliveryPlan(_InboundReplyRequest):
    """主回复链在上下文/生成阶段完成后的投递计划。"""

    session: Any
    smart_result: dict
    reply_content: str
    intent_level: str
    intent_score: float
    reply_logical_message_id: str
    reply_outbox_id: str


class _InboundDeliveryExecutionResult(TypedDict):
    """主回复链投递阶段的执行结果。"""

    should_update_intent: bool
    reply_outbox_id: str
    delivery_trace: dict[str, Any]


@dataclass(frozen=True)
class _InboundReplyRequestDeps:
    is_message_done: Callable[[str, str, str], bool]
    is_recently_sent_by_us: Callable[[str, str], bool]


@dataclass(frozen=True)
class _InboundReplyRequestProvider:
    is_message_done: Callable[[str, str, str], bool]
    is_recently_sent_by_us: Callable[[str, str], bool]

    @classmethod
    def from_callbacks(
        cls,
        *,
        is_message_done: Callable[[str, str, str], bool],
        is_recently_sent_by_us: Callable[[str, str], bool],
    ) -> "_InboundReplyRequestProvider":
        adapter = _InboundReplyRequestProviderAdapter(
            is_message_done=is_message_done,
            is_recently_sent_by_us=is_recently_sent_by_us,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_resolver(
        cls,
        *,
        is_message_done_resolver: Callable[[], Optional[Callable[[str, str, str], bool]]],
        is_recently_sent_by_us_resolver: Callable[[], Optional[Callable[[str, str], bool]]],
    ) -> "_InboundReplyRequestProvider":
        adapter = _InboundReplyRequestProviderAdapter(
            is_message_done_resolver=is_message_done_resolver,
            is_recently_sent_by_us_resolver=is_recently_sent_by_us_resolver,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_adapter(
        cls, adapter: "_InboundReplyRequestProviderAdapter"
    ) -> "_InboundReplyRequestProvider":
        return cls(
            is_message_done=adapter.is_message_done,
            is_recently_sent_by_us=adapter.is_recently_sent_by_us,
        )


@dataclass(frozen=True)
class _InboundReplyPlanDeps:
    prepare_inbound_reply_context: Callable[..., dict]
    generate_inbound_smart_reply: Callable[..., dict]
    prepare_inbound_reply_candidate: Callable[..., Optional[dict]]


@dataclass(frozen=True)
class _InboundReplyPlanProvider:
    prepare_inbound_reply_context: Callable[..., dict]
    generate_inbound_smart_reply: Callable[..., dict]
    prepare_inbound_reply_candidate: Callable[..., Optional[dict]]

    @classmethod
    def from_callbacks(
        cls,
        *,
        prepare_inbound_reply_context: Callable[..., dict],
        generate_inbound_smart_reply: Callable[..., dict],
        prepare_inbound_reply_candidate: Callable[..., Optional[dict]],
    ) -> "_InboundReplyPlanProvider":
        adapter = _InboundReplyPlanProviderAdapter(
            prepare_inbound_reply_context=prepare_inbound_reply_context,
            generate_inbound_smart_reply=generate_inbound_smart_reply,
            prepare_inbound_reply_candidate=prepare_inbound_reply_candidate,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_resolver(
        cls,
        *,
        prepare_inbound_reply_context_resolver: Callable[[], Optional[Callable[..., dict]]],
        generate_inbound_smart_reply_resolver: Callable[[], Optional[Callable[..., dict]]],
        prepare_inbound_reply_candidate_resolver: Callable[[], Optional[Callable[..., Optional[dict]]]],
    ) -> "_InboundReplyPlanProvider":
        adapter = _InboundReplyPlanProviderAdapter(
            prepare_inbound_reply_context_resolver=prepare_inbound_reply_context_resolver,
            generate_inbound_smart_reply_resolver=generate_inbound_smart_reply_resolver,
            prepare_inbound_reply_candidate_resolver=prepare_inbound_reply_candidate_resolver,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_adapter(cls, adapter: "_InboundReplyPlanProviderAdapter") -> "_InboundReplyPlanProvider":
        return cls(
            prepare_inbound_reply_context=adapter.prepare_inbound_reply_context,
            generate_inbound_smart_reply=adapter.generate_inbound_smart_reply,
            prepare_inbound_reply_candidate=adapter.prepare_inbound_reply_candidate,
        )


@dataclass(frozen=True)
class _InboundEventGateway:
    finalize_inbound_event_safe: Callable[..., None]
    skip_inbound_pipeline: Callable[..., None]

    @classmethod
    def from_callbacks(
        cls,
        *,
        finalize_inbound_event_safe: Callable[..., None],
        skip_inbound_pipeline: Callable[..., None],
    ) -> "_InboundEventGateway":
        adapter = _InboundEventGatewayAdapter(
            finalize_inbound_event_safe=finalize_inbound_event_safe,
            skip_inbound_pipeline=skip_inbound_pipeline,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_resolver(
        cls,
        *,
        finalize_inbound_event_safe_resolver: Callable[[], Optional[Callable[..., None]]],
        skip_inbound_pipeline_resolver: Callable[[], Optional[Callable[..., None]]],
    ) -> "_InboundEventGateway":
        adapter = _InboundEventGatewayAdapter(
            finalize_inbound_event_safe_resolver=finalize_inbound_event_safe_resolver,
            skip_inbound_pipeline_resolver=skip_inbound_pipeline_resolver,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_adapter(cls, adapter: "_InboundEventGatewayAdapter") -> "_InboundEventGateway":
        return cls(
            finalize_inbound_event_safe=adapter.finalize_inbound_event_safe,
            skip_inbound_pipeline=adapter.skip_inbound_pipeline,
        )


@dataclass(frozen=True)
class _WorkflowRunGateway:
    fail_workflow_run: Callable[[str, str], None]
    bind_workflow_outbox: Callable[[str, str], None]

    @classmethod
    def from_manager(cls, workflow_manager: Any) -> "_WorkflowRunGateway":
        adapter = _WorkflowRunGatewayAdapter(workflow_manager)
        return cls.from_adapter(adapter)

    @classmethod
    def from_manager_resolver(cls, workflow_manager_resolver: Callable[[], Any]) -> "_WorkflowRunGateway":
        adapter = _WorkflowRunGatewayAdapter(workflow_manager_resolver=workflow_manager_resolver)
        return cls.from_adapter(adapter)

    @classmethod
    def from_adapter(cls, adapter: "_WorkflowRunGatewayAdapter") -> "_WorkflowRunGateway":
        return cls(
            fail_workflow_run=adapter.fail_workflow_run,
            bind_workflow_outbox=adapter.bind_workflow_outbox,
        )


@dataclass(frozen=True)
class _CustomerIntentGateway:
    update_customer_intent: Callable[[str, str, str, float], None]

    @classmethod
    def from_callbacks(
        cls,
        *,
        update_customer_intent: Callable[[str, str, str, float], None],
    ) -> "_CustomerIntentGateway":
        adapter = _CustomerIntentGatewayAdapter(update_customer_intent=update_customer_intent)
        return cls.from_adapter(adapter)

    @classmethod
    def from_resolver(
        cls,
        *,
        update_customer_intent_resolver: Callable[[], Optional[Callable[[str, str, str, float], None]]],
    ) -> "_CustomerIntentGateway":
        adapter = _CustomerIntentGatewayAdapter(
            update_customer_intent_resolver=update_customer_intent_resolver,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_adapter(cls, adapter: "_CustomerIntentGatewayAdapter") -> "_CustomerIntentGateway":
        return cls(update_customer_intent=adapter.update_customer_intent)


@dataclass(frozen=True)
class _OutboxEventGateway:
    update_outbox_event: Callable[..., None]

    @classmethod
    def from_callbacks(
        cls,
        *,
        update_outbox_event: Callable[..., None],
    ) -> "_OutboxEventGateway":
        adapter = _OutboxEventGatewayAdapter(update_outbox_event=update_outbox_event)
        return cls.from_adapter(adapter)

    @classmethod
    def from_resolver(
        cls,
        *,
        update_outbox_event_resolver: Callable[[], Optional[Callable[..., None]]],
    ) -> "_OutboxEventGateway":
        adapter = _OutboxEventGatewayAdapter(
            update_outbox_event_resolver=update_outbox_event_resolver,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_adapter(cls, adapter: "_OutboxEventGatewayAdapter") -> "_OutboxEventGateway":
        return cls(update_outbox_event=adapter.update_outbox_event)


class _InboundEventGatewayAdapter:
    """将入站状态收口能力适配为稳定的 inbound gateway。"""

    def __init__(
        self,
        finalize_inbound_event_safe: Optional[Callable[..., None]] = None,
        skip_inbound_pipeline: Optional[Callable[..., None]] = None,
        finalize_inbound_event_safe_resolver: Optional[Callable[[], Optional[Callable[..., None]]]] = None,
        skip_inbound_pipeline_resolver: Optional[Callable[[], Optional[Callable[..., None]]]] = None,
    ):
        self._finalize_inbound_event_safe = finalize_inbound_event_safe
        self._skip_inbound_pipeline = skip_inbound_pipeline
        self._finalize_inbound_event_safe_resolver = finalize_inbound_event_safe_resolver
        self._skip_inbound_pipeline_resolver = skip_inbound_pipeline_resolver

    def _resolve_finalize(self) -> Optional[Callable[..., None]]:
        if self._finalize_inbound_event_safe_resolver is not None:
            return self._finalize_inbound_event_safe_resolver()
        return self._finalize_inbound_event_safe

    def _resolve_skip(self) -> Optional[Callable[..., None]]:
        if self._skip_inbound_pipeline_resolver is not None:
            return self._skip_inbound_pipeline_resolver()
        return self._skip_inbound_pipeline

    def finalize_inbound_event_safe(self, *args, **kwargs) -> None:
        finalize = self._resolve_finalize()
        if finalize is None:
            return
        finalize(*args, **kwargs)

    def skip_inbound_pipeline(self, **kwargs) -> None:
        skip = self._resolve_skip()
        if skip is None:
            return
        skip(**kwargs)


class _InboundReplyPlanProviderAdapter:
    """将主回复链生成阶段能力适配为稳定的 plan provider。"""

    def __init__(
        self,
        prepare_inbound_reply_context: Optional[Callable[..., dict]] = None,
        generate_inbound_smart_reply: Optional[Callable[..., dict]] = None,
        prepare_inbound_reply_candidate: Optional[Callable[..., Optional[dict]]] = None,
        prepare_inbound_reply_context_resolver: Optional[Callable[[], Optional[Callable[..., dict]]]] = None,
        generate_inbound_smart_reply_resolver: Optional[Callable[[], Optional[Callable[..., dict]]]] = None,
        prepare_inbound_reply_candidate_resolver: Optional[
            Callable[[], Optional[Callable[..., Optional[dict]]]]
        ] = None,
    ):
        self._prepare_inbound_reply_context = prepare_inbound_reply_context
        self._generate_inbound_smart_reply = generate_inbound_smart_reply
        self._prepare_inbound_reply_candidate = prepare_inbound_reply_candidate
        self._prepare_inbound_reply_context_resolver = prepare_inbound_reply_context_resolver
        self._generate_inbound_smart_reply_resolver = generate_inbound_smart_reply_resolver
        self._prepare_inbound_reply_candidate_resolver = prepare_inbound_reply_candidate_resolver

    def _resolve_prepare_context(self) -> Optional[Callable[..., dict]]:
        if self._prepare_inbound_reply_context_resolver is not None:
            return self._prepare_inbound_reply_context_resolver()
        return self._prepare_inbound_reply_context

    def _resolve_generate(self) -> Optional[Callable[..., dict]]:
        if self._generate_inbound_smart_reply_resolver is not None:
            return self._generate_inbound_smart_reply_resolver()
        return self._generate_inbound_smart_reply

    def _resolve_prepare_candidate(self) -> Optional[Callable[..., Optional[dict]]]:
        if self._prepare_inbound_reply_candidate_resolver is not None:
            return self._prepare_inbound_reply_candidate_resolver()
        return self._prepare_inbound_reply_candidate

    def prepare_inbound_reply_context(self, **kwargs) -> dict:
        handler = self._resolve_prepare_context()
        if handler is None:
            return {}
        return handler(**kwargs)

    def generate_inbound_smart_reply(self, **kwargs) -> dict:
        handler = self._resolve_generate()
        if handler is None:
            return {}
        return handler(**kwargs)

    def prepare_inbound_reply_candidate(self, **kwargs) -> Optional[dict]:
        handler = self._resolve_prepare_candidate()
        if handler is None:
            return None
        return handler(**kwargs)


class _InboundReplyRequestProviderAdapter:
    """将主回复链 request 阶段能力适配为稳定的 request provider。"""

    def __init__(
        self,
        is_message_done: Optional[Callable[..., bool]] = None,
        is_recently_sent_by_us: Optional[Callable[[str, str], bool]] = None,
        is_message_done_resolver: Optional[
            Callable[[], Optional[Callable[..., bool]]]
        ] = None,
        is_recently_sent_by_us_resolver: Optional[
            Callable[[], Optional[Callable[[str, str], bool]]]
        ] = None,
    ):
        self._is_message_done = is_message_done
        self._is_recently_sent_by_us = is_recently_sent_by_us
        self._is_message_done_resolver = is_message_done_resolver
        self._is_recently_sent_by_us_resolver = is_recently_sent_by_us_resolver

    def _resolve_is_message_done(self) -> Optional[Callable[..., bool]]:
        if self._is_message_done_resolver is not None:
            return self._is_message_done_resolver()
        return self._is_message_done

    def _resolve_is_recently_sent_by_us(self) -> Optional[Callable[[str, str], bool]]:
        if self._is_recently_sent_by_us_resolver is not None:
            return self._is_recently_sent_by_us_resolver()
        return self._is_recently_sent_by_us

    def is_message_done(
        self,
        customer_name: str,
        content: str,
        msg_id: str = "",
        **kwargs,
    ) -> bool:
        handler = self._resolve_is_message_done()
        if handler is None:
            return False
        return bool(_call_with_optional_kwargs(handler, customer_name, content, msg_id, **kwargs))

    def is_recently_sent_by_us(self, customer_name: str, content: str) -> bool:
        handler = self._resolve_is_recently_sent_by_us()
        if handler is None:
            return False
        return bool(handler(customer_name, content))


class _InboundReplyExecutionProviderAdapter:
    """将主回复链执行阶段能力适配为稳定的 execution provider。"""

    def __init__(
        self,
        attempt_reply_delivery: Optional[Callable[..., dict]] = None,
        handle_inbound_cooling_retry: Optional[Callable[..., None]] = None,
        handle_inbound_delivery_result: Optional[Callable[..., bool]] = None,
        attempt_reply_delivery_resolver: Optional[Callable[[], Optional[Callable[..., dict]]]] = None,
        handle_inbound_cooling_retry_resolver: Optional[
            Callable[[], Optional[Callable[..., None]]]
        ] = None,
        handle_inbound_delivery_result_resolver: Optional[
            Callable[[], Optional[Callable[..., bool]]]
        ] = None,
    ):
        self._attempt_reply_delivery = attempt_reply_delivery
        self._handle_inbound_cooling_retry = handle_inbound_cooling_retry
        self._handle_inbound_delivery_result = handle_inbound_delivery_result
        self._attempt_reply_delivery_resolver = attempt_reply_delivery_resolver
        self._handle_inbound_cooling_retry_resolver = handle_inbound_cooling_retry_resolver
        self._handle_inbound_delivery_result_resolver = handle_inbound_delivery_result_resolver

    def _resolve_attempt(self) -> Optional[Callable[..., dict]]:
        if self._attempt_reply_delivery_resolver is not None:
            return self._attempt_reply_delivery_resolver()
        return self._attempt_reply_delivery

    def _resolve_cooling_retry(self) -> Optional[Callable[..., None]]:
        if self._handle_inbound_cooling_retry_resolver is not None:
            return self._handle_inbound_cooling_retry_resolver()
        return self._handle_inbound_cooling_retry

    def _resolve_delivery_result(self) -> Optional[Callable[..., bool]]:
        if self._handle_inbound_delivery_result_resolver is not None:
            return self._handle_inbound_delivery_result_resolver()
        return self._handle_inbound_delivery_result

    def attempt_reply_delivery(self, **kwargs) -> dict:
        handler = self._resolve_attempt()
        if handler is None:
            return {}
        return handler(**kwargs)

    def handle_inbound_cooling_retry(self, **kwargs) -> None:
        handler = self._resolve_cooling_retry()
        if handler is None:
            return
        handler(**kwargs)

    def handle_inbound_delivery_result(self, **kwargs) -> bool:
        handler = self._resolve_delivery_result()
        if handler is None:
            return False
        return bool(handler(**kwargs))


class _InboundReplyFailureProviderAdapter:
    """将主回复链失败标记能力适配为稳定的 failure provider。"""

    def __init__(
        self,
        mark_message_failed: Optional[Callable[[str, str, str], None]] = None,
        mark_message_failed_resolver: Optional[
            Callable[[], Optional[Callable[[str, str, str], None]]]
        ] = None,
    ):
        self._mark_message_failed = mark_message_failed
        self._mark_message_failed_resolver = mark_message_failed_resolver

    def _resolve_mark_failed(self) -> Optional[Callable[[str, str, str], None]]:
        if self._mark_message_failed_resolver is not None:
            return self._mark_message_failed_resolver()
        return self._mark_message_failed

    def mark_message_failed(
        self,
        customer_name: str,
        content: str,
        msg_id: str = "",
        **kwargs,
    ) -> None:
        handler = self._resolve_mark_failed()
        if handler is None:
            return
        _call_with_optional_kwargs(handler, customer_name, content, msg_id, **kwargs)


class _CustomerIntentGatewayAdapter:
    """将客户画像更新能力适配为稳定的 customer intent gateway。"""

    def __init__(
        self,
        update_customer_intent: Optional[Callable[[str, str, str, float], None]] = None,
        update_customer_intent_resolver: Optional[
            Callable[[], Optional[Callable[[str, str, str, float], None]]]
        ] = None,
    ):
        self._update_customer_intent = update_customer_intent
        self._update_customer_intent_resolver = update_customer_intent_resolver

    def _resolve_update(self) -> Optional[Callable[[str, str, str, float], None]]:
        if self._update_customer_intent_resolver is not None:
            return self._update_customer_intent_resolver()
        return self._update_customer_intent

    def update_customer_intent(self, customer_id: str, platform: str, intent_level: str, intent_score: float) -> None:
        update = self._resolve_update()
        if update is None:
            return
        update(customer_id, platform, intent_level, intent_score)


class _WorkflowRunGatewayAdapter:
    """将 workflow manager 适配为主回复链使用的稳定工作流网关。"""

    def __init__(self, workflow_manager: Any = None, workflow_manager_resolver: Optional[Callable[[], Any]] = None):
        self._workflow_manager = workflow_manager
        self._workflow_manager_resolver = workflow_manager_resolver

    def _resolve_workflow_manager(self) -> Any:
        if self._workflow_manager_resolver is not None:
            return self._workflow_manager_resolver()
        return self._workflow_manager

    def fail_workflow_run(self, workflow_run_id: str, reason: str) -> None:
        workflow_manager = self._resolve_workflow_manager()
        if workflow_manager is None:
            return
        workflow_manager.fail_run(workflow_run_id, reason=reason)

    def bind_workflow_outbox(self, workflow_run_id: str, outbox_id: str) -> None:
        workflow_manager = self._resolve_workflow_manager()
        if workflow_manager is None:
            return
        workflow_manager.bind_outbox(workflow_run_id, outbox_id)


class _OutboxEventGatewayAdapter:
    """将 outbox 状态更新能力适配为稳定的 outbox gateway。"""

    def __init__(
        self,
        update_outbox_event: Optional[Callable[..., None]] = None,
        update_outbox_event_resolver: Optional[Callable[[], Optional[Callable[..., None]]]] = None,
    ):
        self._update_outbox_event = update_outbox_event
        self._update_outbox_event_resolver = update_outbox_event_resolver

    def _resolve_update(self) -> Optional[Callable[..., None]]:
        if self._update_outbox_event_resolver is not None:
            return self._update_outbox_event_resolver()
        return self._update_outbox_event

    def update_outbox_event(self, outbox_id: str, **kwargs) -> None:
        update = self._resolve_update()
        if update is None:
            return
        update(outbox_id, **kwargs)


@dataclass(frozen=True)
class _InboundReplyPlannerDeps:
    request: _InboundReplyRequestDeps
    reply_generator: "_ReplyGenerator"
    access: "_InboundReplyPlannerAccessBundle"


@dataclass(frozen=True)
class _InboundReplyExecutionDeps:
    attempt_reply_delivery: Callable[..., dict]
    handle_inbound_cooling_retry: Callable[..., None]
    handle_inbound_delivery_result: Callable[..., bool]


@dataclass(frozen=True)
class _ReplyGenerator:
    prepare_inbound_reply_context: Callable[..., dict]
    generate_inbound_smart_reply: Callable[..., dict]
    prepare_inbound_reply_candidate: Callable[..., Optional[dict]]

    @classmethod
    def from_plan_deps(cls, deps: _InboundReplyPlanDeps) -> "_ReplyGenerator":
        return cls(
            prepare_inbound_reply_context=deps.prepare_inbound_reply_context,
            generate_inbound_smart_reply=deps.generate_inbound_smart_reply,
            prepare_inbound_reply_candidate=deps.prepare_inbound_reply_candidate,
        )


@dataclass(frozen=True)
class _InboundReplyExecutionProvider:
    attempt_reply_delivery: Callable[..., dict]
    handle_inbound_cooling_retry: Callable[..., None]
    handle_inbound_delivery_result: Callable[..., bool]

    @classmethod
    def from_callbacks(
        cls,
        *,
        attempt_reply_delivery: Callable[..., dict],
        handle_inbound_cooling_retry: Callable[..., None],
        handle_inbound_delivery_result: Callable[..., bool],
    ) -> "_InboundReplyExecutionProvider":
        adapter = _InboundReplyExecutionProviderAdapter(
            attempt_reply_delivery=attempt_reply_delivery,
            handle_inbound_cooling_retry=handle_inbound_cooling_retry,
            handle_inbound_delivery_result=handle_inbound_delivery_result,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_resolver(
        cls,
        *,
        attempt_reply_delivery_resolver: Callable[[], Optional[Callable[..., dict]]],
        handle_inbound_cooling_retry_resolver: Callable[[], Optional[Callable[..., None]]],
        handle_inbound_delivery_result_resolver: Callable[[], Optional[Callable[..., bool]]],
    ) -> "_InboundReplyExecutionProvider":
        adapter = _InboundReplyExecutionProviderAdapter(
            attempt_reply_delivery_resolver=attempt_reply_delivery_resolver,
            handle_inbound_cooling_retry_resolver=handle_inbound_cooling_retry_resolver,
            handle_inbound_delivery_result_resolver=handle_inbound_delivery_result_resolver,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_adapter(
        cls, adapter: "_InboundReplyExecutionProviderAdapter"
    ) -> "_InboundReplyExecutionProvider":
        return cls(
            attempt_reply_delivery=adapter.attempt_reply_delivery,
            handle_inbound_cooling_retry=adapter.handle_inbound_cooling_retry,
            handle_inbound_delivery_result=adapter.handle_inbound_delivery_result,
        )


@dataclass(frozen=True)
class _InboundReplyFailureProvider:
    mark_message_failed: Callable[..., None]

    @classmethod
    def from_callbacks(
        cls,
        *,
        mark_message_failed: Callable[..., None],
    ) -> "_InboundReplyFailureProvider":
        adapter = _InboundReplyFailureProviderAdapter(
            mark_message_failed=mark_message_failed,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_resolver(
        cls,
        *,
        mark_message_failed_resolver: Callable[[], Optional[Callable[..., None]]],
    ) -> "_InboundReplyFailureProvider":
        adapter = _InboundReplyFailureProviderAdapter(
            mark_message_failed_resolver=mark_message_failed_resolver,
        )
        return cls.from_adapter(adapter)

    @classmethod
    def from_adapter(
        cls, adapter: "_InboundReplyFailureProviderAdapter"
    ) -> "_InboundReplyFailureProvider":
        return cls(mark_message_failed=adapter.mark_message_failed)


@dataclass(frozen=True)
class _ReplySender:
    attempt_reply_delivery: Callable[..., dict]
    handle_inbound_cooling_retry: Callable[..., None]
    handle_inbound_delivery_result: Callable[..., bool]

    @classmethod
    def from_execution_deps(cls, deps: _InboundReplyExecutionDeps) -> "_ReplySender":
        return cls(
            attempt_reply_delivery=deps.attempt_reply_delivery,
            handle_inbound_cooling_retry=deps.handle_inbound_cooling_retry,
            handle_inbound_delivery_result=deps.handle_inbound_delivery_result,
        )


@dataclass(frozen=True)
class _MessageStateRepository:
    finalize_inbound_event_safe: Callable[..., None]
    update_outbox_event: Callable[..., None]
    bind_workflow_outbox: Callable[[str, str], None]
    fail_workflow_run: Callable[[str, str], None]
    mark_message_failed: Callable[[str, str, str], None]


@dataclass(frozen=True)
class _InboundReplyExecutorDeps:
    reply_sender: _ReplySender
    state_repository: _MessageStateRepository
    access: "_InboundReplyExecutorAccessBundle"


@dataclass(frozen=True)
class _InboundReplyPlannerAccessBundle:
    inbound_gateway: _InboundEventGateway
    workflow_gateway: _WorkflowRunGateway


@dataclass(frozen=True)
class _InboundReplyExecutorIntentAccessBundle:
    customer_intent_gateway: _CustomerIntentGateway


@dataclass(frozen=True)
class _InboundReplyExecutorFailureInboundStateAccessBundle:
    inbound_gateway: _InboundEventGateway


@dataclass(frozen=True)
class _InboundReplyExecutorFailureOutboxStateAccessBundle:
    outbox_gateway: _OutboxEventGateway


@dataclass(frozen=True)
class _InboundReplyExecutorFailureStateAccessBundle:
    inbound_state_access: _InboundReplyExecutorFailureInboundStateAccessBundle
    outbox_state_access: _InboundReplyExecutorFailureOutboxStateAccessBundle


@dataclass(frozen=True)
class _InboundReplyExecutorFailureWorkflowBindingAccessBundle:
    workflow_gateway: _WorkflowRunGateway


@dataclass(frozen=True)
class _InboundReplyExecutorFailureWorkflowFailureAccessBundle:
    workflow_gateway: _WorkflowRunGateway


@dataclass(frozen=True)
class _InboundReplyExecutorFailureWorkflowAccessBundle:
    binding_access: _InboundReplyExecutorFailureWorkflowBindingAccessBundle
    failure_access: _InboundReplyExecutorFailureWorkflowFailureAccessBundle


@dataclass(frozen=True)
class _InboundReplyExecutorFailureAccessBundle:
    state_access: _InboundReplyExecutorFailureStateAccessBundle
    workflow_access: _InboundReplyExecutorFailureWorkflowAccessBundle


@dataclass(frozen=True)
class _InboundReplyExecutorAccessBundle:
    intent_access: _InboundReplyExecutorIntentAccessBundle
    failure_access: _InboundReplyExecutorFailureAccessBundle


@dataclass(frozen=True)
class _InboundReplyOrchestratorDeps:
    planner: _InboundReplyPlanner
    executor: _InboundReplyExecutor


@dataclass(frozen=True)
class _InboundReplyFactoryDeps:
    request: _InboundReplyRequestDeps
    plan: _InboundReplyPlanDeps
    execution: _InboundReplyExecutionDeps
    failure: _InboundReplyFailureProvider
    planner_access: _InboundReplyPlannerAccessBundle
    executor_access: _InboundReplyExecutorAccessBundle


class _InboundReplyPlanner:
    """封装主回复链 request/plan 阶段，供 orchestrator 通过协作对象调用。"""

    def __init__(self, deps: _InboundReplyPlannerDeps):
        self._deps = deps

    def prepare_request(self, message: dict) -> Optional[_InboundReplyRequest]:
        content = message.get("content", message.get("last_message_content", ""))
        conversation_id = message.get("conversation_id", "")
        customer_name = message.get("customer_name", "")
        msg_id = message.get("msg_id", "")
        logical_message_id = message.get("logical_message_id", "")
        workflow_run_id = message.get("workflow_run_id", "")
        trace_id = str(logical_message_id or "")[:12]

        id_seed = logical_message_id or msg_id or f"{conversation_id}_{content}"
        reply_msg_id = (
            f"auto_{hashlib.md5(id_seed.encode('utf-8')).hexdigest()[:16]}"
        )

        logger.info(
            f"[回复主链] start trace={trace_id} customer={customer_name} "
            f"conversation_id={conversation_id or '-'} msg_id={msg_id or '-'} "
            f"logical_message_id={logical_message_id or '-'}"
        )

        if not customer_name or not customer_name.strip():
            logger.debug(
                f"跳过无客户名的消息: trace={trace_id or '-'} "
                f"conversation_id={conversation_id or '-'} source_message_id={msg_id or '-'}"
            )
            return None

        if not content or not content.strip():
            logger.debug(
                f"跳过空消息回复: customer={customer_name} trace={trace_id or '-'} "
                f"conversation_id={conversation_id or '-'} source_message_id={msg_id or '-'}"
            )
            self._deps.access.inbound_gateway.skip_inbound_pipeline(
                customer_name=customer_name,
                content=content,
                msg_id=msg_id,
                logical_message_id=logical_message_id,
                reason="empty",
            )
            return None

        if bool(
            _call_with_optional_kwargs(
                self._deps.request.is_message_done,
                customer_name,
                content.strip(),
                msg_id,
                conversation_id=conversation_id,
                logical_message_id=logical_message_id,
                source_message_id=msg_id,
            )
        ):
            logger.debug(
                f"回复阶段二次幂等检查: 消息已完成，跳过: customer={customer_name} "
                f"trace={trace_id or '-'} conversation_id={conversation_id or '-'} "
                f"source_message_id={msg_id or '-'} logical_message_id={logical_message_id or '-'}"
            )
            self._deps.access.inbound_gateway.finalize_inbound_event_safe(
                logical_message_id,
                status="done",
                source_message_id=msg_id,
                reason="already_done",
            )
            return None

        customer_id = message.get("sec_uid", message.get("customer_id", message.get("sender_id", "")))
        platform = message.get("platform", "douyin")

        normalized_content = content.strip()

        return {
            "reply_msg_id": reply_msg_id,
            "content": normalized_content,
            "conversation_id": conversation_id,
            "customer_name": customer_name,
            "msg_id": msg_id,
            "logical_message_id": logical_message_id,
            "workflow_run_id": workflow_run_id,
            "trace_id": trace_id,
            "customer_id": customer_id,
            "platform": platform,
            "enterprise_id": str(
                message.get("enterprise_id")
                or message.get("enterpriseId")
                or message.get("tenant_id")
                or message.get("tenantId")
                or ""
            ).strip(),
            "preferred_schema_id": str(
                message.get("preferred_schema_id")
                or message.get("preferredSchemaId")
                or message.get("schema_id")
                or message.get("schemaId")
                or ""
            ).strip(),
            "schema_id": str(
                message.get("schema_id")
                or message.get("schemaId")
                or ""
            ).strip(),
            "tenant_resolution_mode": str(
                message.get("tenant_resolution_mode")
                or message.get("tenantResolutionMode")
                or ""
            ).strip(),
            "cached_smart_result": message.get("_cached_smart_result"),
            "cached_reply_content": str(message.get("_cached_reply_content", "") or ""),
        }

    def prepare_plan(
        self,
        *,
        message: dict,
        request: _InboundReplyRequest,
    ) -> Optional[_InboundDeliveryPlan]:
        customer_name = request["customer_name"]
        content = request["content"]
        conversation_id = request["conversation_id"]
        customer_id = request["customer_id"]
        platform = request["platform"]
        enterprise_id = request.get("enterprise_id", "")
        preferred_schema_id = request.get("preferred_schema_id", "")
        schema_id = request.get("schema_id", "")
        tenant_resolution_mode = request.get("tenant_resolution_mode", "")
        logical_message_id = request["logical_message_id"]
        workflow_run_id = request["workflow_run_id"]
        trace_id = request["trace_id"]
        reply_msg_id = request["reply_msg_id"]
        cached_smart_result = request["cached_smart_result"]
        cached_reply_content = request["cached_reply_content"]
        msg_id = request["msg_id"]

        context = self._deps.reply_generator.prepare_inbound_reply_context(
            message=message,
            customer_name=customer_name,
            content=content,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
            logical_message_id=logical_message_id,
            workflow_run_id=workflow_run_id,
        )
        resolved_customer_id = context.get("customer_id", "")
        resolved_conversation_id = context.get("conversation_id", "") or conversation_id
        resolved_workflow_run_id = context.get("workflow_run_id", "") or workflow_run_id
        session = context.get("session")
        if not session:
            logger.error(f"无法获取或创建会话: {resolved_customer_id}")
            self._deps.access.inbound_gateway.skip_inbound_pipeline(
                customer_name=customer_name,
                content=content,
                msg_id=msg_id,
                logical_message_id=logical_message_id,
                reason="no_session",
            )
            self._deps.access.workflow_gateway.fail_workflow_run(resolved_workflow_run_id, "no_session")
            return None

        smart_result = self._deps.reply_generator.generate_inbound_smart_reply(
            customer_name=customer_name,
            content=content,
            conversation_id=resolved_conversation_id,
            customer_id=resolved_customer_id,
            platform=platform,
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
            schema_id=schema_id,
            tenant_resolution_mode=tenant_resolution_mode,
            session_id=context.get("session_id", ""),
            conversation_history=context.get("conversation_history", []),
            workflow_run_id=resolved_workflow_run_id,
            trace_id=trace_id,
            cached_smart_result=cached_smart_result,
            cached_reply_content=cached_reply_content,
        )
        candidate = self._deps.reply_generator.prepare_inbound_reply_candidate(
            customer_name=customer_name,
            content=content,
            conversation_id=resolved_conversation_id,
            customer_id=resolved_customer_id,
            msg_id=msg_id,
            logical_message_id=logical_message_id,
            workflow_run_id=resolved_workflow_run_id,
            trace_id=trace_id,
            platform=platform,
            reply_msg_id=reply_msg_id,
            smart_result=smart_result,
            cached_reply_content=cached_reply_content,
            history_message_count=len(context.get("conversation_history", []) or []),
        )
        if not candidate:
            logger.info(
                f"[回复主链] candidate为空，跳过发送: trace={trace_id} "
                f"customer={customer_name} conversation_id={conversation_id or '-'}"
            )
            return None

        return {
            **request,
            "customer_id": resolved_customer_id,
            "conversation_id": resolved_conversation_id,
            "workflow_run_id": resolved_workflow_run_id,
            "session": session,
            "smart_result": smart_result,
            "reply_content": candidate["reply_content"],
            "intent_level": candidate["intent_level"],
            "intent_score": candidate["intent_score"],
            "reply_logical_message_id": candidate["reply_logical_message_id"],
            "reply_outbox_id": candidate["reply_outbox_id"],
        }


class _InboundReplyExecutor:
    """封装主回复链 execute/post-update/exception 阶段。"""

    def __init__(self, deps: _InboundReplyExecutorDeps):
        self._deps = deps

    @staticmethod
    def _build_delivery_trace_payload(
        *,
        plan: _InboundDeliveryPlan,
        delivery_result: Optional[dict[str, Any]] = None,
        allow_duplicate_content: bool = False,
    ) -> dict[str, Any]:
        return {
            "trace": build_trace_snapshot(),
            "delivery": {
                "business_trace_id": str(plan.get("trace_id", "") or ""),
                "customer_name": str(plan.get("customer_name", "") or ""),
                "conversation_id": str(plan.get("conversation_id", "") or ""),
                "source_message_id": str(plan.get("msg_id", "") or ""),
                "logical_message_id": str(plan.get("logical_message_id", "") or ""),
                "reply_logical_message_id": str(plan.get("reply_logical_message_id", "") or ""),
                "reply_msg_id": str(plan.get("reply_msg_id", "") or ""),
                "reply_outbox_id": str(plan.get("reply_outbox_id", "") or ""),
                "workflow_run_id": str(plan.get("workflow_run_id", "") or ""),
                "allow_duplicate_content": bool(allow_duplicate_content),
                "status": str((delivery_result or {}).get("status", "") or ""),
                "success": bool((delivery_result or {}).get("success", False)),
            },
        }

    @staticmethod
    def _map_delivery_trace_status(delivery_status: str) -> str:
        normalized_status = str(delivery_status or "").strip().lower()
        if normalized_status == "sent":
            return "ok"
        if normalized_status in {"duplicate", "deduplicated"}:
            return "skipped"
        if normalized_status == "blocked":
            return "blocked"
        return "error"

    @classmethod
    def _append_delivery_trace_span(
        cls,
        payload: dict[str, Any],
        *,
        name: str,
        started_at: float,
        delivery_status: str,
        metadata: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        append_trace_span(
            payload,
            name=name,
            stage="delivery",
            duration_ms=(time.perf_counter() - started_at) * 1000,
            metadata={
                "delivery_status": str(delivery_status or ""),
                **(metadata or {}),
            },
            status=cls._map_delivery_trace_status(delivery_status),
            error=error,
        )

    def execute_plan(
        self,
        *,
        message: dict,
        plan: _InboundDeliveryPlan,
    ) -> _InboundDeliveryExecutionResult:
        reply_outbox_id = plan["reply_outbox_id"]
        allow_duplicate_content = (
            bool(str(plan["msg_id"] or "").strip())
            and int(message.get("_cooling_retry_count", 0) or 0) <= 0
            and int(message.get("_retry_count", 0) or 0) <= 0
            and int(message.get("_submit_retry_count", 0) or 0) <= 0
        )
        delivery_started_at = time.perf_counter()
        delivery_result = self._deps.reply_sender.attempt_reply_delivery(
            customer_name=plan["customer_name"],
            reply_content=plan["reply_content"],
            conversation_id=plan["conversation_id"],
            reply_msg_id=plan["reply_msg_id"],
            customer_id=plan["customer_id"],
            platform=plan["platform"],
            intent_level=plan["intent_level"],
            intent_score=plan["intent_score"],
            smart_result=plan["smart_result"],
            session=plan["session"],
            original_content=plan["content"],
            logical_message_id=plan["reply_logical_message_id"],
            outbox_id=reply_outbox_id,
            allow_duplicate_content=allow_duplicate_content,
            duplicate_as_success=False,
            cancel_duplicate_reservation=True,
        )
        delivery_status = str(delivery_result.get("status", "") or "")
        delivery_trace = self._build_delivery_trace_payload(
            plan=plan,
            delivery_result=delivery_result,
            allow_duplicate_content=allow_duplicate_content,
        )
        self._append_delivery_trace_span(
            delivery_trace,
            name="reply_delivery_attempt",
            started_at=delivery_started_at,
            delivery_status=delivery_status,
            metadata={
                "success": bool(delivery_result.get("success", False)),
                "message_id": str(delivery_result.get("message_id", "") or ""),
                "handler": "attempt_reply_delivery",
            },
        )

        if delivery_status == "blocked":
            self._deps.reply_sender.handle_inbound_cooling_retry(
                message=message,
                customer_name=plan["customer_name"],
                content=plan["content"],
                msg_id=plan["msg_id"],
                logical_message_id=plan["logical_message_id"],
                workflow_run_id=plan["workflow_run_id"],
                reply_outbox_id=reply_outbox_id,
                smart_result=plan["smart_result"],
                reply_content=plan["reply_content"],
                blocked_reason=str(delivery_result.get("message", "") or ""),
            )
            self._append_delivery_trace_span(
                delivery_trace,
                name="reply_delivery_chain",
                started_at=delivery_started_at,
                delivery_status=delivery_status,
                metadata={
                    "should_update_intent": False,
                    "handler": "cooling_retry",
                    "retry_scheduled": True,
                    "reason": str(delivery_result.get("message", "") or ""),
                },
            )
            return {
                "should_update_intent": False,
                "reply_outbox_id": reply_outbox_id,
                "delivery_trace": delivery_trace,
            }

        logger.info(
            f"智能回复: {plan['reply_content'][:30]}... "
            f"(意向等级: {plan['intent_level']}, 分数: {plan['intent_score']})"
        )
        try:
            should_update_intent = self._deps.reply_sender.handle_inbound_delivery_result(
                customer_name=plan["customer_name"],
                content=plan["content"],
                msg_id=plan["msg_id"],
                customer_id=plan["customer_id"],
                logical_message_id=plan["logical_message_id"],
                workflow_run_id=plan["workflow_run_id"],
                conversation_id=plan["conversation_id"],
                reply_content=plan["reply_content"],
                reply_msg_id=plan["reply_msg_id"],
                reply_outbox_id=reply_outbox_id,
                trace_id=plan["trace_id"],
                delivery_result=delivery_result,
            )
        except Exception as exc:
            self._append_delivery_trace_span(
                delivery_trace,
                name="reply_delivery_chain",
                started_at=delivery_started_at,
                delivery_status=delivery_status or "handler_error",
                metadata={
                    "should_update_intent": False,
                    "handler": "handle_inbound_delivery_result",
                },
                error=str(exc),
            )
            raise
        self._append_delivery_trace_span(
            delivery_trace,
            name="reply_delivery_chain",
            started_at=delivery_started_at,
            delivery_status=delivery_status,
            metadata={
                "should_update_intent": bool(should_update_intent),
                "handler": "handle_inbound_delivery_result",
                "reason": str(
                    delivery_result.get("message", "")
                    or delivery_result.get("failure_reason", "")
                    or ""
                ),
            },
        )
        return {
            "should_update_intent": should_update_intent,
            "reply_outbox_id": reply_outbox_id,
            "delivery_trace": delivery_trace,
        }

    def update_customer_intent_if_needed(
        self,
        *,
        customer_id: str,
        platform: str,
        intent_level: str,
        intent_score: float,
    ) -> None:
        if not customer_id or customer_id.startswith("temp_"):
            return
        try:
            self._deps.access.intent_access.customer_intent_gateway.update_customer_intent(
                customer_id, platform, intent_level, intent_score
            )
        except Exception as intent_e:
            logger.debug(f"更新客户意向等级失败: {intent_e}")

    def handle_pipeline_exception(
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
        reason = f"pipeline_exception:{str(exc)[:80]}"
        logger.error(f"自动回复失败: {exc}")
        import traceback

        logger.error(f"异常堆栈：{traceback.format_exc()}")
        try:
            if customer_name and content:
                self._deps.state_repository.mark_message_failed(customer_name, content, msg_id)
        except Exception as mark_e:
            logger.debug(f"外层异常后标记消息失败状态出错: {mark_e}")
        try:
            if logical_message_id:
                self._deps.state_repository.finalize_inbound_event_safe(
                    logical_message_id,
                    status="failed",
                    source_message_id=msg_id,
                    reason=reason,
                )
        except Exception as finalize_e:
            logger.debug(f"外层异常后更新入站逻辑事件失败: {finalize_e}")
        try:
            if workflow_run_id:
                if reply_outbox_id:
                    self._deps.state_repository.bind_workflow_outbox(
                        workflow_run_id, reply_outbox_id
                    )
                self._deps.state_repository.fail_workflow_run(
                    workflow_run_id, reason
                )
        except Exception as workflow_e:
            logger.debug(f"外层异常后收口工作流失败: {workflow_e}")
        try:
            if reply_outbox_id:
                self._deps.state_repository.update_outbox_event(
                    reply_outbox_id,
                    status="failed",
                    message_id=reply_msg_id,
                    reason=reason,
                    workflow_run_id=workflow_run_id or None,
                )
        except Exception as outbox_e:
            logger.debug(f"外层异常后更新 outbox 失败: {outbox_e}")


class _InboundReplyOrchestrator:
    """封装主回复链总控，让 BotService 仅保留装配入口。"""

    def __init__(self, deps: _InboundReplyOrchestratorDeps):
        self._deps = deps

    def run(self, message: Any) -> None:
        reply_outbox_id = ""
        request: Optional[_InboundReplyRequest] = None
        plan: Optional[_InboundDeliveryPlan] = None
        normalized_message = message if isinstance(message, dict) else {}

        try:
            request = self._deps.planner.prepare_request(normalized_message)
            if not request:
                return

            plan = self._deps.planner.prepare_plan(message=normalized_message, request=request)
            if not plan:
                return

            reply_outbox_id = plan["reply_outbox_id"]
            execution_result = self._deps.executor.execute_plan(
                message=normalized_message,
                plan=plan,
            )
            reply_outbox_id = execution_result["reply_outbox_id"]
            if not execution_result["should_update_intent"]:
                return

            self._deps.executor.update_customer_intent_if_needed(
                customer_id=plan["customer_id"],
                platform=plan["platform"],
                intent_level=plan["intent_level"],
                intent_score=plan["intent_score"],
            )
        except Exception as exc:
            fallback = plan or request or {}
            self._deps.executor.handle_pipeline_exception(
                customer_name=str(fallback.get("customer_name", "") or ""),
                content=str(fallback.get("content", "") or ""),
                msg_id=str(fallback.get("msg_id", "") or ""),
                logical_message_id=str(fallback.get("logical_message_id", "") or ""),
                workflow_run_id=str(fallback.get("workflow_run_id", "") or ""),
                reply_outbox_id=reply_outbox_id,
                reply_msg_id=str(fallback.get("reply_msg_id", "") or ""),
                exc=exc,
            )


class _InboundReplyComponentFactory:
    """统一装配主回复链协作对象，便于 BotService 继续下沉装配细节。"""

    def __init__(self, deps: _InboundReplyFactoryDeps):
        self._deps = deps

    def build_planner(self) -> _InboundReplyPlanner:
        return _InboundReplyPlanner(
            _InboundReplyPlannerDeps(
                request=self._deps.request,
                reply_generator=self.build_reply_generator(),
                access=self._deps.planner_access,
            )
        )

    def build_executor(self) -> _InboundReplyExecutor:
        return _InboundReplyExecutor(
            _InboundReplyExecutorDeps(
                reply_sender=self.build_reply_sender(),
                state_repository=self.build_message_state_repository(),
                access=self._deps.executor_access,
            )
        )

    def build_reply_generator(self) -> _ReplyGenerator:
        return _ReplyGenerator.from_plan_deps(self._deps.plan)

    def build_reply_sender(self) -> _ReplySender:
        return _ReplySender.from_execution_deps(self._deps.execution)

    def build_message_state_repository(self) -> _MessageStateRepository:
        return _MessageStateRepository(
            finalize_inbound_event_safe=(
                self._deps.executor_access.failure_access.state_access.inbound_state_access.inbound_gateway.finalize_inbound_event_safe
            ),
            update_outbox_event=(
                self._deps.executor_access.failure_access.state_access.outbox_state_access.outbox_gateway.update_outbox_event
            ),
            bind_workflow_outbox=(
                self._deps.executor_access.failure_access.workflow_access.binding_access.workflow_gateway.bind_workflow_outbox
            ),
            fail_workflow_run=(
                self._deps.executor_access.failure_access.workflow_access.failure_access.workflow_gateway.fail_workflow_run
            ),
            mark_message_failed=self._deps.failure.mark_message_failed,
        )

    def build_orchestrator(self) -> _InboundReplyOrchestrator:
        return _InboundReplyOrchestrator(
            _InboundReplyOrchestratorDeps(
                planner=self.build_planner(),
                executor=self.build_executor(),
            )
        )
