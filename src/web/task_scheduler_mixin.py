"""
TaskSchedulerMixin - Worker线程任务调度相关方法

从 BotService 中提取的任务调度方法集合，包含：
1. 任务队列创建（_create_task_queue）
2. 任务优先级推断（_infer_task_priority）
3. 任务项构建与规范化（_build_task_item / _build_shutdown_task_item / _normalize_task_item / _extract_task_payload）
4. Worker线程主循环（_worker_loop）
5. 任务提交（_submit_task / _submit_task_no_wait）
6. Worker线程存活检测（_ensure_worker_alive）
7. 等待间隔任务处理（_process_pending_tasks_during_wait）
"""
import queue
import threading
import time
import itertools
from typing import Optional

from concurrent.futures import Future
from loguru import logger


class TaskSchedulerMixin:
    """任务调度 Mixin —— 从 BotService 提取的 Worker 线程任务调度相关方法。"""

    # 以下常量在 BotService 中定义，Mixin 通过 self 访问：
    #   TASK_PRIORITY_HIGH = 0
    #   TASK_PRIORITY_NORMAL = 50
    #   TASK_PRIORITY_LOW = 100

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
