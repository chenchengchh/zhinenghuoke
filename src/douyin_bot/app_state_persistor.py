"""
应用状态持久化模块

负责在服务关闭时保存运行时状态，启动时恢复状态，
确保服务重启后消息去重、自回复防护、会话状态机等关键功能不中断。

持久化数据分区：
1. bot_service_state.json - BotService消息处理缓存
2. rpa_engine_state.json - RPA引擎会话状态机与消息去重
3. boundary_guard_state.json - 边界保护器状态
4. session_state.json - 会话管理器数据
5. message_monitor_state.json - 消息监控器会话状态
6. monitoring_state.json - 监听运行状态（是否在监听等）

设计原则：
1. 原子写入：tempfile + os.replace，防止写入中断导致数据损坏
2. 节流保存：最少间隔10秒，避免频繁I/O
3. 过期清理：加载时自动清理超时数据
4. 分区存储：不同模块状态分开存储
5. 向后兼容：缺失字段使用默认值
6. 延长过期：关键状态过期时间延长至3600秒，确保重启后可恢复
7. 启动恢复：提供启动恢复辅助方法，清理不一致状态
"""

import json
import time
import threading
import tempfile
import os
from pathlib import Path
from typing import Dict, Any, Optional, List
from loguru import logger
from src.config.settings import DATA_DIR


