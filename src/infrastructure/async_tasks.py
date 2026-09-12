# -*- coding: utf-8 -*-
"""
异步任务模块
提供企业级应用的异步任务处理功能
"""
import asyncio
import time
import uuid
import threading
from typing import Any, Callable, Dict, List, Optional, Union
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

from src.infrastructure.logger import get_logger

logger = get_logger("async_tasks")


class TaskStatus(str, Enum):
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(int, Enum):
    """任务优先级"""
    LOW = 1
    NORMAL = 5
    HIGH = 10
    CRITICAL = 20


@dataclass
class TaskResult:
    """任务结果"""
    task_id: str
    status: TaskStatus
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    """任务定义"""
    task_id: str
    func: Callable
    args: tuple
    kwargs: dict
    priority: TaskPriority = TaskPriority.NORMAL
    timeout: Optional[int] = None
    retry_count: int = 0
    max_retries: int = 3
    created_at: datetime = field(default_factory=datetime.now)
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[TaskResult] = None


class AsyncTaskQueue:
    """
    异步任务队列
    
    特点：
    - 优先级队列
    - 超时控制
    - 重试机制
    - 并发控制
    """
    
    def __init__(
        self,
        max_workers: int = 10,
        max_queue_size: int = 1000,
        default_timeout: int = 300
    ):
        self.max_workers = max_workers
        self.max_queue_size = max_queue_size
        self.default_timeout = default_timeout
        
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=max_queue_size)
        self._tasks: Dict[str, Task] = {}
        self._results: Dict[str, TaskResult] = {}
        self._max_results = 1000
        self._workers: List[asyncio.Task] = []
        self._running = False
        self._semaphore = asyncio.Semaphore(max_workers)
    
    def _generate_task_id(self) -> str:
        """生成任务ID"""
        return f"task_{uuid.uuid4().hex[:12]}"
    
    async def submit(
        self,
        func: Callable,
        *args,
        priority: TaskPriority = TaskPriority.NORMAL,
        timeout: Optional[int] = None,
        max_retries: int = 3,
        **kwargs
    ) -> str:
        """
        提交任务
        
        Args:
            func: 任务函数
            *args: 位置参数
            priority: 优先级
            timeout: 超时时间(秒)
            max_retries: 最大重试次数
            **kwargs: 关键字参数
            
        Returns:
            任务ID
        """
        task_id = self._generate_task_id()
        
        task = Task(
            task_id=task_id,
            func=func,
            args=args,
            kwargs=kwargs,
            priority=priority,
            timeout=timeout or self.default_timeout,
            max_retries=max_retries
        )
        
        self._tasks[task_id] = task
        
        await self._queue.put((-priority.value, task.created_at.timestamp(), task_id))
        
        logger.info(f"任务已提交: {task_id}, 优先级: {priority.name}")
        
        if not self._running:
            await self.start()
        
        return task_id
    
    async def _execute_task(self, task: Task) -> TaskResult:
        """执行任务"""
        result = TaskResult(
            task_id=task.task_id,
            status=TaskStatus.RUNNING,
            started_at=datetime.now()
        )
        
        try:
            if asyncio.iscoroutinefunction(task.func):
                output = await asyncio.wait_for(
                    task.func(*task.args, **task.kwargs),
                    timeout=task.timeout
                )
            else:
                import functools
                loop = asyncio.get_running_loop()
                wrapped = functools.partial(task.func, *task.args, **task.kwargs)
                future = loop.run_in_executor(
                    None,
                    wrapped
                )
                output = await asyncio.wait_for(future, timeout=task.timeout)

            result.status = TaskStatus.COMPLETED
            result.result = output
                
        except asyncio.TimeoutError:
            result.status = TaskStatus.FAILED
            result.error = f"任务超时 ({task.timeout}秒)"
            logger.warning(f"任务超时: {task.task_id}")
            
        except asyncio.CancelledError:
            result.status = TaskStatus.CANCELLED
            result.error = "任务被取消"
            logger.info(f"任务取消: {task.task_id}")
            
        except Exception as e:
            result.status = TaskStatus.FAILED
            result.error = str(e)
            logger.error(f"任务失败: {task.task_id}, 错误: {e}")
            
            if task.retry_count < task.max_retries:
                task.retry_count += 1
                task.status = TaskStatus.PENDING
                await self._queue.put((-task.priority.value, time.time(), task.task_id))
                logger.info(f"任务重试: {task.task_id}, 第{task.retry_count}次")
        
        result.completed_at = datetime.now()
        result.duration_ms = (result.completed_at - result.started_at).total_seconds() * 1000
        
        return result
    
    async def _worker(self):
        """工作协程"""
        while self._running:
            try:
                _, _, task_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                
                task = self._tasks.get(task_id)
                if not task:
                    continue
                
                async with self._semaphore:
                    task.status = TaskStatus.RUNNING
                    result = await self._execute_task(task)
                    self._results[task_id] = result
                    task.result = result
                    task.status = result.status
                    if result.status != TaskStatus.PENDING:
                        if task_id in self._tasks:
                            del self._tasks[task_id]
                    if len(self._results) > self._max_results:
                        oldest_keys = list(self._results.keys())[:len(self._results) - self._max_results]
                        for k in oldest_keys:
                            del self._results[k]
                    
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"工作协程错误: {e}")
    
    async def start(self):
        """启动任务队列"""
        if self._running:
            return
        
        self._running = True
        
        for _ in range(self.max_workers):
            worker = asyncio.create_task(self._worker())
            self._workers.append(worker)
        
        logger.info(f"任务队列已启动, 工作协程数: {self.max_workers}")
    
    async def stop(self):
        """停止任务队列"""
        self._running = False
        
        for worker in self._workers:
            worker.cancel()
        
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        
        logger.info("任务队列已停止")
    
    def get_status(self, task_id: str) -> Optional[TaskResult]:
        """获取任务状态"""
        return self._results.get(task_id) or self._tasks.get(task_id)
    
    def get_result(self, task_id: str) -> Optional[Any]:
        """获取任务结果"""
        result = self._results.get(task_id)
        if result and result.status == TaskStatus.COMPLETED:
            return result.result
        return None
    
    def get_stats(self) -> Dict[str, Any]:
        """获取队列统计"""
        return {
            "running": self._running,
            "queue_size": self._queue.qsize(),
            "total_tasks": len(self._tasks),
            "completed_tasks": len(self._results),
            "workers": len(self._workers),
            "max_workers": self.max_workers
        }


