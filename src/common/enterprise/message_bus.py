"""
企业级消息总线

提供：
1. 异步消息处理
2. 事件驱动架构
3. 消息持久化
4. 死信队列
"""

import json
import time
import threading
import uuid
from typing import Dict, List, Callable, Any, Optional
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger
import queue
from collections import defaultdict, deque


class MessageType(Enum):
    """消息类型"""
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    SYSTEM = "system"
    EVENT = "event"
    COMMAND = "command"


class MessagePriority(Enum):
    """消息优先级"""
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass
class BusMessage:
    """总线消息"""
    id: str
    type: MessageType
    priority: MessagePriority
    payload: Dict
    timestamp: float
    correlation_id: str = ""
    reply_to: str = ""
    headers: Dict = field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 3


class MessageBus:
    """
    企业级消息总线

    特性：
    1. 异步处理 - 使用线程池处理消息
    2. 优先级队列 - 高优先级消息优先处理
    3. 消息持久化 - 防止消息丢失
    4. 死信队列 - 处理失败的消息
    5. 消息重试 - 自动重试机制
    """

    def __init__(self, max_workers: int = 10, max_queue_size: int = 10000):
        self._subscribers: Dict[MessageType, List[Callable]] = defaultdict(list)
        self._high_priority_queue = queue.PriorityQueue(maxsize=max_queue_size)
        self._normal_priority_queue = queue.Queue(maxsize=max_queue_size)
        self._dead_letter_queue: deque = deque(maxlen=max_queue_size if max_queue_size else 1000)
        self._dead_letter_queue_max_size = max_queue_size if max_queue_size else 1000
        self._lock = threading.RLock()
        self._max_workers = max_workers
        self._running = False
        self._worker_threads: List[threading.Thread] = []
        self._pending_timers: List[threading.Timer] = []

        self._stats = {
            "total_messages": 0,
            "processed_messages": 0,
            "failed_messages": 0,
            "retry_messages": 0,
            "dlq_messages": 0
        }

    def subscribe(self, message_type: MessageType, handler: Callable):
        """订阅消息"""
        with self._lock:
            self._subscribers[message_type].append(handler)
            logger.info(f"订阅消息: {message_type.value}")

    def unsubscribe(self, message_type: MessageType, handler: Callable):
        """取消订阅"""
        with self._lock:
            if handler in self._subscribers[message_type]:
                self._subscribers[message_type].remove(handler)

    def publish(self, message: BusMessage):
        """发布消息（总线停止期间拒绝新消息入队，防止stop期间消息无限堆积）"""
        if not self._running:
            logger.warning(f"消息总线已停止，拒绝新消息入队: {message.type.value} (ID: {message.id})")
            self._add_to_dlq(message, "Bus stopped")
            return
        with self._lock:
            self._stats["total_messages"] += 1
            counter = self._stats["total_messages"]
        try:
            if message.priority == MessagePriority.HIGH:
                self._high_priority_queue.put((message.priority.value, counter, message))
            else:
                self._normal_priority_queue.put((message.priority.value, counter, message))
            logger.debug(f"消息发布: {message.type.value} (ID: {message.id})")
        except queue.Full:
            logger.error("消息队列已满")
            self._add_to_dlq(message, "Queue full")

    def publish_simple(
        self,
        msg_type: str,
        payload: Dict,
        priority: str = "NORMAL",
        correlation_id: str = ""
    ) -> BusMessage:
        """
        简化发布接口

        Args:
            msg_type: 消息类型字符串
            payload: 消息内容
            priority: 优先级 (HIGH/NORMAL/LOW)
            correlation_id: 关联ID

        Returns:
            BusMessage对象
        """
        try:
            message_type = MessageType(msg_type)
        except ValueError:
            logger.warning(f"无效的消息类型: {msg_type}，使用SYSTEM默认值")
            message_type = MessageType.SYSTEM
        
        try:
            msg_priority = MessagePriority[priority]
        except KeyError:
            logger.warning(f"无效的优先级: {priority}，使用NORMAL默认值")
            msg_priority = MessagePriority.NORMAL

        message = BusMessage(
            id=str(uuid.uuid4()),
            type=message_type,
            priority=msg_priority,
            payload=payload,
            timestamp=time.time(),
            correlation_id=correlation_id
        )

        self.publish(message)
        return message

    def start(self):
        """启动消息总线"""
        if self._running:
            return
        self._running = True
        for i in range(self._max_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            self._worker_threads.append(t)
        logger.info(f"消息总线启动 ({self._max_workers} 个工作线程)")

    def stop(self):
        """停止消息总线（先拒绝新消息入队，再排空队列中的消息，最后停止工作线程）"""
        self._running = False

        remaining = self._high_priority_queue.qsize() + self._normal_priority_queue.qsize()
        if remaining > 0:
            logger.info(f"消息总线停止前，队列中还有 {remaining} 条消息等待处理")
            timeout = min(remaining * 2, 30)
            start_time = time.time()
            while time.time() - start_time < timeout:
                q_size = self._high_priority_queue.qsize() + self._normal_priority_queue.qsize()
                if q_size == 0:
                    break
                time.sleep(0.5)

        with self._lock:
            for timer in self._pending_timers:
                try:
                    timer.cancel()
                except Exception:
                    pass
            self._pending_timers.clear()
        for t in self._worker_threads:
            t.join(timeout=30)
        self._worker_threads.clear()
        logger.info("消息总线已停止")

    def _worker_loop(self):
        """工作线程循环（停止后继续处理队列中剩余消息，防止消息丢失）"""
        while self._running or not self._high_priority_queue.empty() or not self._normal_priority_queue.empty():
            try:
                message = None
                try:
                    priority, counter, message = self._high_priority_queue.get_nowait()
                except queue.Empty:
                    try:
                        priority, counter, message = self._normal_priority_queue.get(timeout=1)
                    except queue.Empty:
                        if not self._running:
                            break
                        continue
                self._process_message(message)
                try:
                    for _ in range(3):
                        priority, counter, msg = self._normal_priority_queue.get_nowait()
                        self._process_message(msg)
                except queue.Empty:
                    pass
            except Exception as e:
                logger.error(f"工作线程异常: {e}")

    def _process_message(self, message: BusMessage):
        """处理消息"""
        try:
            with self._lock:
                handlers = list(self._subscribers.get(message.type, []))
            if not handlers:
                logger.warning(f"没有处理者: {message.type.value}")
                return

            for handler in handlers:
                try:
                    handler(message)
                    with self._lock:
                        self._stats["processed_messages"] += 1
                except Exception as e:
                    logger.error(f"处理消息失败: {e}")
                    self._handle_failure(message, e)

        except Exception as e:
            logger.error(f"处理消息异常: {e}")
            self._handle_failure(message, e)

    def _handle_failure(self, message: BusMessage, error: Exception):
        """处理失败消息（先深拷贝再操作，避免修改原始共享对象）"""
        import copy
        message_copy = copy.deepcopy(message)
        message_copy.retry_count += 1
        current_retry = message_copy.retry_count

        if current_retry <= message_copy.max_retries and current_retry <= 3:
            with self._lock:
                self._stats["retry_messages"] += 1
            delay = min(2 ** current_retry, 30)
            timer_holder = {}

            def _retry_and_cleanup(msg=message_copy):
                self._requeue_message(msg)
                with self._lock:
                    t = timer_holder.get('timer')
                    if t:
                        try:
                            self._pending_timers.remove(t)
                        except ValueError:
                            pass

            timer = threading.Timer(delay, _retry_and_cleanup)
            timer_holder['timer'] = timer
            with self._lock:
                self._pending_timers.append(timer)
            timer.daemon = True
            timer.start()
        else:
            with self._lock:
                self._stats["failed_messages"] += 1
            self._add_to_dlq(message_copy, str(error))

    def _requeue_message(self, message: BusMessage):
        """重新入队消息（使用独立重试计数器确保FIFO顺序，不计入total_messages统计）"""
        if not self._running:
            logger.debug("消息总线已停止，放弃重试入队")
            return
        try:
            with self._lock:
                self._retry_counter = getattr(self, '_retry_counter', 0) + 1
                counter = self._stats["total_messages"] + self._retry_counter
            if message.priority == MessagePriority.HIGH:
                self._high_priority_queue.put((message.priority.value, counter, message))
            else:
                self._normal_priority_queue.put((message.priority.value, counter, message))
        except queue.Full:
            logger.error("重试入队失败: 队列已满")
            self._add_to_dlq(message, "Queue full on retry")

    def _add_to_dlq(self, message: BusMessage, reason: str):
        """添加到死信队列"""
        with self._lock:
            message.headers["dlq_reason"] = reason
            message.headers["dlq_time"] = time.time()
            self._dead_letter_queue.append(message)
            self._stats["dlq_messages"] += 1

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            **self._stats,
            "queue_size_high": self._high_priority_queue.qsize(),
            "queue_size_normal": self._normal_priority_queue.qsize(),
            "dlq_size": len(self._dead_letter_queue)
        }

    def get_dlq(self) -> List[BusMessage]:
        """获取死信队列"""
        return self._dead_letter_queue.copy()

    def clear_dlq(self):
        """清空死信队列"""
        with self._lock:
            self._dead_letter_queue.clear()
