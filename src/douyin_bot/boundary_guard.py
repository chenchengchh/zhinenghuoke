"""
RPA边界保护器

负责：
1. 并发控制
2. 限流保护
3. 失败率监控
4. 自动降级
"""

import time
import threading
from typing import Dict, Optional, Any
from dataclasses import dataclass
from enum import Enum
from loguru import logger


class ProtectionLevel(Enum):
    """保护级别"""
    NORMAL = "normal"
    WARNING = "warning"
    PROTECTED = "protected"
    LOCKED = "locked"


@dataclass
class OperationRecord:
    """操作记录"""
    operation_type: str
    target: str
    success: bool
    duration: float
    timestamp: float
    error: Optional[str] = None


class BoundaryGuard:
    """
    RPA边界保护器

    保护机制：
    1. 并发控制：Semaphore限制并发数
    2. 限流：滑动窗口计数
    3. 失败率：自动触发保护
    4. 熔断：连续失败自动暂停
    """

    MAX_CONCURRENT = 1
    WINDOW_SIZE = 60
    MAX_SEND_PER_WINDOW = 20
    MAX_CLICK_PER_WINDOW = 30
    MAX_FETCH_PER_WINDOW = 60

    FAILURE_RATE_WARNING = 0.3
    FAILURE_RATE_PROTECT = 0.5
    FAILURE_RATE_LOCK = 0.7

    CONSECUTIVE_FAILURES_LOCK = 5
    LOCK_DURATION = 15
    AUTO_RECOVERY_CHECK = 3
    INITIAL_FAILURE_TOLERANCE = 3

    def __init__(self):
        """初始化边界保护器"""
        self._semaphore = threading.Semaphore(self.MAX_CONCURRENT)

        self._operations: Dict[str, list] = {
            'send': [],
            'click': [],
            'fetch': []
        }
        self._lock = threading.RLock()

        self._protection_level = ProtectionLevel.NORMAL
        self._consecutive_failures = 0
        self._locked_until = 0
        self._last_operation_time = 0

        self._recovery_thread = threading.Thread(target=self._recovery_loop, daemon=True)
        self._running = True
        self._stop_event = threading.Event()
        self._recovery_thread.start()
        self._acquired = threading.local()

        logger.info("BoundaryGuard 初始化完成")

    def acquire(self, operation_type: str, timeout: float = 30) -> bool:
        """获取操作许可（先检查状态+尝试恢复，再获取信号量，避免锁内等待信号量导致死锁）"""
        with self._lock:
            if self._is_locked():
                logger.warning(f"操作被锁定，请等待 {self._get_remaining_lock_time():.1f} 秒")
                return False
            if self._is_rate_limited(operation_type):
                logger.warning(f"{operation_type} 操作限流中")
                return False
            self._check_and_try_recovery()

        acquired = self._semaphore.acquire(timeout=timeout)
        if acquired:
            with self._lock:
                self._last_operation_time = time.time()
            self._acquired.flag = True
        return acquired

    def release(self):
        """释放信号量（防止重复释放导致计数泄漏）"""
        if not getattr(self._acquired, 'flag', False):
            logger.warning("信号量释放跳过：acquire未成功或已释放")
            return
        try:
            self._semaphore.release()
            self._acquired.flag = False
        except ValueError:
            logger.warning("信号量释放失败：可能存在重复释放")

    def record_operation(self, operation_type: str, target: str,
                        success: bool, duration: float, error: Optional[str] = None):
        """
        记录操作结果

        Args:
            operation_type: 操作类型
            target: 操作目标
            success: 是否成功
            duration: 耗时
            error: 错误信息
        """
        record = OperationRecord(
            operation_type=operation_type,
            target=target,
            success=success,
            duration=duration,
            timestamp=time.time(),
            error=error
        )

        with self._lock:
            if operation_type not in self._operations:
                self._operations[operation_type] = []

            self._operations[operation_type].append(record)

            self._cleanup_expired(operation_type)

            if not success:
                self._consecutive_failures += 1
            else:
                self._consecutive_failures = 0

            self._check_protection_level()

    def _is_locked(self) -> bool:
        """纯查询：检查是否被锁定（无副作用，不触发状态恢复）"""
        if self._protection_level != ProtectionLevel.LOCKED:
            return False
        if time.time() > self._locked_until:
            return False
        return True

    def _check_and_try_recovery(self):
        """检查锁定是否过期并尝试恢复（显式调用，有副作用，调用方需持有_lock）"""
        if self._protection_level == ProtectionLevel.LOCKED and time.time() > self._locked_until:
            self._try_recovery_unlocked()
        elif self._protection_level == ProtectionLevel.WARNING and self._consecutive_failures == 0:
            self._protection_level = ProtectionLevel.NORMAL
            logger.info("恢复成功：WARNING级别观察期无失败，恢复到正常模式")

    def _get_remaining_lock_time(self) -> float:
        """获取剩余锁定时间"""
        if self._protection_level != ProtectionLevel.LOCKED:
            return 0
        return max(0, self._locked_until - time.time())

    def _is_rate_limited(self, operation_type: str) -> bool:
        """检查是否限流"""
        with self._lock:
            current_time = time.time()
            window_start = current_time - self.WINDOW_SIZE

            recent_ops = [
                op for op in self._operations.get(operation_type, [])
                if op.timestamp > window_start
            ]

            max_ops = {
                'send': self.MAX_SEND_PER_WINDOW,
                'click': self.MAX_CLICK_PER_WINDOW,
                'fetch': self.MAX_FETCH_PER_WINDOW
            }.get(operation_type, 100)

            return len(recent_ops) >= max_ops

    def _check_protection_level(self):
        """检查保护级别"""
        current_time = time.time()
        window_start = current_time - self.WINDOW_SIZE

        all_ops = []
        for ops in self._operations.values():
            all_ops.extend([op for op in ops if op.timestamp > window_start])

        if not all_ops:
            return

        failures = sum(1 for op in all_ops if not op.success)
        failure_rate = failures / len(all_ops)

        if self._consecutive_failures >= self.CONSECUTIVE_FAILURES_LOCK:
            if len(all_ops) < self.INITIAL_FAILURE_TOLERANCE:
                logger.warning(f"初始阶段连续{self._consecutive_failures}次失败，但样本不足({len(all_ops)}次)，仅降级到保护模式")
                self._protection_level = ProtectionLevel.PROTECTED
                self._consecutive_failures = 0
                return
            self._protection_level = ProtectionLevel.LOCKED
            self._locked_until = time.time() + self.LOCK_DURATION
            logger.error(f"触发熔断锁定，锁定 {self.LOCK_DURATION} 秒")
            return

        if failure_rate >= self.FAILURE_RATE_LOCK:
            self._protection_level = ProtectionLevel.LOCKED
            self._locked_until = time.time() + self.LOCK_DURATION
            logger.error(f"失败率 {failure_rate:.1%} 触发锁定")
        elif failure_rate >= self.FAILURE_RATE_PROTECT:
            self._protection_level = ProtectionLevel.PROTECTED
            logger.warning(f"失败率 {failure_rate:.1%} 进入保护模式")
        elif failure_rate >= self.FAILURE_RATE_WARNING:
            self._protection_level = ProtectionLevel.WARNING
            logger.info(f"失败率 {failure_rate:.1%} 进入警告模式")
        else:
            self._protection_level = ProtectionLevel.NORMAL

    def _try_recovery(self):
        """尝试恢复（加锁版本，供外部调用）"""
        with self._lock:
            self._try_recovery_unlocked()

    def _try_recovery_unlocked(self):
        """尝试恢复（不加锁版本，调用方需持有_lock）
        
        增强恢复策略：
        1. 渐进式恢复：从保护模式逐步恢复到正常
        2. 成功率验证：验证恢复后的操作成功率
        3. 自适应锁定时间：根据失败严重程度调整锁定时间
        """
        current_time = time.time()
        
        # 检查最近的操作成功率
        window_start = current_time - self.WINDOW_SIZE
        recent_ops = []
        for ops in self._operations.values():
            recent_ops.extend([op for op in ops if op.timestamp > window_start])
        
        if recent_ops:
            recent_successes = sum(1 for op in recent_ops if op.success)
            recent_success_rate = recent_successes / len(recent_ops)
            
            # 根据成功率决定恢复策略
            if recent_success_rate >= 0.8:
                # 高成功率，直接恢复到正常模式
                self._protection_level = ProtectionLevel.NORMAL
                logger.info(f"恢复成功：成功率{recent_success_rate:.1%}，恢复到正常模式")
            elif recent_success_rate >= 0.5:
                # 中等成功率，先恢复到保护模式
                self._protection_level = ProtectionLevel.PROTECTED
                logger.info(f"恢复中：成功率{recent_success_rate:.1%}，进入保护模式")
            else:
                # 低成功率，延长锁定时间
                extended_lock = min(self.LOCK_DURATION, 60)  # 最长5分钟
                self._locked_until = current_time + extended_lock
                logger.warning(f"恢复失败：成功率{recent_success_rate:.1%}，延长锁定{extended_lock}秒")
                return
        else:
            self._protection_level = ProtectionLevel.WARNING
            logger.info("恢复中：无近期操作记录，先恢复到WARNING级别观察")
        
        self._consecutive_failures = 0
        self._locked_until = 0

    def _recovery_loop(self):
        """恢复检查循环（使用Event.wait替代time.sleep，支持及时中断）"""
        while self._running:
            try:
                if self._stop_event.wait(timeout=self.AUTO_RECOVERY_CHECK):
                    break

                with self._lock:
                    self._check_and_try_recovery()

            except Exception as e:
                logger.error(f"恢复检查失败: {e}")

    def _cleanup_expired(self, operation_type: str):
        """清理过期记录"""
        current_time = time.time()
        window_start = current_time - self.WINDOW_SIZE * 2

        self._operations[operation_type] = [
            op for op in self._operations[operation_type]
            if op.timestamp > window_start
        ]

    def get_status(self) -> Dict[str, Any]:
        """获取保护状态"""
        with self._lock:
            current_time = time.time()
            window_start = current_time - self.WINDOW_SIZE

            stats = {}
            for op_type, ops in self._operations.items():
                recent = [op for op in ops if op.timestamp > window_start]
                failures = sum(1 for op in recent if not op.success)
                stats[op_type] = {
                    'total': len(recent),
                    'failures': failures,
                    'failure_rate': failures / len(recent) if recent else 0
                }

            return {
                'protection_level': self._protection_level.value,
                'consecutive_failures': self._consecutive_failures,
                'locked_until': self._locked_until,
                'remaining_lock_time': self._get_remaining_lock_time(),
                'operations': stats
            }

    def get_failure_rate(self, operation_type: str = "send") -> float:
        """获取特定操作的失败率"""
        with self._lock:
            current_time = time.time()
            window_start = current_time - self.WINDOW_SIZE

            recent_ops = [
                op for op in self._operations.get(operation_type, [])
                if op.timestamp > window_start
            ]

            if not recent_ops:
                return 0.0

            failures = sum(1 for op in recent_ops if not op.success)
            return failures / len(recent_ops)

    def reset(self):
        """重置保护器状态"""
        with self._lock:
            self._operations = {
                'send': [],
                'click': [],
                'fetch': []
            }
            self._protection_level = ProtectionLevel.NORMAL
            self._consecutive_failures = 0
            self._locked_until = 0
            logger.info("BoundaryGuard 已重置")

    def restore_persisted_state(self, state: Dict[str, Any]):
        """从持久化存储恢复边界保护器状态

        Args:
            state: 持久化状态字典，包含 protection_level, consecutive_failures, locked_until, operations
        """
        try:
            with self._lock:
                level_str = state.get('protection_level', 'normal')
                try:
                    self._protection_level = ProtectionLevel(level_str)
                except ValueError:
                    self._protection_level = ProtectionLevel.NORMAL

                self._consecutive_failures = state.get('consecutive_failures', 0)
                self._locked_until = state.get('locked_until', 0)

                if self._locked_until and self._locked_until < time.time():
                    self._protection_level = ProtectionLevel.NORMAL
                    self._locked_until = 0
                    self._consecutive_failures = 0
                    logger.info("恢复的锁定已过期，重置为正常状态")

                for op_type, records in state.get('operations', {}).items():
                    if op_type not in self._operations:
                        self._operations[op_type] = []
                    for record_data in records:
                        try:
                            self._operations[op_type].append(OperationRecord(
                                operation_type=record_data.get('operation_type', op_type),
                                target=record_data.get('target', 'unknown'),
                                success=record_data.get('success', False),
                                duration=record_data.get('duration', 0),
                                timestamp=record_data.get('timestamp', 0),
                                error=record_data.get('error'),
                            ))
                        except Exception:
                            continue

            logger.info(f"BoundaryGuard 状态已恢复: level={self._protection_level.value}, failures={self._consecutive_failures}")
        except Exception as e:
            logger.error(f"恢复BoundaryGuard状态失败: {e}")

    def stop(self):
        """停止保护器（等待恢复线程退出）"""
        self._running = False
        self._stop_event.set()
        if hasattr(self, '_recovery_thread') and self._recovery_thread.is_alive():
            self._recovery_thread.join(timeout=10)