class BackgroundTaskManager:
    """
    后台任务管理器
    
    管理周期性任务和后台服务
    """
    
    def __init__(self):
        self._tasks: Dict[str, asyncio.Task] = {}
        self._running = False
    
    async def add_periodic_task(
        self,
        name: str,
        func: Callable,
        interval: float,
        *args,
        **kwargs
    ):
        """
        添加周期性任务
        
        Args:
            name: 任务名称
            func: 任务函数
            interval: 执行间隔(秒)
            *args: 位置参数
            **kwargs: 关键字参数
        """
        async def periodic():
            while self._running:
                try:
                    if asyncio.iscoroutinefunction(func):
                        await func(*args, **kwargs)
                    else:
                        func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"周期任务错误 [{name}]: {e}")
                
                await asyncio.sleep(interval)
        
        self._tasks[name] = asyncio.create_task(periodic())
        logger.info(f"周期任务已添加: {name}, 间隔: {interval}秒")
    
    async def start(self):
        """启动管理器"""
        self._running = True
        logger.info("后台任务管理器已启动")
    
    async def stop(self):
        """停止管理器"""
        self._running = False
        
        for name, task in self._tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        self._tasks.clear()
        logger.info("后台任务管理器已停止")
    
    def get_task_names(self) -> List[str]:
        """获取所有任务名称"""
        return list(self._tasks.keys())


_task_queue: Optional[AsyncTaskQueue] = None
_background_manager: Optional[BackgroundTaskManager] = None
_task_queue_lock = threading.Lock()
_background_manager_lock = threading.Lock()


def get_task_queue() -> AsyncTaskQueue:
    """获取任务队列实例（线程安全）"""
    global _task_queue
    if _task_queue is None:
        with _task_queue_lock:
            if _task_queue is None:
                _task_queue = AsyncTaskQueue()
    return _task_queue


def get_background_manager() -> BackgroundTaskManager:
    """获取后台任务管理器实例（线程安全）"""
    global _background_manager
    if _background_manager is None:
        with _background_manager_lock:
            if _background_manager is None:
                _background_manager = BackgroundTaskManager()
    return _background_manager


def async_task(
    priority: TaskPriority = TaskPriority.NORMAL,
    timeout: Optional[int] = None,
    max_retries: int = 0
):
    """
    异步任务装饰器
    
    将函数标记为可异步执行的任务
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            queue = get_task_queue()
            task_id = await queue.submit(
                func,
                *args,
                priority=priority,
                timeout=timeout,
                max_retries=max_retries,
                **kwargs
            )
            return task_id
        
        wrapper.submit = wrapper
        return wrapper
    
    return decorator
