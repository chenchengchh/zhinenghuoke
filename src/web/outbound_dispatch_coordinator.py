from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional

from loguru import logger

from src.common.monitoring import get_metrics
from src.web.outbound_send_gateway import OutboundSendResult


@dataclass
class _DispatchTask:
    conversation_key: str
    customer_name: str
    conversation_id: str
    customer_id: str
    platform: str
    task_id: str
    trace_id: str
    task: Callable[[], OutboundSendResult]
    timeout_seconds: float
    created_at: float = field(default_factory=time.time)
    future: Future = field(default_factory=Future)


class OutboundDispatchCoordinator:
    """出站调度器：按会话排队，轮转调度到单一发送通道。"""

    def __init__(self, *, autostart: bool = True):
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._queues: Dict[str, Deque[_DispatchTask]] = {}
        self._conversation_order: Deque[str] = deque()
        self._recent_events: Deque[dict[str, object]] = deque(maxlen=50)
        self._shutdown = False
        self._worker_thread: Optional[threading.Thread] = None
        if autostart:
            self.start()

    @staticmethod
    def _build_conversation_key(
        *,
        conversation_id: str = "",
        customer_id: str = "",
        customer_name: str = "",
        platform: str = "douyin",
    ) -> str:
        normalized_platform = str(platform or "douyin").strip() or "douyin"
        normalized_conversation_id = str(conversation_id or "").strip()
        if normalized_conversation_id:
            return f"conv:{normalized_platform}:{normalized_conversation_id}"
        normalized_customer_id = str(customer_id or "").strip()
        if normalized_customer_id:
            return f"cust:{normalized_platform}:{normalized_customer_id}"
        normalized_customer_name = str(customer_name or "").strip() or "unknown"
        return f"name:{normalized_platform}:{normalized_customer_name}"

    def start(self) -> None:
        with self._condition:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._shutdown = False
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="outbound-dispatch-worker",
                daemon=True,
            )
            self._worker_thread.start()

    def shutdown(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        with self._condition:
            self._shutdown = True
            self._condition.notify_all()
        if wait and self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)

    def submit(
        self,
        *,
        customer_name: str,
        task: Callable[[], OutboundSendResult],
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
        timeout_seconds: float = 30.0,
        trace_id: str = "",
    ) -> Future:
        conversation_key = self._build_conversation_key(
            conversation_id=conversation_id,
            customer_id=customer_id,
            customer_name=customer_name,
            platform=platform,
        )
        dispatch_task = _DispatchTask(
            conversation_key=conversation_key,
            customer_name=str(customer_name or "").strip(),
            conversation_id=str(conversation_id or "").strip(),
            customer_id=str(customer_id or "").strip(),
            platform=str(platform or "douyin").strip() or "douyin",
            task_id=uuid.uuid4().hex[:12],
            trace_id=str(trace_id or "").strip() or uuid.uuid4().hex[:12],
            task=task,
            timeout_seconds=max(float(timeout_seconds or 0), 0.01),
        )
        with self._condition:
            queue = self._queues.get(conversation_key)
            if queue is None:
                queue = deque()
                self._queues[conversation_key] = queue
                self._conversation_order.append(conversation_key)
            queue.append(dispatch_task)
            self._record_event_locked(
                "enqueued",
                task_id=dispatch_task.task_id,
                trace_id=dispatch_task.trace_id,
                conversation_key=conversation_key,
                resolution_level="conversation"
                if dispatch_task.conversation_id
                else ("customer" if dispatch_task.customer_id else "name"),
            )
            self._update_metrics_locked()
            self._condition.notify()
        logger.info(
            f"[出站调度] task_enqueued task_id={dispatch_task.task_id} "
            f"trace={dispatch_task.trace_id} conversation_key={conversation_key} "
            f"customer={dispatch_task.customer_name or '-'}"
        )
        return dispatch_task.future

    def run_or_enqueue(
        self,
        *,
        customer_name: str,
        task: Callable[[], OutboundSendResult],
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
        timeout_seconds: float = 30.0,
        trace_id: str = "",
    ) -> OutboundSendResult:
        future = self.submit(
            customer_name=customer_name,
            task=task,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
            timeout_seconds=timeout_seconds,
            trace_id=trace_id,
        )
        wait_timeout = max(float(timeout_seconds or 0), 0.01) + 15.0
        try:
            return future.result(timeout=wait_timeout)
        except FuturesTimeoutError:
            logger.warning(
                f"[出站调度] dispatch_wait_timeout customer={customer_name or '-'} "
                f"conversation_id={conversation_id or '-'} timeout={wait_timeout:.1f}s"
            )
            return OutboundSendResult(
                success=False,
                channel="dispatch",
                reason="dispatch_wait_timeout",
            )

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._shutdown and not self._conversation_order:
                    self._condition.wait(timeout=0.5)
                if self._shutdown and not self._conversation_order:
                    self._update_metrics_locked()
                    return
                task = self._pop_next_task_locked()
            if task is None:
                continue

            wait_seconds = max(time.time() - task.created_at, 0.0)
            get_metrics().record_outbound_dispatch_wait(wait_seconds)

            if wait_seconds > task.timeout_seconds:
                logger.warning(
                    f"[出站调度] queue_timeout task_id={task.task_id} "
                    f"trace={task.trace_id} conversation_key={task.conversation_key} waited={wait_seconds:.2f}s"
                )
                with self._condition:
                    self._record_event_locked(
                        "queue_timeout",
                        task_id=task.task_id,
                        trace_id=task.trace_id,
                        conversation_key=task.conversation_key,
                        wait_seconds=round(wait_seconds, 3),
                    )
                task.future.set_result(
                    OutboundSendResult(
                        success=False,
                        channel="dispatch",
                        reason="dispatch_queue_timeout",
                    )
                )
                continue

            try:
                logger.info(
                    f"[出站调度] task_started task_id={task.task_id} "
                    f"trace={task.trace_id} conversation_key={task.conversation_key} waited={wait_seconds:.2f}s"
                )
                with self._condition:
                    self._record_event_locked(
                        "started",
                        task_id=task.task_id,
                        trace_id=task.trace_id,
                        conversation_key=task.conversation_key,
                        wait_seconds=round(wait_seconds, 3),
                    )
                result = task.task()
                if not isinstance(result, OutboundSendResult):
                    result = OutboundSendResult(
                        success=bool(getattr(result, "success", False)),
                        channel=str(getattr(result, "channel", "dispatch") or "dispatch"),
                        reason=str(getattr(result, "reason", "") or ""),
                    )
                with self._condition:
                    self._record_event_locked(
                        "finished",
                        task_id=task.task_id,
                        trace_id=task.trace_id,
                        conversation_key=task.conversation_key,
                        result_channel=result.channel,
                        success=result.success,
                    )
                task.future.set_result(result)
            except Exception as exc:
                logger.exception(f"[出站调度] task_failed task_id={task.task_id} trace={task.trace_id}: {exc}")
                with self._condition:
                    self._record_event_locked(
                        "exception",
                        task_id=task.task_id,
                        trace_id=task.trace_id,
                        conversation_key=task.conversation_key,
                        error=str(exc)[:120],
                    )
                task.future.set_result(
                    OutboundSendResult(
                        success=False,
                        channel="dispatch",
                        reason=f"dispatch_exception:{str(exc)[:120]}",
                    )
                )

    def _pop_next_task_locked(self) -> Optional[_DispatchTask]:
        while self._conversation_order:
            conversation_key = self._conversation_order.popleft()
            queue = self._queues.get(conversation_key)
            if not queue:
                self._queues.pop(conversation_key, None)
                continue
            task = queue.popleft()
            if queue:
                self._conversation_order.append(conversation_key)
            else:
                self._queues.pop(conversation_key, None)
            self._update_metrics_locked()
            return task
        self._update_metrics_locked()
        return None

    def _update_metrics_locked(self) -> None:
        queue_size = sum(len(queue) for queue in self._queues.values())
        get_metrics().update_outbound_dispatch_runtime(
            queue_size=queue_size,
            active_conversations=len(self._queues),
        )

    def _record_event_locked(self, event_type: str, **payload: object) -> None:
        event = {
            "event": str(event_type or "").strip(),
            "time": time.time(),
        }
        event.update(payload)
        self._recent_events.append(event)

    def get_runtime_snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "queue_size": sum(len(queue) for queue in self._queues.values()),
                "active_conversations": len(self._queues),
                "queued_conversation_keys": list(self._queues.keys())[:20],
                "recent_events": list(self._recent_events),
            }
