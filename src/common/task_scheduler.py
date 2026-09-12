"""
定时任务调度器
管理智能学习系统的定时任务，如知识衰减、清理、统计等
"""
import asyncio
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from loguru import logger


class TaskStatus(Enum):
    """
    任务状态枚举
    """
    PENDING = "pending"         # 待执行
    RUNNING = "running"         # 执行中
    COMPLETED = "completed"     # 已完成
    FAILED = "failed"           # 失败
    DISABLED = "disabled"       # 已禁用


class TaskPriority(Enum):
    """
    任务优先级
    """
    HIGH = 1
    MEDIUM = 2
    LOW = 3


@dataclass
class ScheduledTask:
    """
    定时任务
    
    定义一个定时执行的任务
    """
    task_id: str
    name: str
    description: str
    
    # 执行配置
    interval_seconds: int           # 执行间隔（秒）
    priority: TaskPriority = TaskPriority.MEDIUM
    
    # 状态
    status: TaskStatus = TaskStatus.PENDING
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    last_result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    
    # 统计
    run_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    total_runtime: float = 0.0
    
    # 回调函数
    callback: Optional[Callable] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'task_id': self.task_id,
            'name': self.name,
            'description': self.description,
            'interval_seconds': self.interval_seconds,
            'priority': self.priority.value,
            'status': self.status.value,
            'last_run': self.last_run.isoformat() if self.last_run else None,
            'next_run': self.next_run.isoformat() if self.next_run else None,
            'last_result': self.last_result,
            'error_message': self.error_message,
            'run_count': self.run_count,
            'success_count': self.success_count,
            'fail_count': self.fail_count,
            'average_runtime': self.total_runtime / max(self.run_count, 1)
        }


@dataclass
class SchedulerConfig:
    """
    调度器配置
    
    定义调度器的各项参数
    """
    # 检查间隔（秒）
    check_interval: int = 60
    
    # 最大并发任务数
    max_concurrent_tasks: int = 3
    
    # 任务超时时间（秒）
    task_timeout: int = 300
    
    # 失败重试次数
    max_retries: int = 3
    
    # 重试间隔（秒）
    retry_interval: int = 60


