"""
企业级会话管理器

提供：
1. 会话持久化
2. 会话状态管理
3. 分布式会话支持
"""

import time
import json
import threading
import hashlib
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from loguru import logger


@dataclass
class Session:
    """会话数据模型"""
    session_id: str
    customer_id: str
    platform: str
    state: str = "active"
    context: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    message_count: int = 0
    metadata: Dict = field(default_factory=dict)


class SessionManager:
    """
    企业级会话管理器

    特性：
    1. 会话持久化 - 支持Redis/数据库
    2. 状态机管理 - 会话状态流转
    3. 超时管理 - 自动清理过期会话
    4. 并发安全 - 线程安全操作
    """

    DEFAULT_TTL = 1800
    EXTENDED_TTL = 7200
    MAX_CONTEXT_SIZE = 100

    def __init__(self, redis_client=None, db=None):
        self._redis = redis_client
        self._db = db
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.RLock()
        self._running = True
        self._stop_event = threading.Event()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

        self._stats = {
            "total_sessions": 0,
            "active_sessions": 0,
            "expired_sessions": 0,
            "total_messages": 0
        }

    def create_session(self, customer_id: str, platform: str, metadata: Dict = None) -> Session:
        """创建新会话（持久化操作移到锁外，避免锁内I/O阻塞其他线程）"""
        session_id = self._generate_session_id(customer_id, platform)
        session_to_persist = None
        with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                was_inactive = session.state != "active"
                session.updated_at = time.time()
                session.last_activity = time.time()
                session.state = "active"
                if was_inactive:
                    self._stats["active_sessions"] += 1
                session_to_persist = session
                return session

            session = Session(
                session_id=session_id,
                customer_id=customer_id,
                platform=platform,
                state="active",
                metadata=metadata or {}
            )
            self._sessions[session_id] = session
            self._stats["total_sessions"] += 1
            self._stats["active_sessions"] += 1
            session_to_persist = session

        if session_to_persist:
            try:
                self._persist_session(session_to_persist)
            except Exception as e:
                logger.debug(f"持久化会话失败: {e}")

        logger.info(f"创建会话: {session_id}")
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """获取会话（返回深拷贝，防止外部无锁修改内部状态）"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                import copy
                return copy.deepcopy(session)
            return None

    def get_session_by_customer(self, customer_id: str, platform: str) -> Optional[Session]:
        """根据客户ID和平台获取会话"""
        session_id = self._generate_session_id(customer_id, platform)
        return self.get_session(session_id)

    def update_session(self, session_id: str, context: Dict = None, state: str = None):
        """更新会话"""
        session_data = None
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return

            if context is not None:
                session.context.update(context)
                if len(session.context) > self.MAX_CONTEXT_SIZE:
                    oldest_keys = list(session.context.keys())[:-self.MAX_CONTEXT_SIZE]
                    for k in oldest_keys:
                        del session.context[k]

            if state:
                session.state = state

            session.updated_at = time.time()
            session.last_activity = time.time()
            session_data = {
                "session_id": session.session_id,
                "customer_id": session.customer_id,
                "platform": session.platform,
                "state": session.state,
                "context": session.context,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "last_activity": session.last_activity,
                "message_count": session.message_count,
                "metadata": session.metadata
            }

        if session_data:
            self._persist_session_data(session_data)

    def add_message(self, session_id: str, direction: str, content: str):
        """添加消息到会话"""
        session_data = None
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return

            if "messages" not in session.context:
                session.context["messages"] = []

            session.context["messages"].append({
                "direction": direction,
                "content": content,
                "timestamp": time.time()
            })

            if len(session.context["messages"]) > self.MAX_CONTEXT_SIZE:
                session.context["messages"] = session.context["messages"][-self.MAX_CONTEXT_SIZE:]

            session.message_count += 1
            session.last_activity = time.time()
            self._stats["total_messages"] += 1
            session_data = {
                "session_id": session.session_id,
                "customer_id": session.customer_id,
                "platform": session.platform,
                "state": session.state,
                "context": session.context,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "last_activity": session.last_activity,
                "message_count": session.message_count,
                "metadata": session.metadata
            }

        if session_data:
            self._persist_session_data(session_data)

    def get_context(self, session_id: str) -> List[Dict]:
        """获取会话上下文"""
        session = self.get_session(session_id)
        if not session:
            return []
        return session.context.get("messages", [])

    def end_session(self, session_id: str, reason: str = "completed"):
        """结束会话（防止同一会话重复结束导致active_sessions双重递减，验证reason参数）"""
        valid_reasons = ("completed", "expired", "closed", "error", "timeout")
        if reason not in valid_reasons:
            logger.warning(f"无效的会话结束原因: {reason}, 使用默认值'completed'")
            reason = "completed"
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return
            if session.state in ("completed", "expired", "closed"):
                logger.debug(f"会话已处于结束状态({session.state})，跳过: {session_id}")
                return
            session.state = reason
            session.updated_at = time.time()
            if self._stats["active_sessions"] > 0:
                self._stats["active_sessions"] -= 1
        logger.info(f"会话结束: {session_id}, 原因: {reason}")

    def _generate_session_id(self, customer_id: str, platform: str) -> str:
        """生成会话ID（确定性，基于customer_id和platform）"""
        raw = f"{platform}:{customer_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def _persist_session(self, session: Session):
        """持久化会话（从Session对象）"""
        session_data = {
            "session_id": session.session_id,
            "customer_id": session.customer_id,
            "platform": session.platform,
            "state": session.state,
            "context": session.context,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "last_activity": session.last_activity,
            "message_count": session.message_count,
            "metadata": session.metadata
        }
        self._persist_session_data(session_data)

    def _persist_session_data(self, session_data: Dict):
        """持久化会话数据（从字典，在锁外调用，避免锁内I/O）"""
        if self._redis:
            try:
                key = f"session:{session_data['session_id']}"
                data = json.dumps(session_data)
                self._redis.setex(key, self.DEFAULT_TTL, data)
            except Exception as e:
                logger.error(f"会话持久化失败: {e}")

    def _cleanup_loop(self):
        """清理过期会话"""
        while self._running:
            try:
                if self._stop_event.wait(300):
                    break
                self._cleanup_expired()
            except Exception as e:
                logger.error(f"清理会话失败: {e}")

    def _cleanup_expired(self):
        """清理过期会话（非active会话按DEFAULT_TTL清理，active会话按EXTENDED_TTL超长清理防止内存泄漏）"""
        current_time = time.time()
        expired = []
        force_expired = []
        with self._lock:
            for session_id, session in self._sessions.items():
                idle_time = current_time - session.last_activity
                if session.state == "active":
                    if idle_time > self.EXTENDED_TTL:
                        force_expired.append(session_id)
                    continue
                if idle_time > self.DEFAULT_TTL:
                    expired.append(session_id)
            for session_id in expired:
                del self._sessions[session_id]
                self._stats["expired_sessions"] += 1
            for session_id in force_expired:
                session = self._sessions.pop(session_id, None)
                if session:
                    self._stats["expired_sessions"] += 1
                    if self._stats["active_sessions"] > 0:
                        self._stats["active_sessions"] -= 1
                    logger.warning(f"活跃会话超长无活动({self.EXTENDED_TTL}s)，强制过期: {session_id}")
        if expired or force_expired:
            logger.info(f"清理 {len(expired)} 个过期会话, {len(force_expired)} 个超长活跃会话")

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            return {
                **self._stats,
                "total_in_memory": len(self._sessions)
            }

    def stop(self):
        """停止管理器"""
        self._running = False
        self._stop_event.set()

    def restore_persisted_state(self, state: Dict[str, Any]):
        """从持久化存储恢复会话状态

        Args:
            state: 持久化状态字典，包含 sessions 数据
        """
        try:
            sessions_data = state.get('sessions', {})
            with self._lock:
                for session_id, session_data in sessions_data.items():
                    if isinstance(session_data, dict):
                        try:
                            session = Session(
                                session_id=session_data.get('session_id', session_id),
                                customer_id=session_data.get('customer_id', ''),
                                platform=session_data.get('platform', 'douyin'),
                                state=session_data.get('state', 'active'),
                                context=session_data.get('context', {}),
                                created_at=session_data.get('created_at', time.time()),
                                updated_at=session_data.get('updated_at', time.time()),
                                last_activity=session_data.get('last_activity', time.time()),
                                message_count=session_data.get('message_count', 0),
                                metadata=session_data.get('metadata', {}),
                            )
                            self._sessions[session_id] = session
                        except Exception as sess_e:
                            logger.debug(f"恢复会话失败 [{session_id}]: {sess_e}")
                            continue

                restored_count = len(sessions_data)
                self._stats["active_sessions"] = sum(
                    1 for s in self._sessions.values() if s.state == "active"
                )

            logger.info(f"SessionManager 状态已恢复: {restored_count}个会话")
        except Exception as e:
            logger.error(f"恢复SessionManager状态失败: {e}")