class AppStatePersistor:
    """
    应用状态持久化器

    设计原则：
    1. 原子写入：tempfile + os.replace，防止写入中断导致数据损坏
    2. 节流保存：最少间隔10秒，避免频繁I/O
    3. 过期清理：加载时自动清理超时数据
    4. 分区存储：不同模块状态分开存储
    5. 向后兼容：缺失字段使用默认值
    6. 延长过期：关键状态过期时间延长至3600秒，确保重启后可恢复
    7. 启动恢复：提供启动恢复辅助方法，清理不一致状态
    """

    SAVE_INTERVAL = 10
    STATE_DIR = DATA_DIR / "app_state"
    BOT_SERVICE_STATE_TTL = 3600
    RPA_ENGINE_STATE_TTL = 7200
    BOUNDARY_GUARD_STATE_TTL = 1800
    TRANSIENT_RPA_STATE_KEYS = {
        "dirty_unread_until",
        "dirty_unread_count",
        "dirty_unread_preview",
        "dirty_unread_hits",
    }
    SESSION_STATE_TTL = 3600
    MESSAGE_MONITOR_STATE_TTL = 3600
    MONITORING_STATE_TTL = 7200

    def __init__(self):
        """初始化持久化器"""
        self.STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._save_lock = threading.Lock()
        self._last_save_time: Dict[str, float] = {}
        self._save_pending: Dict[str, bool] = {}
        self._auto_save_thread: Optional[threading.Thread] = None
        self._auto_save_running = False

    def _get_state_path(self, name: str) -> Path:
        """获取状态文件路径"""
        return self.STATE_DIR / f"{name}.json"

    @classmethod
    def _sanitize_rpa_state_entry(cls, state: Dict[str, Any]) -> Dict[str, Any]:
        """去除不应跨重启继承的短期 RPA 状态字段。"""
        sanitized = dict(state or {})
        for key in cls.TRANSIENT_RPA_STATE_KEYS:
            sanitized.pop(key, None)
        return sanitized

    def _atomic_write(self, path: Path, data: dict):
        """原子写入JSON文件（增强版）
        
        增强原子性保证：
        1. 写入前验证数据完整性
        2. 使用fsync确保数据落盘
        3. 增加重试机制
        4. 备份旧文件防止数据丢失
        """
        max_retries = 3
        backup_path = None
        
        for attempt in range(max_retries):
            try:
                # 验证数据完整性
                if not isinstance(data, dict):
                    raise ValueError(f"持久化数据必须是字典类型，实际类型: {type(data)}")
                
                # 序列化验证
                json_str = json.dumps(data, ensure_ascii=False, indent=2)
                if len(json_str) > 10 * 1024 * 1024:  # 10MB限制
                    logger.warning(f"状态文件过大({len(json_str)}字节)，可能存在问题")
                
                dir_path = str(path.parent)
                
                # 备份旧文件（如果存在）
                if path.exists() and attempt == 0:
                    backup_path = path.with_suffix(f'.backup.{int(time.time())}')
                    try:
                        import shutil
                        shutil.copy2(path, backup_path)
                        logger.debug(f"已备份旧状态文件: {backup_path.name}")
                    except Exception as backup_e:
                        logger.warning(f"备份旧文件失败: {backup_e}")
                
                # 创建临时文件
                fd, tmp_path = tempfile.mkstemp(suffix='.tmp', dir=dir_path)
                try:
                    # 写入数据并强制刷新到磁盘
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        f.write(json_str)
                        f.flush()
                        os.fsync(f.fileno())  # 强制刷新到磁盘
                    
                    # 原子替换
                    os.replace(tmp_path, str(path))
                    
                    # 验证写入成功
                    if not path.exists():
                        raise IOError(f"文件替换后不存在: {path}")
                    
                    # 验证文件可读
                    with open(path, 'r', encoding='utf-8') as f:
                        loaded_data = json.load(f)
                    
                    # 基本数据验证
                    if not isinstance(loaded_data, dict):
                        raise ValueError(f"加载的数据不是字典类型: {type(loaded_data)}")
                    
                    logger.debug(f"状态文件原子写入成功 [{path.name}]: {len(json_str)}字节")
                    
                    # 清理备份文件（如果存在）
                    if backup_path and backup_path.exists():
                        try:
                            backup_path.unlink()
                        except Exception:
                            pass
                    
                    return  # 成功退出
                    
                except Exception as write_e:
                    # 清理临时文件
                    try:
                        if os.path.exists(tmp_path):
                            os.unlink(tmp_path)
                    except Exception:
                        pass
                    
                    if attempt < max_retries - 1:
                        logger.warning(f"原子写入失败，第{attempt+1}次重试 [{path.name}]: {write_e}")
                        time.sleep(0.1 * (attempt + 1))  # 指数退避
                        continue
                    else:
                        # 恢复备份文件（如果存在）
                        if backup_path and backup_path.exists():
                            try:
                                os.replace(backup_path, str(path))
                                logger.info(f"已从备份恢复状态文件: {backup_path.name}")
                            except Exception as restore_e:
                                logger.error(f"恢复备份文件失败: {restore_e}")
                        raise
                        
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"原子写入状态文件失败 [{path.name}]: {e}")
                    raise
                else:
                    continue
        
        # 最终清理备份文件
        if backup_path and backup_path.exists():
            try:
                backup_path.unlink()
            except Exception:
                pass

    def _load_json(self, path: Path) -> Optional[dict]:
        """加载JSON文件"""
        try:
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"加载状态文件失败 [{path.name}]: {e}")
        return None

    def save_bot_service_state(self, processed_cache: Dict, recent_sent_cache: Dict,
                                login_status: bool = False, use_rpa_mode: bool = True,
                                source_content_hash_map: Dict = None,
                                outbound_idempotency_cache: Dict = None,
                                force: bool = False):
        """保存BotService状态

        Args:
            processed_cache: 消息幂等性缓存 {key: (timestamp, status)}
            recent_sent_cache: 最近回复窗口缓存 {key: [{time, content}]}
            login_status: 登录状态缓存
            use_rpa_mode: 是否使用RPA模式
        """
        now = time.time()
        if not force and not self._check_throttle('bot_service', now):
            return
        if force:
            self._last_save_time['bot_service'] = now
            self._save_pending['bot_service'] = False

        def _should_persist_processed_key(key: Any) -> bool:
            normalized = str(key or "").strip()
            return normalized.startswith(("lmid:", "src:", "mid:", "conv:", "cust:"))

        cleaned_processed = {}
        for key, value in processed_cache.items():
            if not _should_persist_processed_key(key):
                continue
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                ts, status = value[0], value[1]
                if now - ts < 7200:
                    cleaned_processed[key] = [ts, status]

        cleaned_sent = {}
        for key, value in recent_sent_cache.items():
            if isinstance(value, dict):
                ts = value.get('time', 0)
                if now - ts < 3600:
                    cleaned_sent[key] = value
            elif isinstance(value, list):
                cleaned_list = []
                for item in value:
                    if isinstance(item, dict):
                        ts = item.get('time', 0)
                        if now - ts < 3600:
                            cleaned_list.append(item)
                if cleaned_list:
                    cleaned_sent[key] = cleaned_list[-20:]

        cleaned_outbound_idempotency = {}
        for key, value in (outbound_idempotency_cache or {}).items():
            if isinstance(value, dict):
                ts = value.get('time', 0)
                if now - ts < 3600:
                    cleaned_outbound_idempotency[key] = value

        state = {
            'processed_messages_cache': cleaned_processed,
            'recent_sent_cache': cleaned_sent,
            'outbound_idempotency_cache': cleaned_outbound_idempotency,
            'login_status_cache': login_status,
            'use_rpa_mode': use_rpa_mode,
            'source_content_hash_map': source_content_hash_map or {},
            'saved_at': now,
            'version': 3
        }

        with self._save_lock:
            self._atomic_write(self._get_state_path('bot_service_state'), state)
        logger.info(
            f"BotService状态已保存: {len(cleaned_processed)}条已处理消息, "
            f"{len(cleaned_sent)}条最近回复缓存, {len(cleaned_outbound_idempotency)}条出站幂等缓存"
        )

    def load_bot_service_state(self) -> Dict[str, Any]:
        """加载BotService状态

        Returns:
            包含所有BotService持久化状态的字典
        """
        data = self._load_json(self._get_state_path('bot_service_state'))
        if not data:
            return {}

        now = time.time()
        saved_at = data.get('saved_at', 0)
        age = now - saved_at

        if age > self.BOT_SERVICE_STATE_TTL:
            logger.info(f"BotService状态文件已过期({age:.0f}秒)，跳过加载")
            return {}

        def _should_restore_processed_key(key: Any) -> bool:
            normalized = str(key or "").strip()
            return normalized.startswith(("lmid:", "src:", "mid:", "conv:", "cust:"))

        processed_cache = {}
        for key, value in data.get('processed_messages_cache', {}).items():
            if not _should_restore_processed_key(key):
                continue
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                ts, status = value[0], value[1]
                adjusted_ts = ts + age
                if now - adjusted_ts < self.BOT_SERVICE_STATE_TTL:
                    processed_cache[key] = (adjusted_ts, status)

        recent_sent_cache = {}
        for key, value in data.get('recent_sent_cache', {}).items():
            if isinstance(value, dict):
                ts = value.get('time', 0)
                adjusted_ts = ts + age
                if now - adjusted_ts < self.BOT_SERVICE_STATE_TTL:
                    value['time'] = adjusted_ts
                    recent_sent_cache[key] = value
            elif isinstance(value, list):
                restored_list = []
                for item in value:
                    if isinstance(item, dict):
                        ts = item.get('time', 0)
                        adjusted_ts = ts + age
                        if now - adjusted_ts < self.BOT_SERVICE_STATE_TTL:
                            restored_item = dict(item)
                            restored_item['time'] = adjusted_ts
                            restored_list.append(restored_item)
                if restored_list:
                    recent_sent_cache[key] = restored_list[-20:]

        outbound_idempotency_cache = {}
        for key, value in data.get('outbound_idempotency_cache', {}).items():
            if not isinstance(value, dict):
                continue
            ts = value.get('time', 0)
            adjusted_ts = ts + age
            if now - adjusted_ts < self.BOT_SERVICE_STATE_TTL:
                restored_value = dict(value)
                restored_value['time'] = adjusted_ts
                outbound_idempotency_cache[key] = restored_value

        result = {
            'processed_messages_cache': processed_cache,
            'recent_sent_cache': recent_sent_cache,
            'outbound_idempotency_cache': outbound_idempotency_cache,
            'login_status_cache': data.get('login_status_cache', False),
            'use_rpa_mode': data.get('use_rpa_mode', True),
            'source_content_hash_map': data.get('source_content_hash_map', {}),
        }

        logger.info(
            f"BotService状态已加载: {len(processed_cache)}条已处理消息, "
            f"{len(recent_sent_cache)}条最近回复缓存, {len(outbound_idempotency_cache)}条出站幂等缓存"
        )
        return result

    def save_rpa_engine_state(self, states: Dict, sent_cache: Dict, processed_msg_ids: Dict,
                              force: bool = False):
        """保存RPA引擎状态

        Args:
            states: 会话状态机 {customer_name: {last_content, last_time, sent_by_us}}
            sent_cache: 已发送消息缓存 {hash: timestamp}
            processed_msg_ids: 已处理消息ID {msg_id: timestamp}
        """
        now = time.time()
        if not force and not self._check_throttle('rpa_engine', now):
            return
        if force:
            self._last_save_time['rpa_engine'] = now
            self._save_pending['rpa_engine'] = False

        cleaned_states = {}
        for name, state in states.items():
            if isinstance(state, dict):
                ts = state.get('last_time', 0)
                if now - ts < 1800:
                    cleaned_states[name] = self._sanitize_rpa_state_entry(state)

        cleaned_sent = {}
        for key, ts in sent_cache.items():
            if now - ts < 3600:
                cleaned_sent[key] = ts

        cleaned_msg_ids = {}
        for msg_id, ts in processed_msg_ids.items():
            if now - ts < 1800:
                cleaned_msg_ids[msg_id] = ts

        state = {
            'states': cleaned_states,
            'sent_cache': cleaned_sent,
            'processed_msg_ids': cleaned_msg_ids,
            'saved_at': now,
            'version': 2
        }

        with self._save_lock:
            self._atomic_write(self._get_state_path('rpa_engine_state'), state)
        logger.info(f"RPA引擎状态已保存: {len(cleaned_states)}个会话, {len(cleaned_msg_ids)}条已处理消息")

    def load_rpa_engine_state(self) -> Dict[str, Any]:
        """加载RPA引擎状态

        Returns:
            包含所有RPA引擎持久化状态的字典
        """
        data = self._load_json(self._get_state_path('rpa_engine_state'))
        if not data:
            return {}

        now = time.time()
        saved_at = data.get('saved_at', 0)
        age = now - saved_at

        if age > self.RPA_ENGINE_STATE_TTL:
            logger.info(f"RPA引擎状态文件已过期({age:.0f}秒)，跳过加载")
            return {}

        time_offset = age

        cleaned_states = {}
        for name, state in data.get('states', {}).items():
            if isinstance(state, dict):
                ts = state.get('last_time', 0)
                adjusted_ts = ts + time_offset
                if now - adjusted_ts < self.RPA_ENGINE_STATE_TTL:
                    sanitized_state = self._sanitize_rpa_state_entry(state)
                    sanitized_state['last_time'] = adjusted_ts
                    cleaned_states[name] = sanitized_state

        cleaned_sent = {}
        for key, ts in data.get('sent_cache', {}).items():
            adjusted_ts = ts + time_offset
            if now - adjusted_ts < self.RPA_ENGINE_STATE_TTL:
                cleaned_sent[key] = adjusted_ts

        cleaned_msg_ids = {}
        for msg_id, ts in data.get('processed_msg_ids', {}).items():
            adjusted_ts = ts + time_offset
            if now - adjusted_ts < 1800:
                cleaned_msg_ids[msg_id] = adjusted_ts

        result = {
            'states': cleaned_states,
            'sent_cache': cleaned_sent,
            'processed_msg_ids': cleaned_msg_ids,
        }

        logger.info(f"RPA引擎状态已加载: {len(cleaned_states)}个会话, {len(cleaned_msg_ids)}条已处理消息")
        return result

    def save_boundary_guard_state(self, protection_level: str, consecutive_failures: int,
                                   locked_until: float, operations: Dict[str, list]):
        """保存边界保护器状态

        Args:
            protection_level: 保护级别 (normal/warning/protected/locked)
            consecutive_failures: 连续失败次数
            locked_until: 锁定截止时间戳
            operations: 操作记录 {type: [records]}
        """
        now = time.time()

        cleaned_ops = {}
        for op_type, records in operations.items():
            cleaned = []
            for record in records:
                if isinstance(record, dict):
                    ts = record.get('timestamp', 0)
                    if now - ts < 120:
                        cleaned.append(record)
            if cleaned:
                cleaned_ops[op_type] = cleaned

        state = {
            'protection_level': protection_level,
            'consecutive_failures': consecutive_failures,
            'locked_until': locked_until,
            'operations': cleaned_ops,
            'saved_at': now,
            'version': 1
        }

        with self._save_lock:
            self._atomic_write(self._get_state_path('boundary_guard_state'), state)
        logger.info(f"边界保护器状态已保存: level={protection_level}, failures={consecutive_failures}")

    def load_boundary_guard_state(self) -> Dict[str, Any]:
        """加载边界保护器状态

        Returns:
            包含边界保护器持久化状态的字典
        """
        data = self._load_json(self._get_state_path('boundary_guard_state'))
        if not data:
            return {}

        now = time.time()
        saved_at = data.get('saved_at', 0)
        age = now - saved_at

        if age > self.BOUNDARY_GUARD_STATE_TTL:
            logger.info(f"边界保护器状态文件已过期({age:.0f}秒)，跳过加载")
            return {}

        locked_until = data.get('locked_until', 0)
        if locked_until and locked_until < now:
            data['protection_level'] = 'normal'
            data['locked_until'] = 0
            data['consecutive_failures'] = 0
            logger.info("边界保护器锁定已过期，重置为正常状态")

        time_offset = age
        cleaned_ops = {}
        for op_type, records in data.get('operations', {}).items():
            cleaned = []
            for record in records:
                if isinstance(record, dict):
                    ts = record.get('timestamp', 0)
                    adjusted_ts = ts + time_offset
                    if now - adjusted_ts < self.BOUNDARY_GUARD_STATE_TTL:
                        record['timestamp'] = adjusted_ts
                        cleaned.append(record)
            if cleaned:
                cleaned_ops[op_type] = cleaned

        result = {
            'protection_level': data.get('protection_level', 'normal'),
            'consecutive_failures': data.get('consecutive_failures', 0),
            'locked_until': data.get('locked_until', 0),
            'operations': cleaned_ops,
        }

        logger.info(f"边界保护器状态已加载: level={result['protection_level']}, failures={result['consecutive_failures']}")
        return result

    def save_session_state(self, sessions: Dict[str, Dict]):
        """保存会话管理器状态

        Args:
            sessions: 会话数据 {session_id: session_dict}
        """
        now = time.time()

        cleaned = {}
        for session_id, session_data in sessions.items():
            if isinstance(session_data, dict):
                last_activity = session_data.get('last_activity', 0)
                if now - last_activity < 1800:
                    cleaned[session_id] = session_data

        state = {
            'sessions': cleaned,
            'saved_at': now,
            'version': 1
        }

        with self._save_lock:
            self._atomic_write(self._get_state_path('session_state'), state)
        logger.info(f"会话状态已保存: {len(cleaned)}个活跃会话")

    def load_session_state(self) -> Dict[str, Any]:
        """加载会话管理器状态

        Returns:
            包含会话数据的字典
        """
        data = self._load_json(self._get_state_path('session_state'))
        if not data:
            return {}

        now = time.time()
        saved_at = data.get('saved_at', 0)
        age = now - saved_at

        if age > self.SESSION_STATE_TTL:
            logger.info(f"会话状态文件已过期({age:.0f}秒)，跳过加载")
            return {}

        time_offset = age
        cleaned = {}
        for session_id, session_data in data.get('sessions', {}).items():
            if isinstance(session_data, dict):
                last_activity = session_data.get('last_activity', 0)
                adjusted_activity = last_activity + time_offset
                if now - adjusted_activity < 1800:
                    cleaned_session = dict(session_data)
                    cleaned_session['last_activity'] = adjusted_activity
                    if 'created_at' in cleaned_session:
                        cleaned_session['created_at'] = cleaned_session['created_at'] + time_offset
                    if 'updated_at' in cleaned_session:
                        cleaned_session['updated_at'] = cleaned_session['updated_at'] + time_offset
                    cleaned[session_id] = cleaned_session

        logger.info(f"会话状态已加载: {len(cleaned)}个活跃会话")
        return {'sessions': cleaned}

    def save_message_monitor_state(self, conversation_states: Dict):
        """保存消息监控器状态

        Args:
            conversation_states: 会话状态 {customer_name: {content, time, is_outbound, sent_by_us}}
        """
        now = time.time()

        cleaned_states = {}
        for name, state in conversation_states.items():
            if isinstance(state, dict):
                ts = state.get('time', 0)
                if now - ts < 1800:
                    cleaned_states[name] = state

        state = {
            'conversation_states': cleaned_states,
            'saved_at': now,
            'version': 1
        }

        with self._save_lock:
            self._atomic_write(self._get_state_path('message_monitor_state'), state)
        logger.info(f"消息监控器状态已保存: {len(cleaned_states)}个会话")

    def load_message_monitor_state(self) -> Dict[str, Any]:
        """加载消息监控器状态

        Returns:
            包含消息监控器持久化状态的字典
        """
        data = self._load_json(self._get_state_path('message_monitor_state'))
        if not data:
            return {}

        now = time.time()
        saved_at = data.get('saved_at', 0)
        age = now - saved_at

        if age > self.MESSAGE_MONITOR_STATE_TTL:
            logger.info(f"消息监控器状态文件已过期({age:.0f}秒)，跳过加载")
            return {}

        time_offset = age

        cleaned_states = {}
        for name, state in data.get('conversation_states', {}).items():
            if isinstance(state, dict):
                ts = state.get('time', 0)
                adjusted_ts = ts + time_offset
                if now - adjusted_ts < 1800:
                    cleaned_state = dict(state)
                    cleaned_state['time'] = adjusted_ts
                    cleaned_states[name] = cleaned_state

        result = {
            'conversation_states': cleaned_states,
        }

        logger.info(f"消息监控器状态已加载: {len(cleaned_states)}个会话")
        return result

    def save_monitoring_state(self, is_monitoring: bool, use_rpa_mode: bool,
                               monitor_active: bool, last_monitor_check_time: float):
        """保存监听运行状态

        Args:
            is_monitoring: 是否在监听消息
            use_rpa_mode: 是否使用RPA模式
            monitor_active: 传统模式是否激活
            last_monitor_check_time: 最后一次监听检查时间
        """
        now = time.time()
        state = {
            'is_monitoring': is_monitoring,
            'use_rpa_mode': use_rpa_mode,
            'monitor_active': monitor_active,
            'last_monitor_check_time': last_monitor_check_time,
            'saved_at': now,
            'version': 1
        }

        with self._save_lock:
            self._atomic_write(self._get_state_path('monitoring_state'), state)
        logger.info(f"监听状态已保存: is_monitoring={is_monitoring}, use_rpa_mode={use_rpa_mode}")

    def load_monitoring_state(self) -> Dict[str, Any]:
        """加载监听运行状态

        Returns:
            包含监听状态的字典
        """
        data = self._load_json(self._get_state_path('monitoring_state'))
        if not data:
            return {}

        now = time.time()
        saved_at = data.get('saved_at', 0)
        age = now - saved_at

        if age > self.MONITORING_STATE_TTL:
            logger.info(f"监听状态文件已过期({age:.0f}秒)，跳过加载")
            return {}

        last_check = data.get('last_monitor_check_time', 0)
        if last_check > 0:
            last_check = last_check + age

        result = {
            'is_monitoring': data.get('is_monitoring', False),
            'use_rpa_mode': data.get('use_rpa_mode', True),
            'monitor_active': data.get('monitor_active', False),
            'last_monitor_check_time': last_check,
        }

        logger.info(f"监听状态已加载: is_monitoring={result['is_monitoring']}, age={age:.0f}s")
        return result

    def perform_startup_recovery(self, db_manager=None) -> Dict[str, Any]:
        """执行启动恢复操作

        清理上一次运行可能遗留的不一致状态：
        1. 清理数据库中孤立的预留记录（reservation消息）
        2. 重置processing状态的消息（可能因崩溃而未完成）
        3. 返回需要重试的失败消息列表

        Args:
            db_manager: 数据库管理器实例

        Returns:
            恢复操作结果字典
        """
        recovery_result = {
            'orphan_reservations_cleaned': 0,
            'stale_processing_reset': 0,
            'failed_messages_to_retry': [],
        }

        if db_manager is None:
            return recovery_result

        try:
            with db_manager._msg_lock:
                data = db_manager._load_messages_data_unlocked()
                messages = data.get('messages', [])
                now = time.time()
                modified = False

                reservation_indices = []
                for i, msg in enumerate(messages):
                    msg_type = msg.get('message_type', '')
                    msg_id = msg.get('message_id', '')
                    if msg_type == 'reservation' or (isinstance(msg_id, str) and msg_id.startswith('_reservation_')):
                        created_at = msg.get('created_at', '')
                        try:
                            from datetime import datetime as dt
                            if created_at:
                                msg_time = dt.fromisoformat(created_at)
                                age_seconds = (dt.now() - msg_time).total_seconds()
                                if age_seconds > 60:
                                    reservation_indices.append(i)
                                else:
                                    continue
                            else:
                                reservation_indices.append(i)
                        except Exception:
                            reservation_indices.append(i)

                for i in reversed(reservation_indices):
                    messages.pop(i)
                    recovery_result['orphan_reservations_cleaned'] += 1
                    modified = True

                if modified:
                    db_manager._persist_message_store_and_sidecar_unlocked(data)

            if recovery_result['orphan_reservations_cleaned'] > 0:
                logger.info(f"启动恢复: 清理了 {recovery_result['orphan_reservations_cleaned']} 条孤立预留记录")

        except Exception as e:
            logger.error(f"启动恢复操作失败: {e}")

        return recovery_result

    def save_all(self, bot_service=None, rpa_engine=None, boundary_guard=None, session_manager=None,
                 message_monitor=None):
        """保存所有模块状态

        Args:
            bot_service: BotService实例
            rpa_engine: DouYinRPAEngine实例
            boundary_guard: BoundaryGuard实例
            session_manager: SessionManager实例
            message_monitor: MessageMonitor实例
        """
        logger.info("=== 开始保存应用状态 ===")

        if bot_service is not None:
            try:
                self.save_bot_service_state(
                    processed_cache=bot_service._processed_messages_cache,
                    recent_sent_cache=bot_service._get_outbound_recent_reply_store().get_snapshot()
                    if hasattr(bot_service, "_get_outbound_recent_reply_store") else bot_service._recent_sent_cache,
                    login_status=bot_service._login_status_cache,
                    use_rpa_mode=bot_service._use_rpa_mode,
                    source_content_hash_map=getattr(
                        bot_service._get_inbound_idempotency_service(),
                        "_source_content_hash_map",
                        None,
                    ) if hasattr(bot_service, "_get_inbound_idempotency_service") else None,
                    outbound_idempotency_cache=bot_service._get_outbound_idempotency_service().get_cache_snapshot()
                    if hasattr(bot_service, "_get_outbound_idempotency_service") else {},
                    force=True,
                )
            except Exception as e:
                logger.error(f"保存BotService状态失败: {e}")

            try:
                self.save_monitoring_state(
                    is_monitoring=bot_service.is_monitoring_messages,
                    use_rpa_mode=bot_service._use_rpa_mode,
                    monitor_active=bot_service._monitor_active,
                    last_monitor_check_time=bot_service._last_monitor_check_time,
                )
            except Exception as e:
                logger.error(f"保存监听状态失败: {e}")

        if rpa_engine is not None:
            try:
                self.save_rpa_engine_state(
                    states=rpa_engine._states,
                    sent_cache=rpa_engine._sent_cache,
                    processed_msg_ids=rpa_engine._processed_msg_ids,
                    force=True,
                )
            except Exception as e:
                logger.error(f"保存RPA引擎状态失败: {e}")

        if boundary_guard is not None:
            try:
                ops_serializable = {}
                with boundary_guard._lock:
                    for op_type, records in boundary_guard._operations.items():
                        serialized = []
                        for r in records:
                            serialized.append({
                                'operation_type': r.operation_type,
                                'target': r.target,
                                'success': r.success,
                                'duration': r.duration,
                                'timestamp': r.timestamp,
                                'error': r.error,
                            })
                        ops_serializable[op_type] = serialized

                self.save_boundary_guard_state(
                    protection_level=boundary_guard._protection_level.value,
                    consecutive_failures=boundary_guard._consecutive_failures,
                    locked_until=boundary_guard._locked_until,
                    operations=ops_serializable,
                )
            except Exception as e:
                logger.error(f"保存边界保护器状态失败: {e}")

        if session_manager is not None:
            try:
                sessions_data = {}
                with session_manager._lock:
                    for session_id, session in session_manager._sessions.items():
                        sessions_data[session_id] = {
                            'session_id': session.session_id,
                            'customer_id': session.customer_id,
                            'platform': session.platform,
                            'state': session.state,
                            'context': session.context,
                            'created_at': session.created_at,
                            'updated_at': session.updated_at,
                            'last_activity': session.last_activity,
                            'message_count': session.message_count,
                            'metadata': session.metadata,
                        }
                self.save_session_state(sessions_data)
            except Exception as e:
                logger.error(f"保存会话状态失败: {e}")

        if message_monitor is not None:
            try:
                self.save_message_monitor_state(
                    conversation_states=message_monitor._conversation_states,
                )
            except Exception as e:
                logger.error(f"保存消息监控器状态失败: {e}")

        logger.info("=== 应用状态保存完成 ===")

    def _check_throttle(self, name: str, now: float) -> bool:
        """检查节流是否允许保存

        Args:
            name: 状态分区名称
            now: 当前时间戳

        Returns:
            是否允许保存
        """
        last = self._last_save_time.get(name, 0)
        if now - last < self.SAVE_INTERVAL:
            self._save_pending[name] = True
            return False
        if self._save_pending.get(name, False):
            self._save_pending[name] = False
        self._last_save_time[name] = now
        return True

    def start_auto_save(self, interval: int = 30, get_state_callback=None):
        """启动自动保存线程

        Args:
            interval: 自动保存间隔（秒）
            get_state_callback: 获取状态的回调函数，返回 (bot_service, rpa_engine, boundary_guard, session_manager)
        """
        if self._auto_save_running:
            return

        self._auto_save_running = True
        self._get_state_callback = get_state_callback

        def _auto_save_loop():
            while self._auto_save_running:
                try:
                    time.sleep(interval)
                    if not self._auto_save_running:
                        break
                    if self._get_state_callback:
                        bs, rpa, bg, sm = self._get_state_callback()
                        mm = getattr(bs, 'message_monitor', None) if bs else None
                        self.save_all(bot_service=bs, rpa_engine=rpa,
                                      boundary_guard=bg, session_manager=sm,
                                      message_monitor=mm)
                except Exception as e:
                    logger.error(f"自动保存失败: {e}")

        self._auto_save_thread = threading.Thread(target=_auto_save_loop, daemon=True, name="app_state_autosave")
        self._auto_save_thread.start()
        logger.info(f"应用状态自动保存已启动，间隔{interval}秒")

    def stop_auto_save(self):
        """停止自动保存线程"""
        self._auto_save_running = False
        if self._auto_save_thread and self._auto_save_thread.is_alive():
            self._auto_save_thread.join(timeout=5)
        logger.info("应用状态自动保存已停止")

    def get_persist_status(self) -> Dict[str, Any]:
        """获取持久化状态信息"""
        status = {
            'state_dir': str(self.STATE_DIR),
            'files': {},
            'auto_save_running': self._auto_save_running,
        }
        for name in ['bot_service_state', 'rpa_engine_state', 'boundary_guard_state',
                      'session_state', 'message_monitor_state', 'monitoring_state']:
            path = self._get_state_path(name)
            if path.exists():
                stat = path.stat()
                status['files'][name] = {
                    'exists': True,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                }
            else:
                status['files'][name] = {'exists': False}
        return status


_persistor_instance: Optional[AppStatePersistor] = None
_persistor_lock = threading.Lock()


def get_app_state_persistor() -> AppStatePersistor:
    """获取AppStatePersistor单例"""
    global _persistor_instance
    if _persistor_instance is None:
        with _persistor_lock:
            if _persistor_instance is None:
                _persistor_instance = AppStatePersistor()
    return _persistor_instance