class TaskScheduler:
    """
    定时任务调度器
    
    功能：
    - 管理定时任务
    - 执行任务调度
    - 任务状态跟踪
    - 失败重试
    """
    
    # 预定义任务
    PREDEFINED_TASKS = {
        'knowledge_decay': {
            'name': '知识衰减处理',
            'description': '应用知识衰减机制，更新知识优先级',
            'interval_seconds': 3600,  # 每小时
            'priority': TaskPriority.MEDIUM
        },
        'knowledge_cleanup': {
            'name': '知识清理',
            'description': '清理过期和低质量知识',
            'interval_seconds': 86400,  # 每天
            'priority': TaskPriority.LOW
        },
        'quality_assessment': {
            'name': '质量评估',
            'description': '评估知识库整体质量',
            'interval_seconds': 21600,  # 每6小时
            'priority': TaskPriority.MEDIUM
        },
        'conflict_detection': {
            'name': '冲突检测',
            'description': '检测知识库中的冲突',
            'interval_seconds': 43200,  # 每12小时
            'priority': TaskPriority.MEDIUM
        },
        'active_learning': {
            'name': '主动学习',
            'description': '生成学习任务和知识缺口报告',
            'interval_seconds': 7200,  # 每2小时
            'priority': TaskPriority.HIGH
        },
        'memory_tier_management': {
            'name': '记忆层级管理',
            'description': '管理分层记忆，执行晋升和降级',
            'interval_seconds': 1800,  # 每30分钟
            'priority': TaskPriority.MEDIUM
        },
        'statistics_update': {
            'name': '统计更新',
            'description': '更新系统统计数据',
            'interval_seconds': 600,  # 每10分钟
            'priority': TaskPriority.LOW
        }
    }
    
    def __init__(self, config: SchedulerConfig = None, learning_system=None):
        """
        初始化任务调度器
        
        Args:
            config: 调度器配置
            learning_system: 学习系统实例
        """
        self.config = config or SchedulerConfig()
        self.learning_system = learning_system
        
        # 任务存储
        self._tasks: Dict[str, ScheduledTask] = {}
        
        # 运行状态
        self._running = False
        self._scheduler_task: Optional[asyncio.Task] = None
        
        # 并发控制
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)
        
        # 初始化预定义任务
        self._init_predefined_tasks()

    def _list_learning_knowledge_items(self) -> List[Any]:
        if not self.learning_system:
            return []
        knowledge_base = getattr(self.learning_system, "knowledge_base", None)
        if knowledge_base is None:
            return []
        if hasattr(knowledge_base, "list_knowledge_items"):
            return list(knowledge_base.list_knowledge_items() or [])
        if hasattr(knowledge_base, "get_knowledge_list"):
            return list(knowledge_base.get_knowledge_list() or [])
        if hasattr(knowledge_base, "get_all_items"):
            return list(knowledge_base.get_all_items() or [])
        return list(getattr(knowledge_base, "knowledge_items", []) or [])
    
    def _init_predefined_tasks(self):
        """
        初始化预定义任务
        """
        for task_id, task_config in self.PREDEFINED_TASKS.items():
            task = ScheduledTask(
                task_id=task_id,
                name=task_config['name'],
                description=task_config['description'],
                interval_seconds=task_config['interval_seconds'],
                priority=task_config['priority'],
                status=TaskStatus.PENDING
            )
            task.next_run = datetime.now() + timedelta(seconds=task.interval_seconds)
            self._tasks[task_id] = task
    
    def register_task(
        self,
        task_id: str,
        name: str,
        callback: Callable,
        interval_seconds: int,
        description: str = "",
        priority: TaskPriority = TaskPriority.MEDIUM
    ) -> ScheduledTask:
        """
        注册新任务
        
        Args:
            task_id: 任务ID
            name: 任务名称
            callback: 回调函数
            interval_seconds: 执行间隔（秒）
            description: 任务描述
            priority: 任务优先级
            
        Returns:
            创建的任务
        """
        task = ScheduledTask(
            task_id=task_id,
            name=name,
            description=description,
            interval_seconds=interval_seconds,
            priority=priority,
            callback=callback,
            next_run=datetime.now() + timedelta(seconds=interval_seconds)
        )
        
        self._tasks[task_id] = task
        logger.info(f"注册任务: {name} (间隔: {interval_seconds}秒)")
        
        return task
    
    def unregister_task(self, task_id: str) -> bool:
        """
        注销任务
        
        Args:
            task_id: 任务ID
            
        Returns:
            是否成功
        """
        if task_id in self._tasks:
            del self._tasks[task_id]
            logger.info(f"注销任务: {task_id}")
            return True
        return False
    
    def enable_task(self, task_id: str) -> bool:
        """
        启用任务
        
        Args:
            task_id: 任务ID
            
        Returns:
            是否成功
        """
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.PENDING
            task.next_run = datetime.now() + timedelta(seconds=task.interval_seconds)
            logger.info(f"启用任务: {task.name}")
            return True
        return False
    
    def disable_task(self, task_id: str) -> bool:
        """
        禁用任务
        
        Args:
            task_id: 任务ID
            
        Returns:
            是否成功
        """
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.DISABLED
            logger.info(f"禁用任务: {task.name}")
            return True
        return False
    
    async def start(self):
        """
        启动调度器
        """
        if self._running:
            logger.warning("调度器已在运行")
            return
        
        self._running = True
        self._scheduler_task = asyncio.create_task(self._run_scheduler())
        logger.info("任务调度器已启动")
    
    async def stop(self):
        """
        停止调度器
        """
        self._running = False
        
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        
        logger.info("任务调度器已停止")
    
    async def _run_scheduler(self):
        """
        运行调度循环
        """
        while self._running:
            try:
                await self._check_and_execute_tasks()
                await asyncio.sleep(self.config.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"调度循环错误: {e}")
                await asyncio.sleep(self.config.check_interval)
    
    async def _check_and_execute_tasks(self):
        """
        检查并执行到期任务
        """
        now = datetime.now()
        
        # 获取到期任务
        due_tasks = [
            task for task in self._tasks.values()
            if (task.status == TaskStatus.PENDING and 
                task.next_run and 
                task.next_run <= now)
        ]
        
        # 按优先级排序
        due_tasks.sort(key=lambda t: t.priority.value)
        
        # 执行任务
        for task in due_tasks:
            if self._queue_task_execution(task, now=now):
                asyncio.create_task(self._execute_task(task))

    def _queue_task_execution(self, task: ScheduledTask, now: Optional[datetime] = None) -> bool:
        """在创建协程前先原子切换任务状态，避免重复排队。"""
        normalized_now = now or datetime.now()
        if task.status != TaskStatus.PENDING:
            return False
        if task.next_run and task.next_run > normalized_now:
            return False
        task.status = TaskStatus.RUNNING
        task.last_run = normalized_now
        return True
    
    async def _execute_task(self, task: ScheduledTask):
        """
        执行单个任务
        
        Args:
            task: 任务对象
        """
        async with self._semaphore:
            start_time = datetime.now()
            
            try:
                # 执行任务回调
                result = await self._run_task_callback(task)
                
                # 更新结果
                task.last_result = result
                task.success_count += 1
                task.status = TaskStatus.COMPLETED
                task.error_message = None
                
                logger.info(f"任务完成: {task.name}")
                
            except Exception as e:
                task.fail_count += 1
                task.error_message = str(e)
                task.status = TaskStatus.FAILED
                
                logger.error(f"任务失败: {task.name}, 错误: {e}")
                
                # 检查是否需要重试
                if task.fail_count < self.config.max_retries:
                    task.status = TaskStatus.PENDING
                    task.next_run = datetime.now() + timedelta(seconds=self.config.retry_interval)
                    logger.info(f"任务将重试: {task.name}")
                    return
            
            finally:
                # 更新统计
                runtime = (datetime.now() - start_time).total_seconds()
                task.total_runtime += runtime
                task.run_count += 1
                
                # 计算下次执行时间
                if task.status == TaskStatus.COMPLETED:
                    task.status = TaskStatus.PENDING
                    task.next_run = datetime.now() + timedelta(seconds=task.interval_seconds)
    
    async def _run_task_callback(self, task: ScheduledTask) -> Dict[str, Any]:
        """
        运行任务回调
        
        Args:
            task: 任务对象
            
        Returns:
            执行结果
        """
        # 预定义任务处理
        if task.task_id in self.PREDEFINED_TASKS:
            return await self._run_predefined_task(task.task_id)
        
        # 自定义回调
        if task.callback:
            if asyncio.iscoroutinefunction(task.callback):
                return await task.callback()
            else:
                return task.callback()
        
        return {'status': 'no_callback'}
    
    async def _run_predefined_task(self, task_id: str) -> Dict[str, Any]:
        """
        执行预定义任务
        
        Args:
            task_id: 任务ID
            
        Returns:
            执行结果
        """
        if not self.learning_system:
            return {'status': 'no_learning_system'}
        
        result = {'task_id': task_id, 'executed_at': datetime.now().isoformat()}
        
        try:
            if task_id == 'knowledge_decay':
                decay_result = self.learning_system.apply_knowledge_decay()
                result['decay_result'] = decay_result
                
            elif task_id == 'knowledge_cleanup':
                if hasattr(self.learning_system, 'decay_manager'):
                    cleaned = self.learning_system.decay_manager.cleanup_old_data(90)
                    result['cleaned_count'] = cleaned
                    
            elif task_id == 'quality_assessment':
                if hasattr(self.learning_system, 'get_quality_report'):
                    report = self.learning_system.get_quality_report()
                    result['quality_report'] = report
                    
            elif task_id == 'conflict_detection':
                if hasattr(self.learning_system, 'conflict_detector'):
                    conflicts = self.learning_system.conflict_detector.detect_all_conflicts(
                        self._list_learning_knowledge_items()
                    )
                    result['conflicts_found'] = len(conflicts)
                    
            elif task_id == 'active_learning':
                if hasattr(self.learning_system, 'active_learner'):
                    gaps = self.learning_system.active_learner.export_gaps()
                    result['gaps_found'] = len(gaps)
                    
            elif task_id == 'memory_tier_management':
                if hasattr(self.learning_system, 'decay_manager'):
                    cleaned = self.learning_system.decay_manager.cleanup_expired()
                    result['expired_cleaned'] = cleaned
                    
            elif task_id == 'statistics_update':
                stats = self.learning_system.get_enhanced_stats()
                result['stats'] = stats
                
            result['status'] = 'success'
            
        except Exception as e:
            result['status'] = 'error'
            result['error'] = str(e)
            raise
        
        return result
    
    def run_task_now(self, task_id: str) -> bool:
        """
        立即执行任务
        
        Args:
            task_id: 任务ID
            
        Returns:
            是否成功启动
        """
        task = self._tasks.get(task_id)
        now = datetime.now()
        if task and task.status != TaskStatus.RUNNING:
            task.next_run = now
            if not self._queue_task_execution(task, now=now):
                return False
            asyncio.create_task(self._execute_task(task))
            return True
        return False
    
    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        """
        获取任务
        
        Args:
            task_id: 任务ID
            
        Returns:
            任务对象或None
        """
        return self._tasks.get(task_id)
    
    def get_all_tasks(self) -> List[ScheduledTask]:
        """
        获取所有任务
        
        Returns:
            任务列表
        """
        return list(self._tasks.values())
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        获取任务状态
        
        Args:
            task_id: 任务ID
            
        Returns:
            任务状态字典或None
        """
        task = self._tasks.get(task_id)
        if task:
            return task.to_dict()
        return None
    
    def get_scheduler_status(self) -> Dict[str, Any]:
        """
        获取调度器状态
        
        Returns:
            调度器状态字典
        """
        total_tasks = len(self._tasks)
        running_tasks = sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)
        pending_tasks = sum(1 for t in self._tasks.values() if t.status == TaskStatus.PENDING)
        disabled_tasks = sum(1 for t in self._tasks.values() if t.status == TaskStatus.DISABLED)
        
        return {
            'running': self._running,
            'total_tasks': total_tasks,
            'running_tasks': running_tasks,
            'pending_tasks': pending_tasks,
            'disabled_tasks': disabled_tasks,
            'config': {
                'check_interval': self.config.check_interval,
                'max_concurrent_tasks': self.config.max_concurrent_tasks,
                'task_timeout': self.config.task_timeout
            }
        }
    
    def update_task_interval(self, task_id: str, interval_seconds: int) -> bool:
        """
        更新任务执行间隔
        
        Args:
            task_id: 任务ID
            interval_seconds: 新的执行间隔
            
        Returns:
            是否成功
        """
        task = self._tasks.get(task_id)
        if task:
            task.interval_seconds = interval_seconds
            if task.next_run:
                task.next_run = task.last_run + timedelta(seconds=interval_seconds) if task.last_run else datetime.now() + timedelta(seconds=interval_seconds)
            logger.info(f"更新任务间隔: {task.name} -> {interval_seconds}秒")
            return True
        return False


# 全局实例
_task_scheduler = None


def get_task_scheduler(config: SchedulerConfig = None, learning_system=None) -> TaskScheduler:
    """
    获取任务调度器单例
    
    Args:
        config: 调度器配置
        learning_system: 学习系统实例
        
    Returns:
        任务调度器实例
    """
    global _task_scheduler
    if _task_scheduler is None:
        _task_scheduler = TaskScheduler(config, learning_system)
    return _task_scheduler
