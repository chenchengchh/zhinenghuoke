"""
多平台数据库管理器

支持多平台的客户数据、消息记录存储
"""
import json
import os
import copy
import hashlib
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from loguru import logger


def _write_database_import_log(message: str) -> None:
    try:
        log_dir_value = os.getenv("HUOKE_LOG_DIR", "").strip()
        if log_dir_value:
            log_dir = Path(log_dir_value)
        else:
            persistent_root = os.getenv("HUOKE_PERSISTENT_ROOT", "").strip()
            if persistent_root:
                log_dir = Path(persistent_root) / "logs"
            else:
                local_app_data = os.getenv("LOCALAPPDATA", "").strip()
                log_dir = (Path(local_app_data) / "HuokeSmartBot" / "logs") if local_app_data else Path.cwd()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "database_import.log"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} | pid={os.getpid()} | {message}\n")
    except Exception:
        pass


_write_database_import_log("before import DB_PATH")
from src.config.settings import DB_PATH
_write_database_import_log("after import DB_PATH")
_write_database_import_log("before import score_to_intent_level")
from src.common.document_structuring_schema import ensure_document_structuring_tables
from src.common.intent_scoring_policy import score_to_intent_level
_write_database_import_log("after import score_to_intent_level")


class DatabaseManager:
    """数据库管理器 - 支持多平台"""
    CUSTOMER_JOURNAL_COMPACT_BYTES = 256 * 1024
    CUSTOMER_SQLITE_SYNC_BATCH = 500
    VIDEO_STATUS_PENDING = "pending"
    VIDEO_STATUS_IN_PROGRESS = "in_progress"
    VIDEO_STATUS_COMPLETED = "completed"
    VIDEO_STATUS_FAILED = "failed"
    VIDEO_STATUS_SKIPPED = "skipped"
    VIDEO_QUEUE_PRIORITY_LATEST = "latest_unprocessed"
    VIDEO_QUEUE_PRIORITY_PUBLISH = "publish_desc"
    VIDEO_QUEUE_PRIORITY_HOT = "hot_desc"
    VIDEO_QUEUE_PRIORITY_DISCOVERED = "discovered_desc"
    
    # 类级别的锁，防止并发写入
    _lock = threading.Lock()
    _msg_lock = threading.RLock()
    _instance_lock = threading.Lock()
    _instance = None
    
    _messages_cache = None
    _messages_cache_time = 0
    _messages_cache_ttl = 5
    _customer_cache = None
    _customer_cache_time = 0
    _customer_cache_ttl = 2
    _customer_sqlite_ready = False
    _customer_sqlite_ready_path = None
    _message_sqlite_ready = False
    _message_sqlite_ready_path = None
    MISCLASSIFIED_ASSISTANT_PATTERNS = (
        "期待为您服务",
        "有需要随时找我",
        "还有什么需要帮助",
        "对公转账需要",
        "远程协助服务",
        "请提供转账凭证序号",
        "方便的话留个接收资料的联系方式",
        "方便的话也可以留个接收资料的联系方式",
        "更完整的资料、详细说明",
        "继续为您处理",
        "我把说明发您",
        "我把完整说明发您",
    )

    def __new__(cls, db_path: str = None):
        """单例模式，确保全局只有一个DatabaseManager实例"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: str = None):
        with type(self)._instance_lock:
            if hasattr(self, '_initialized') and self._initialized:
                return
            self._initialized = True
            self.db_path = db_path or DB_PATH
            self._init_db()
    
    def _init_db(self):
        """初始化数据库"""
        if not os.path.exists(os.path.dirname(self.db_path)):
            os.makedirs(os.path.dirname(self.db_path))
        
        if not os.path.exists(self.db_path):
            self._save_data(self._default_customer_data())
            logger.info(f"初始化数据库: {self.db_path}")
        else:
            # 确保数据库结构包含send_modes
            data = self._load_data()
            if "send_modes" not in data:
                data["send_modes"] = {}
                self._save_data(data)
            logger.info(f"加载现有数据库: {self.db_path}")
        self._init_customer_sqlite()
        self._bootstrap_customer_sqlite()
        self._init_message_sqlite()
        self._bootstrap_message_sqlite()

    @staticmethod
    def _default_customer_data() -> dict:
        return {"customers": [], "platforms": {}, "send_modes": {}}

    def get_customer_journal_path(self) -> str:
        """获取客户增量日志路径。"""
        dir_path = os.path.dirname(self.db_path)
        return os.path.join(dir_path, "customers.journal.jsonl")

    def get_customer_sqlite_path(self) -> str:
        """获取客户 SQLite 索引库路径。"""
        dir_path = os.path.dirname(self.db_path)
        return os.path.join(dir_path, "customers.sqlite3")

    def get_message_sqlite_path(self) -> str:
        """获取聊天消息 SQLite 存储路径。"""
        dir_path = os.path.dirname(self.db_path)
        return os.path.join(dir_path, "messages.sqlite3")

    def _get_customer_sqlite_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.get_customer_sqlite_path(), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_message_sqlite_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.get_message_sqlite_path(), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _normalize_video_url(url: Any) -> str:
        text = str(url or "").strip()
        if not text:
            return ""
        return text.split("#", 1)[0].strip()

    @staticmethod
    def _extract_aweme_id_from_url(video_url: Any) -> str:
        text = str(video_url or "").strip()
        if not text:
            return ""
        match = re.search(r"/(?:video|note)/([0-9A-Za-z_-]+)", text)
        if match:
            return match.group(1)
        match = re.search(r"(?:modal_id|aweme_id)=([0-9A-Za-z_-]+)", text)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _extract_detail_kind_from_url(video_url: Any) -> str:
        text = str(video_url or "").strip().lower()
        if not text:
            return ""
        if "/note/" in text:
            return "note"
        if "/video/" in text:
            return "video"
        return ""

    @classmethod
    def _build_canonical_detail_url(cls, aweme_id: Any, detail_kind: str = "video") -> str:
        normalized_aweme_id = str(aweme_id or "").strip()
        if not normalized_aweme_id:
            return ""
        normalized_kind = "note" if str(detail_kind or "").strip().lower() == "note" else "video"
        return f"https://www.douyin.com/{normalized_kind}/{normalized_aweme_id}"

    @staticmethod
    def _iso_to_unix_seconds(value: Any) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0

    @staticmethod
    def _dump_json_text(value: Any, *, default: str) -> str:
        if value in (None, ""):
            return default
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return default

    @staticmethod
    def _load_json_text(value: Any, *, default: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return copy.deepcopy(default)
        try:
            return json.loads(text)
        except Exception:
            return copy.deepcopy(default)

    def _init_customer_sqlite(self):
        """初始化客户 SQLite 存储结构。"""
        sqlite_path = self.get_customer_sqlite_path()
        if DatabaseManager._customer_sqlite_ready and DatabaseManager._customer_sqlite_ready_path == sqlite_path:
            return

        conn = self._get_customer_sqlite_conn()
        try:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS customers (
                        sec_uid TEXT NOT NULL,
                        platform TEXT NOT NULL DEFAULT 'douyin',
                        nickname TEXT DEFAULT '',
                        unique_id TEXT DEFAULT '',
                        profile_url TEXT DEFAULT '',
                        source_video_url TEXT DEFAULT '',
                        first_source_video_url TEXT DEFAULT '',
                        comment_content TEXT DEFAULT '',
                        comment_time TEXT DEFAULT '',
                        status TEXT DEFAULT 'pending',
                        interact_status TEXT DEFAULT 'pending',
                        intent_level TEXT DEFAULT 'D',
                        intent_score REAL DEFAULT 0,
                        tags TEXT DEFAULT '[]',
                        created_at TEXT DEFAULT '',
                        updated_at TEXT DEFAULT '',
                        ip_location TEXT DEFAULT '',
                        signature TEXT DEFAULT '',
                        avatar_url TEXT DEFAULT '',
                        video_title TEXT DEFAULT '',
                        author_name TEXT DEFAULT '',
                        first_crawl_task_id TEXT DEFAULT '',
                        PRIMARY KEY (platform, sec_uid)
                    )
                """)
                customer_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(customers)").fetchall()
                }
                if "video_title" not in customer_columns:
                    conn.execute("ALTER TABLE customers ADD COLUMN video_title TEXT DEFAULT ''")
                if "author_name" not in customer_columns:
                    conn.execute("ALTER TABLE customers ADD COLUMN author_name TEXT DEFAULT ''")
                if "first_source_video_url" not in customer_columns:
                    conn.execute("ALTER TABLE customers ADD COLUMN first_source_video_url TEXT DEFAULT ''")
                if "first_crawl_task_id" not in customer_columns:
                    conn.execute("ALTER TABLE customers ADD COLUMN first_crawl_task_id TEXT DEFAULT ''")
                if "interact_status" not in customer_columns:
                    conn.execute("ALTER TABLE customers ADD COLUMN interact_status TEXT DEFAULT 'pending'")

                conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_interact_status ON customers(interact_status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_intent_level ON customers(intent_level)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_created_at ON customers(created_at DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_updated_at ON customers(updated_at DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_nickname ON customers(nickname)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_unique_id ON customers(unique_id)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS crawled_comments (
                        platform TEXT NOT NULL DEFAULT 'douyin',
                        aweme_id TEXT NOT NULL,
                        comment_id TEXT NOT NULL,
                        sec_uid TEXT DEFAULT '',
                        video_url TEXT DEFAULT '',
                        video_name TEXT DEFAULT '',
                        author_name TEXT DEFAULT '',
                        comment_content TEXT DEFAULT '',
                        comment_timestamp INTEGER DEFAULT 0,
                        comment_level INTEGER DEFAULT 1,
                        crawled_at TEXT DEFAULT '',
                        PRIMARY KEY (platform, aweme_id, comment_id)
                    )
                """)
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(crawled_comments)").fetchall()
                }
                if "comment_level" not in columns:
                    conn.execute(
                        "ALTER TABLE crawled_comments ADD COLUMN comment_level INTEGER DEFAULT 1"
                    )
                if "video_name" not in columns:
                    conn.execute(
                        "ALTER TABLE crawled_comments ADD COLUMN video_name TEXT DEFAULT ''"
                    )
                if "author_name" not in columns:
                    conn.execute(
                        "ALTER TABLE crawled_comments ADD COLUMN author_name TEXT DEFAULT ''"
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crawled_comments_aweme_ts "
                    "ON crawled_comments(platform, aweme_id, comment_timestamp DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crawled_comments_aweme_level "
                    "ON crawled_comments(platform, aweme_id, comment_level)"
                )
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS comment_facts (
                        platform TEXT NOT NULL DEFAULT 'douyin',
                        aweme_id TEXT NOT NULL,
                        comment_id TEXT NOT NULL,
                        parent_comment_id TEXT DEFAULT '',
                        root_comment_id TEXT DEFAULT '',
                        comment_level INTEGER DEFAULT 1,
                        sec_uid TEXT DEFAULT '',
                        nickname TEXT DEFAULT '',
                        unique_id TEXT DEFAULT '',
                        comment_content TEXT DEFAULT '',
                        matched_keyword TEXT DEFAULT '',
                        comment_timestamp INTEGER DEFAULT 0,
                        digg_count INTEGER DEFAULT 0,
                        ip_location TEXT DEFAULT '',
                        video_url TEXT DEFAULT '',
                        video_title TEXT DEFAULT '',
                        author_name TEXT DEFAULT '',
                        capture_session_id TEXT DEFAULT '',
                        captured_at TEXT DEFAULT '',
                        raw_json TEXT DEFAULT '{}',
                        PRIMARY KEY (platform, aweme_id, comment_id)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_comment_facts_aweme_ts "
                    "ON comment_facts(platform, aweme_id, comment_timestamp DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_comment_facts_session "
                    "ON comment_facts(capture_session_id, comment_timestamp DESC)"
                )
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS comment_crawl_sessions (
                        session_id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL DEFAULT 'douyin',
                        aweme_id TEXT DEFAULT '',
                        video_url TEXT DEFAULT '',
                        search_task_id TEXT DEFAULT '',
                        crawl_mode TEXT DEFAULT 'incremental',
                        started_at TEXT DEFAULT '',
                        finished_at TEXT DEFAULT '',
                        comment_time_start TEXT DEFAULT '',
                        comment_time_end TEXT DEFAULT '',
                        reply_collection_enabled INTEGER DEFAULT 0,
                        completion_status TEXT DEFAULT '',
                        termination_reason TEXT DEFAULT '',
                        completeness_warning TEXT DEFAULT '',
                        risk_control_detected INTEGER DEFAULT 0,
                        risk_control_reason TEXT DEFAULT '',
                        history_cutoff_timestamp INTEGER DEFAULT 0,
                        expected_comment_count INTEGER DEFAULT 0,
                        top_level_comment_count INTEGER DEFAULT 0,
                        reply_comment_count INTEGER DEFAULT 0,
                        total_comment_count INTEGER DEFAULT 0,
                        matched_comment_count INTEGER DEFAULT 0,
                        saved_customer_count INTEGER DEFAULT 0,
                        duplicate_comment_count INTEGER DEFAULT 0,
                        time_filtered_count INTEGER DEFAULT 0,
                        keyword_filtered_count INTEGER DEFAULT 0,
                        history_recorded_count INTEGER DEFAULT 0,
                        fact_recorded_count INTEGER DEFAULT 0,
                        customer_linked_count INTEGER DEFAULT 0,
                        api_request_count INTEGER DEFAULT 0,
                        reply_api_request_count INTEGER DEFAULT 0,
                        reply_expand_clicks INTEGER DEFAULT 0,
                        dropped_batches_count INTEGER DEFAULT 0,
                        peak_pending_batches INTEGER DEFAULT 0,
                        latest_batch_id INTEGER DEFAULT 0
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_comment_crawl_sessions_aweme_started "
                    "ON comment_crawl_sessions(platform, aweme_id, started_at DESC)"
                )
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS customer_comment_links (
                        platform TEXT NOT NULL DEFAULT 'douyin',
                        sec_uid TEXT NOT NULL,
                        aweme_id TEXT NOT NULL,
                        comment_id TEXT NOT NULL,
                        matched_keyword TEXT DEFAULT '',
                        search_task_id TEXT DEFAULT '',
                        linked_at TEXT DEFAULT '',
                        PRIMARY KEY (platform, sec_uid, aweme_id, comment_id)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customer_comment_links_sec_uid "
                    "ON customer_comment_links(platform, sec_uid, linked_at DESC)"
                )
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS crawled_videos (
                        platform TEXT NOT NULL DEFAULT 'douyin',
                        aweme_id TEXT NOT NULL,
                        video_url TEXT NOT NULL DEFAULT '',
                        normalized_video_url TEXT DEFAULT '',
                        title TEXT DEFAULT '',
                        author_name TEXT DEFAULT '',
                        author_sec_uid TEXT DEFAULT '',
                        author_unique_id TEXT DEFAULT '',
                        publish_timestamp INTEGER DEFAULT 0,
                        like_count INTEGER DEFAULT 0,
                        comment_count INTEGER DEFAULT 0,
                        share_count INTEGER DEFAULT 0,
                        play_count INTEGER DEFAULT 0,
                        search_keyword TEXT DEFAULT '',
                        first_discovered_at TEXT DEFAULT '',
                        last_discovered_at TEXT DEFAULT '',
                        comment_crawl_status TEXT DEFAULT 'pending',
                        comment_crawl_started_at TEXT DEFAULT '',
                        comment_crawl_finished_at TEXT DEFAULT '',
                        last_comment_crawl_at TEXT DEFAULT '',
                        comment_crawl_attempts INTEGER DEFAULT 0,
                        comment_crawl_error TEXT DEFAULT '',
                        last_crawl_matched_comments INTEGER DEFAULT 0,
                        last_crawl_saved_customers INTEGER DEFAULT 0,
                        last_crawl_total_comments INTEGER DEFAULT 0,
                        last_crawl_duplicate_comments INTEGER DEFAULT 0,
                        last_history_cutoff_timestamp INTEGER DEFAULT 0,
                        crawl_priority_score REAL DEFAULT 0,
                        PRIMARY KEY (platform, aweme_id)
                    )
                """)
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_crawled_videos_url_unique "
                    "ON crawled_videos(platform, normalized_video_url) "
                    "WHERE normalized_video_url != ''"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crawled_videos_status "
                    "ON crawled_videos(platform, comment_crawl_status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crawled_videos_keyword "
                    "ON crawled_videos(platform, search_keyword, comment_crawl_status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crawled_videos_publish "
                    "ON crawled_videos(platform, publish_timestamp DESC, comment_count DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crawled_videos_discovered "
                    "ON crawled_videos(platform, last_discovered_at DESC)"
                )
                ensure_document_structuring_tables(conn)
            DatabaseManager._customer_sqlite_ready = True
            DatabaseManager._customer_sqlite_ready_path = sqlite_path
        finally:
            conn.close()

    def _bootstrap_customer_sqlite(self):
        """启动时把现有 JSON/journal 数据同步到 SQLite，逐步完成迁移。"""
        try:
            data = self._load_data()
            customers = data.get("customers", [])
            if customers:
                self._upsert_customers_sqlite(customers)
        except Exception as e:
            logger.warning(f"启动时同步客户 SQLite 失败，将继续使用 JSON 兜底: {e}")

    def _init_message_sqlite(self):
        """初始化聊天消息 SQLite 骨架表，为后续平滑迁移做准备。"""
        sqlite_path = self.get_message_sqlite_path()
        if DatabaseManager._message_sqlite_ready and DatabaseManager._message_sqlite_ready_path == sqlite_path:
            return

        conn = self._get_message_sqlite_conn()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id TEXT UNIQUE DEFAULT '',
                        logical_message_id TEXT DEFAULT '',
                        source_message_id TEXT DEFAULT '',
                        conversation_id TEXT NOT NULL,
                        customer_id TEXT DEFAULT '',
                        platform TEXT NOT NULL DEFAULT 'douyin',
                        direction TEXT NOT NULL DEFAULT 'inbound',
                        message_type TEXT NOT NULL DEFAULT 'text',
                        content TEXT NOT NULL DEFAULT '',
                        sender_id TEXT DEFAULT '',
                        sender_name TEXT DEFAULT '',
                        is_read INTEGER NOT NULL DEFAULT 0,
                        is_processed INTEGER NOT NULL DEFAULT 0,
                        ai_reply_content TEXT DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversations (
                        conversation_id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL DEFAULT 'douyin',
                        customer_id TEXT DEFAULT '',
                        customer_name TEXT DEFAULT '',
                        customer_avatar TEXT DEFAULT '',
                        status TEXT DEFAULT 'active',
                        last_message_time TEXT DEFAULT '',
                        last_message_content TEXT DEFAULT '',
                        unread_count INTEGER DEFAULT 0,
                        intent_level TEXT DEFAULT '',
                        purchase_intent_score REAL DEFAULT 0,
                        lead_score TEXT DEFAULT 'cold',
                        follow_up_priority TEXT DEFAULT 'low',
                        purchase_probability REAL DEFAULT 0,
                        estimated_deal_size REAL DEFAULT 0,
                        lifecycle_stage TEXT DEFAULT 'prospect',
                        buying_role TEXT DEFAULT 'unknown',
                        signals_detected TEXT DEFAULT '[]',
                        risk_factors TEXT DEFAULT '[]',
                        opportunity_factors TEXT DEFAULT '[]',
                        intent_history TEXT DEFAULT '[]',
                        intent_trend TEXT DEFAULT '{}',
                        last_analysis_time TEXT DEFAULT '',
                        next_follow_up_time TEXT DEFAULT '',
                        last_completed_follow_up_signature TEXT DEFAULT '',
                        last_completed_follow_up_at TEXT DEFAULT '',
                        last_dingtalk_notification_signature TEXT DEFAULT '',
                        last_dingtalk_notification_at TEXT DEFAULT '',
                        last_dingtalk_notification_status TEXT DEFAULT '',
                        last_dingtalk_notification_error TEXT DEFAULT '',
                        last_dingtalk_notification_task_id TEXT DEFAULT '',
                        created_at TEXT DEFAULT '',
                        updated_at TEXT DEFAULT ''
                    )
                    """
                )
                try:
                    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id) WHERE message_id != ''")
                except Exception:
                    pass
                conversation_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(conversations)").fetchall()
                }
                _MIGRATION_CONVERSATION_COLUMNS = (
                    ("intent_level", "TEXT DEFAULT ''"),
                    ("purchase_intent_score", "REAL DEFAULT 0"),
                    ("lead_score", "TEXT DEFAULT 'cold'"),
                    ("follow_up_priority", "TEXT DEFAULT 'low'"),
                    ("purchase_probability", "REAL DEFAULT 0"),
                    ("estimated_deal_size", "REAL DEFAULT 0"),
                    ("lifecycle_stage", "TEXT DEFAULT 'prospect'"),
                    ("buying_role", "TEXT DEFAULT 'unknown'"),
                    ("signals_detected", "TEXT DEFAULT '[]'"),
                    ("risk_factors", "TEXT DEFAULT '[]'"),
                    ("opportunity_factors", "TEXT DEFAULT '[]'"),
                    ("intent_history", "TEXT DEFAULT '[]'"),
                    ("intent_trend", "TEXT DEFAULT '{}'"),
                    ("last_analysis_time", "TEXT DEFAULT ''"),
                    ("next_follow_up_time", "TEXT DEFAULT ''"),
                    ("last_completed_follow_up_signature", "TEXT DEFAULT ''"),
                    ("last_completed_follow_up_at", "TEXT DEFAULT ''"),
                    ("last_dingtalk_notification_signature", "TEXT DEFAULT ''"),
                    ("last_dingtalk_notification_at", "TEXT DEFAULT ''"),
                    ("last_dingtalk_notification_status", "TEXT DEFAULT ''"),
                    ("last_dingtalk_notification_error", "TEXT DEFAULT ''"),
                    ("last_dingtalk_notification_task_id", "TEXT DEFAULT ''"),
                )
                for name, ddl in _MIGRATION_CONVERSATION_COLUMNS:
                    if name not in conversation_columns:
                        conn.execute(f"ALTER TABLE conversations ADD COLUMN {name} {ddl}")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_conversation_time "
                    "ON messages(conversation_id, created_at)"
                )
                message_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(messages)").fetchall()
                }
                for name, ddl in (
                    ("customer_name", "TEXT DEFAULT ''"),
                    ("processed_status", "TEXT DEFAULT ''"),
                    ("processed_at", "TEXT DEFAULT ''"),
                    ("processed_reason", "TEXT DEFAULT ''"),
                ):
                    if name not in message_columns:
                        conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_message_id_unique "
                    "ON messages(message_id) WHERE message_id != ''"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_logical_message_id_unique "
                    "ON messages(logical_message_id) WHERE logical_message_id != ''"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_customer_platform "
                    "ON messages(customer_id, platform)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversations_customer_id "
                    "ON conversations(customer_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversations_customer_name "
                    "ON conversations(customer_name)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversations_updated_at "
                    "ON conversations(updated_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_direction "
                    "ON messages(direction)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_created_at "
                    "ON messages(created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversations_status "
                    "ON conversations(status)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS processed_events (
                        logical_message_id TEXT PRIMARY KEY,
                        conversation_id TEXT DEFAULT '',
                        customer_name TEXT DEFAULT '',
                        content_hash TEXT DEFAULT '',
                        source_message_id TEXT DEFAULT '',
                        platform TEXT DEFAULT 'douyin',
                        status TEXT DEFAULT '',
                        created_at TEXT DEFAULT '',
                        updated_at TEXT DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS outbox_events (
                        outbox_id TEXT PRIMARY KEY,
                        logical_message_id TEXT DEFAULT '',
                        conversation_id TEXT DEFAULT '',
                        customer_name TEXT DEFAULT '',
                        platform TEXT DEFAULT 'douyin',
                        reply_content TEXT DEFAULT '',
                        reply_hash TEXT DEFAULT '',
                        source_message_id TEXT DEFAULT '',
                        status TEXT DEFAULT '',
                        retry_count INTEGER DEFAULT 0,
                        next_retry_at TEXT DEFAULT '',
                        message_id TEXT DEFAULT '',
                        reason TEXT DEFAULT '',
                        workflow_run_id TEXT DEFAULT '',
                        created_at TEXT DEFAULT '',
                        updated_at TEXT DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS workflow_runs (
                        workflow_run_id TEXT PRIMARY KEY,
                        conversation_id TEXT DEFAULT '',
                        customer_name TEXT DEFAULT '',
                        customer_id TEXT DEFAULT '',
                        status TEXT DEFAULT '',
                        nodes TEXT DEFAULT '{}',
                        history TEXT DEFAULT '[]',
                        created_at TEXT DEFAULT '',
                        updated_at TEXT DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS handoff_tickets (
                        ticket_id TEXT PRIMARY KEY,
                        workflow_run_id TEXT DEFAULT '',
                        logical_message_id TEXT DEFAULT '',
                        conversation_id TEXT DEFAULT '',
                        customer_id TEXT DEFAULT '',
                        customer_name TEXT DEFAULT '',
                        platform TEXT DEFAULT 'douyin',
                        reason_code TEXT DEFAULT '',
                        handoff_level TEXT DEFAULT '',
                        status TEXT DEFAULT '',
                        owner TEXT DEFAULT '',
                        source TEXT DEFAULT '',
                        suggested_reply TEXT DEFAULT '',
                        raw_customer_message TEXT DEFAULT '',
                        eligibility_snapshot TEXT DEFAULT '{}',
                        reply_analysis_snapshot TEXT DEFAULT '{}',
                        schema_id TEXT DEFAULT '',
                        enterprise_id TEXT DEFAULT '',
                        claimed_at TEXT DEFAULT '',
                        resolved_at TEXT DEFAULT '',
                        escalate_at TEXT DEFAULT '',
                        resolution_note TEXT DEFAULT '',
                        approved_reply TEXT DEFAULT '',
                        created_at TEXT DEFAULT '',
                        updated_at TEXT DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_processed_events_status "
                    "ON processed_events(status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_processed_events_conversation "
                    "ON processed_events(conversation_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_outbox_events_status "
                    "ON outbox_events(status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_outbox_events_conversation "
                    "ON outbox_events(conversation_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_status "
                    "ON workflow_runs(status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_conversation "
                    "ON workflow_runs(conversation_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_handoff_tickets_status "
                    "ON handoff_tickets(status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_handoff_tickets_workflow "
                    "ON handoff_tickets(workflow_run_id)"
                )
            DatabaseManager._message_sqlite_ready = True
            DatabaseManager._message_sqlite_ready_path = sqlite_path
        finally:
            conn.close()

    def bootstrap_message_sqlite(self, *, force_rebuild: bool = False) -> dict:
        """公开的聊天消息 SQLite 引导入口，供迁移脚本和后续双写校验复用。"""
        return self._bootstrap_message_sqlite(force_rebuild=force_rebuild)

    def _bootstrap_message_sqlite(self, *, force_rebuild: bool = False) -> dict:
        """启动时把现有 messages.json 同步到 SQLite。当前阶段只做旁路构建，不切读写主链。"""
        stats = {"messages": 0, "conversations": 0}
        try:
            data = self._get_json_store_snapshot()
        except Exception as e:
            logger.warning(f"启动时同步聊天 SQLite 失败，将继续使用 JSON 兜底: {e}")
            return stats

        messages = list(self._get_business_messages(data))
        conversations = list(data.get("conversations", []))
        if not messages and not conversations:
            return stats

        conn = self._get_message_sqlite_conn()
        try:
            with conn:
                if force_rebuild:
                    conn.execute("DELETE FROM messages")
                    conn.execute("DELETE FROM conversations")

                for message in messages:
                    normalized = self._normalize_message_direction_fields(message)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO messages (
                            message_id, logical_message_id, source_message_id, conversation_id,
                            customer_id, platform, direction, message_type, content,
                            sender_id, sender_name, is_read, is_processed, ai_reply_content, created_at,
                            customer_name, processed_status, processed_at, processed_reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(normalized.get("message_id", "") or ""),
                            str(normalized.get("logical_message_id", "") or ""),
                            str(normalized.get("source_message_id", "") or ""),
                            str(normalized.get("conversation_id", "") or ""),
                            str(normalized.get("customer_id", "") or ""),
                            str(normalized.get("platform", "douyin") or "douyin"),
                            str(normalized.get("direction", "inbound") or "inbound"),
                            str(normalized.get("message_type", "text") or "text"),
                            str(normalized.get("content", "") or ""),
                            str(normalized.get("sender_id", "") or ""),
                            str(normalized.get("sender_name", "") or ""),
                            1 if bool(normalized.get("is_read", False)) else 0,
                            1 if bool(normalized.get("is_processed", False)) else 0,
                            str(normalized.get("ai_reply_content", "") or ""),
                            str(normalized.get("created_at", "") or ""),
                            str(normalized.get("customer_name", normalized.get("sender_name", "")) or ""),
                            str(normalized.get("processed_status", "") or ""),
                            str(normalized.get("processed_at", "") or ""),
                            str(normalized.get("processed_reason", "") or ""),
                        ),
                    )
                stats["messages"] = len(messages)

                for conversation in conversations:
                    conn.execute(
                        """
                        INSERT INTO conversations (
                            conversation_id, platform, customer_id, customer_name, customer_avatar,
                            status, last_message_time, last_message_content, unread_count,
                            intent_level, purchase_intent_score, lead_score, follow_up_priority,
                            purchase_probability, estimated_deal_size, lifecycle_stage, buying_role,
                            signals_detected, risk_factors, opportunity_factors,
                            intent_history, intent_trend,
                            last_analysis_time, next_follow_up_time,
                            last_completed_follow_up_signature, last_completed_follow_up_at,
                            last_dingtalk_notification_signature, last_dingtalk_notification_at,
                            last_dingtalk_notification_status, last_dingtalk_notification_error,
                            last_dingtalk_notification_task_id,
                            created_at, updated_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?
                        )
                        ON CONFLICT(conversation_id) DO UPDATE SET
                            platform=excluded.platform,
                            customer_id=excluded.customer_id,
                            customer_name=excluded.customer_name,
                            customer_avatar=excluded.customer_avatar,
                            status=excluded.status,
                            last_message_time=excluded.last_message_time,
                            last_message_content=excluded.last_message_content,
                            unread_count=excluded.unread_count,
                            intent_level=excluded.intent_level,
                            purchase_intent_score=excluded.purchase_intent_score,
                            lead_score=excluded.lead_score,
                            follow_up_priority=excluded.follow_up_priority,
                            purchase_probability=excluded.purchase_probability,
                            estimated_deal_size=excluded.estimated_deal_size,
                            lifecycle_stage=excluded.lifecycle_stage,
                            buying_role=excluded.buying_role,
                            signals_detected=excluded.signals_detected,
                            risk_factors=excluded.risk_factors,
                            opportunity_factors=excluded.opportunity_factors,
                            intent_history=excluded.intent_history,
                            intent_trend=excluded.intent_trend,
                            last_analysis_time=excluded.last_analysis_time,
                            next_follow_up_time=excluded.next_follow_up_time,
                            last_completed_follow_up_signature=excluded.last_completed_follow_up_signature,
                            last_completed_follow_up_at=excluded.last_completed_follow_up_at,
                            last_dingtalk_notification_signature=excluded.last_dingtalk_notification_signature,
                            last_dingtalk_notification_at=excluded.last_dingtalk_notification_at,
                            last_dingtalk_notification_status=excluded.last_dingtalk_notification_status,
                            last_dingtalk_notification_error=excluded.last_dingtalk_notification_error,
                            last_dingtalk_notification_task_id=excluded.last_dingtalk_notification_task_id,
                            created_at=CASE
                                WHEN conversations.created_at = '' THEN excluded.created_at
                                ELSE conversations.created_at
                            END,
                            updated_at=excluded.updated_at
                        """,
                        (
                            str(conversation.get("conversation_id", "") or ""),
                            str(conversation.get("platform", "douyin") or "douyin"),
                            str(conversation.get("customer_id", "") or ""),
                            str(conversation.get("customer_name", "") or ""),
                            str(conversation.get("customer_avatar", "") or ""),
                            str(conversation.get("status", "active") or "active"),
                            str(conversation.get("last_message_time", "") or ""),
                            str(conversation.get("last_message_content", "") or ""),
                            int(conversation.get("unread_count", 0) or 0),
                            str(conversation.get("intent_level", "") or ""),
                            float(conversation.get("purchase_intent_score", 0) or 0),
                            str(conversation.get("lead_score", "cold") or "cold"),
                            str(conversation.get("follow_up_priority", "low") or "low"),
                            float(conversation.get("purchase_probability", 0) or 0),
                            float(conversation.get("estimated_deal_size", 0) or 0),
                            str(conversation.get("lifecycle_stage", "prospect") or "prospect"),
                            str(conversation.get("buying_role", "unknown") or "unknown"),
                            self._dump_json_text(conversation.get("signals_detected", []), default="[]"),
                            self._dump_json_text(conversation.get("risk_factors", []), default="[]"),
                            self._dump_json_text(conversation.get("opportunity_factors", []), default="[]"),
                            self._dump_json_text(conversation.get("intent_history", []), default="[]"),
                            self._dump_json_text(conversation.get("intent_trend", {}), default="{}"),
                            str(conversation.get("last_analysis_time", "") or ""),
                            str(conversation.get("next_follow_up_time", "") or ""),
                            str(conversation.get("last_completed_follow_up_signature", "") or ""),
                            str(conversation.get("last_completed_follow_up_at", "") or ""),
                            str(conversation.get("last_dingtalk_notification_signature", "") or ""),
                            str(conversation.get("last_dingtalk_notification_at", "") or ""),
                            str(conversation.get("last_dingtalk_notification_status", "") or ""),
                            str(conversation.get("last_dingtalk_notification_error", "") or ""),
                            str(conversation.get("last_dingtalk_notification_task_id", "") or ""),
                            str(conversation.get("created_at", "") or ""),
                            str(conversation.get("updated_at", "") or ""),
                        ),
                    )
                stats["conversations"] = len(conversations)
        except Exception as e:
            logger.warning(f"同步聊天 SQLite 失败，将继续使用 JSON 主链: {e}")
        finally:
            conn.close()
        return stats

    def _sync_message_to_sqlite(self, message: dict) -> None:
        normalized = self._normalize_message_direction_fields(message or {})
        conn = self._get_message_sqlite_conn()
        try:
            with conn:
                message_id = str(normalized.get("message_id", "") or "")
                logical_message_id = str(normalized.get("logical_message_id", "") or "")
                source_message_id = str(normalized.get("source_message_id", "") or "")
                conversation_id = str(normalized.get("conversation_id", "") or "")
                created_at = str(normalized.get("created_at", "") or "")
                content = str(normalized.get("content", "") or "")

                values = (
                    message_id,
                    logical_message_id,
                    source_message_id,
                    conversation_id,
                    str(normalized.get("customer_id", "") or ""),
                    str(normalized.get("platform", "douyin") or "douyin"),
                    str(normalized.get("direction", "inbound") or "inbound"),
                    str(normalized.get("message_type", "text") or "text"),
                    content,
                    str(normalized.get("sender_id", "") or ""),
                    str(normalized.get("sender_name", "") or ""),
                    1 if bool(normalized.get("is_read", False)) else 0,
                    1 if bool(normalized.get("is_processed", False)) else 0,
                    str(normalized.get("ai_reply_content", "") or ""),
                    created_at,
                    str(normalized.get("customer_name", normalized.get("sender_name", "")) or ""),
                    str(normalized.get("processed_status", "") or ""),
                    str(normalized.get("processed_at", "") or ""),
                    str(normalized.get("processed_reason", "") or ""),
                )

                if message_id:
                    existing = conn.execute(
                        "SELECT id FROM messages WHERE message_id = ? LIMIT 1",
                        (message_id,),
                    ).fetchone()
                    if existing:
                        conn.execute(
                            """
                            UPDATE messages SET
                                logical_message_id = ?,
                                source_message_id = ?,
                                conversation_id = ?,
                                customer_id = ?,
                                platform = ?,
                                direction = ?,
                                message_type = ?,
                                content = ?,
                                sender_id = ?,
                                sender_name = ?,
                                is_read = ?,
                                is_processed = ?,
                                ai_reply_content = ?,
                                created_at = ?,
                                customer_name = ?,
                                processed_status = ?,
                                processed_at = ?,
                                processed_reason = ?
                            WHERE message_id = ?
                            """,
                            values[1:] + (message_id,),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO messages (
                                message_id, logical_message_id, source_message_id, conversation_id,
                                customer_id, platform, direction, message_type, content,
                                sender_id, sender_name, is_read, is_processed, ai_reply_content, created_at,
                                customer_name, processed_status, processed_at, processed_reason
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            values,
                        )
                else:
                    delete_clauses = []
                    delete_args = []
                    if logical_message_id:
                        delete_clauses.append("logical_message_id = ?")
                        delete_args.append(logical_message_id)
                    if source_message_id:
                        delete_clauses.append("source_message_id = ?")
                        delete_args.append(source_message_id)
                    if conversation_id and created_at and content:
                        delete_clauses.append("(conversation_id = ? AND created_at = ? AND content = ?)")
                        delete_args.extend([conversation_id, created_at, content])
                    if delete_clauses:
                        where = " OR ".join(delete_clauses)
                        conn.execute(f"DELETE FROM messages WHERE {where}", delete_args)
                    conn.execute(
                        """
                        INSERT INTO messages (
                            message_id, logical_message_id, source_message_id, conversation_id,
                            customer_id, platform, direction, message_type, content,
                            sender_id, sender_name, is_read, is_processed, ai_reply_content, created_at,
                            customer_name, processed_status, processed_at, processed_reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
        except Exception as e:
            logger.warning(f"消息双写同步 SQLite 失败，已保留 JSON 主链: {e}")
        finally:
            conn.close()

    def _sync_conversation_to_sqlite(self, conversation: dict) -> None:
        """将会话摘要同步到 SQLite 侧车库。"""
        payload = dict(conversation or {})
        conn = self._get_message_sqlite_conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO conversations (
                        conversation_id, platform, customer_id, customer_name, customer_avatar,
                        status, last_message_time, last_message_content, unread_count,
                        intent_level, purchase_intent_score, lead_score, follow_up_priority,
                        purchase_probability, estimated_deal_size, lifecycle_stage, buying_role,
                        signals_detected, risk_factors, opportunity_factors, intent_history,
                        intent_trend, last_analysis_time, next_follow_up_time,
                        last_completed_follow_up_signature, last_completed_follow_up_at,
                        last_dingtalk_notification_signature, last_dingtalk_notification_at,
                        last_dingtalk_notification_status, last_dingtalk_notification_error,
                        last_dingtalk_notification_task_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        platform=excluded.platform,
                        customer_id=excluded.customer_id,
                        customer_name=excluded.customer_name,
                        customer_avatar=excluded.customer_avatar,
                        status=excluded.status,
                        last_message_time=excluded.last_message_time,
                        last_message_content=excluded.last_message_content,
                        unread_count=excluded.unread_count,
                        intent_level=excluded.intent_level,
                        purchase_intent_score=excluded.purchase_intent_score,
                        lead_score=excluded.lead_score,
                        follow_up_priority=excluded.follow_up_priority,
                        purchase_probability=excluded.purchase_probability,
                        estimated_deal_size=excluded.estimated_deal_size,
                        lifecycle_stage=excluded.lifecycle_stage,
                        buying_role=excluded.buying_role,
                        signals_detected=excluded.signals_detected,
                        risk_factors=excluded.risk_factors,
                        opportunity_factors=excluded.opportunity_factors,
                        intent_history=excluded.intent_history,
                        intent_trend=excluded.intent_trend,
                        last_analysis_time=excluded.last_analysis_time,
                        next_follow_up_time=excluded.next_follow_up_time,
                        last_completed_follow_up_signature=excluded.last_completed_follow_up_signature,
                        last_completed_follow_up_at=excluded.last_completed_follow_up_at,
                        last_dingtalk_notification_signature=excluded.last_dingtalk_notification_signature,
                        last_dingtalk_notification_at=excluded.last_dingtalk_notification_at,
                        last_dingtalk_notification_status=excluded.last_dingtalk_notification_status,
                        last_dingtalk_notification_error=excluded.last_dingtalk_notification_error,
                        last_dingtalk_notification_task_id=excluded.last_dingtalk_notification_task_id,
                        created_at=CASE
                            WHEN conversations.created_at = '' THEN excluded.created_at
                            ELSE conversations.created_at
                        END,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(payload.get("conversation_id", "") or ""),
                        str(payload.get("platform", "douyin") or "douyin"),
                        str(payload.get("customer_id", "") or ""),
                        str(payload.get("customer_name", "") or ""),
                        str(payload.get("customer_avatar", "") or ""),
                        str(payload.get("status", "active") or "active"),
                        str(payload.get("last_message_time", "") or ""),
                        str(payload.get("last_message_content", "") or ""),
                        int(payload.get("unread_count", 0) or 0),
                        str(payload.get("intent_level", "") or ""),
                        float(payload.get("purchase_intent_score", 0) or 0),
                        str(payload.get("lead_score", "cold") or "cold"),
                        str(payload.get("follow_up_priority", "low") or "low"),
                        float(payload.get("purchase_probability", 0) or 0),
                        float(payload.get("estimated_deal_size", 0) or 0),
                        str(payload.get("lifecycle_stage", "prospect") or "prospect"),
                        str(payload.get("buying_role", "unknown") or "unknown"),
                        self._dump_json_text(payload.get("signals_detected", []), default="[]"),
                        self._dump_json_text(payload.get("risk_factors", []), default="[]"),
                        self._dump_json_text(payload.get("opportunity_factors", []), default="[]"),
                        self._dump_json_text(payload.get("intent_history", []), default="[]"),
                        self._dump_json_text(payload.get("intent_trend", {}), default="{}"),
                        str(payload.get("last_analysis_time", "") or ""),
                        str(payload.get("next_follow_up_time", "") or ""),
                        str(payload.get("last_completed_follow_up_signature", "") or ""),
                        str(payload.get("last_completed_follow_up_at", "") or ""),
                        str(payload.get("last_dingtalk_notification_signature", "") or ""),
                        str(payload.get("last_dingtalk_notification_at", "") or ""),
                        str(payload.get("last_dingtalk_notification_status", "") or ""),
                        str(payload.get("last_dingtalk_notification_error", "") or ""),
                        str(payload.get("last_dingtalk_notification_task_id", "") or ""),
                        str(payload.get("created_at", "") or ""),
                        str(payload.get("updated_at", "") or ""),
                    ),
                )
        except Exception as e:
            logger.warning(f"会话双写同步 SQLite 失败，已保留 JSON 主链: {e}")
        finally:
            conn.close()

    def _sync_processed_event_to_sqlite(self, event: dict) -> None:
        if not event or not event.get("logical_message_id"):
            return
        conn = self._get_message_sqlite_conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO processed_events (
                        logical_message_id, conversation_id, customer_name,
                        content_hash, source_message_id, platform,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(logical_message_id) DO UPDATE SET
                        conversation_id=excluded.conversation_id,
                        customer_name=excluded.customer_name,
                        content_hash=excluded.content_hash,
                        source_message_id=excluded.source_message_id,
                        platform=excluded.platform,
                        status=excluded.status,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(event.get("logical_message_id", "") or ""),
                        str(event.get("conversation_id", "") or ""),
                        str(event.get("customer_name", "") or ""),
                        str(event.get("content_hash", "") or ""),
                        str(event.get("source_message_id", "") or ""),
                        str(event.get("platform", "douyin") or "douyin"),
                        str(event.get("status", "") or ""),
                        str(event.get("created_at", "") or ""),
                        str(event.get("updated_at", "") or ""),
                    ),
                )
        except Exception as e:
            logger.debug(f"processed_events双写同步SQLite失败: {e}")
        finally:
            conn.close()

    def _sync_outbox_event_to_sqlite(self, event: dict) -> None:
        if not event or not event.get("outbox_id"):
            return
        conn = self._get_message_sqlite_conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO outbox_events (
                        outbox_id, logical_message_id, conversation_id, customer_name,
                        platform, reply_content, reply_hash, source_message_id,
                        status, retry_count, next_retry_at, message_id,
                        reason, workflow_run_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(outbox_id) DO UPDATE SET
                        logical_message_id=excluded.logical_message_id,
                        conversation_id=excluded.conversation_id,
                        customer_name=excluded.customer_name,
                        platform=excluded.platform,
                        reply_content=excluded.reply_content,
                        reply_hash=excluded.reply_hash,
                        source_message_id=excluded.source_message_id,
                        status=excluded.status,
                        retry_count=excluded.retry_count,
                        next_retry_at=excluded.next_retry_at,
                        message_id=excluded.message_id,
                        reason=excluded.reason,
                        workflow_run_id=excluded.workflow_run_id,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(event.get("outbox_id", "") or ""),
                        str(event.get("logical_message_id", "") or ""),
                        str(event.get("conversation_id", "") or ""),
                        str(event.get("customer_name", "") or ""),
                        str(event.get("platform", "douyin") or "douyin"),
                        str(event.get("reply_content", "") or ""),
                        str(event.get("reply_hash", "") or ""),
                        str(event.get("source_message_id", "") or ""),
                        str(event.get("status", "") or ""),
                        int(event.get("retry_count", 0) or 0),
                        str(event.get("next_retry_at", "") or ""),
                        str(event.get("message_id", "") or ""),
                        str(event.get("reason", "") or ""),
                        str(event.get("workflow_run_id", "") or ""),
                        str(event.get("created_at", "") or ""),
                        str(event.get("updated_at", "") or ""),
                    ),
                )
        except Exception as e:
            logger.debug(f"outbox_events双写同步SQLite失败: {e}")
        finally:
            conn.close()

    def _sync_workflow_run_to_sqlite(self, run: dict) -> None:
        if not run or not run.get("workflow_run_id"):
            return
        conn = self._get_message_sqlite_conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO workflow_runs (
                        workflow_run_id, conversation_id, customer_name, customer_id,
                        status, nodes, history, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_run_id) DO UPDATE SET
                        conversation_id=excluded.conversation_id,
                        customer_name=excluded.customer_name,
                        customer_id=excluded.customer_id,
                        status=excluded.status,
                        nodes=excluded.nodes,
                        history=excluded.history,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(run.get("workflow_run_id", "") or ""),
                        str(run.get("conversation_id", "") or ""),
                        str(run.get("customer_name", "") or ""),
                        str(run.get("customer_id", "") or ""),
                        str(run.get("status", "") or ""),
                        self._dump_json_text(run.get("nodes", {}), default="{}"),
                        self._dump_json_text(run.get("history", []), default="[]"),
                        str(run.get("created_at", "") or ""),
                        str(run.get("updated_at", "") or ""),
                    ),
                )
        except Exception as e:
            logger.debug(f"workflow_runs双写同步SQLite失败: {e}")
        finally:
            conn.close()

    def get_message_store_sync_summary(self) -> dict:
        """返回聊天 JSON 与 SQLite 侧车库的基础一致性摘要。"""
        json_messages = list(self._get_all_messages_from_json())
        json_conversations = list(self._get_all_conversations_from_json())
        summary = {
            "json_messages": len(json_messages),
            "json_conversations": len(json_conversations),
            "sqlite_messages": 0,
            "sqlite_conversations": 0,
            "message_delta": 0,
            "conversation_delta": 0,
            "in_sync": False,
        }

        conn = self._get_message_sqlite_conn()
        try:
            sqlite_messages = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] or 0)
            sqlite_conversations = int(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] or 0)
            summary["sqlite_messages"] = sqlite_messages
            summary["sqlite_conversations"] = sqlite_conversations
            summary["message_delta"] = sqlite_messages - summary["json_messages"]
            summary["conversation_delta"] = sqlite_conversations - summary["json_conversations"]
            summary["in_sync"] = summary["message_delta"] == 0 and summary["conversation_delta"] == 0
        finally:
            conn.close()
        return summary

    def get_conversation_messages_from_sqlite(self, conversation_id: str, limit: int = 9999) -> list:
        """从 SQLite 侧车库读取会话消息，保持与 JSON 读取接口兼容。"""
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return []

        query = (
            "SELECT message_id, logical_message_id, source_message_id, conversation_id, customer_id, "
            "platform, direction, message_type, content, sender_id, sender_name, is_read, "
            "is_processed, ai_reply_content, created_at, "
            "customer_name, processed_status, processed_at, processed_reason "
            "FROM messages WHERE conversation_id = ? ORDER BY created_at ASC"
        )
        params: list[Any] = [conversation_id]
        if limit and limit > 0 and limit < 9999:
            query = (
                "SELECT * FROM ("
                "SELECT message_id, logical_message_id, source_message_id, conversation_id, customer_id, "
                "platform, direction, message_type, content, sender_id, sender_name, is_read, "
                "is_processed, ai_reply_content, created_at, "
                "customer_name, processed_status, processed_at, processed_reason "
                "FROM messages WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?"
                ") ORDER BY created_at ASC"
            )
            params = [conversation_id, int(limit)]

        conn = self._get_message_sqlite_conn()
        try:
            rows = conn.execute(query, tuple(params)).fetchall()
        finally:
            conn.close()

        return [
            {
                "message_id": str(row["message_id"] or ""),
                "logical_message_id": str(row["logical_message_id"] or ""),
                "source_message_id": str(row["source_message_id"] or ""),
                "conversation_id": str(row["conversation_id"] or ""),
                "customer_id": str(row["customer_id"] or ""),
                "platform": str(row["platform"] or "douyin"),
                "direction": str(row["direction"] or "inbound"),
                "message_type": str(row["message_type"] or "text"),
                "content": str(row["content"] or ""),
                "sender_id": str(row["sender_id"] or ""),
                "sender_name": str(row["sender_name"] or ""),
                "is_read": bool(row["is_read"]),
                "is_processed": bool(row["is_processed"]),
                "ai_reply_content": str(row["ai_reply_content"] or ""),
                "created_at": str(row["created_at"] or ""),
                "customer_name": str(row["customer_name"] or ""),
                "processed_status": str(row["processed_status"] or ""),
                "processed_at": str(row["processed_at"] or ""),
                "processed_reason": str(row["processed_reason"] or ""),
            }
            for row in rows
        ]

    def get_conversation_by_id_from_sqlite(self, conversation_id: str) -> Optional[dict]:
        """从 SQLite 按主键精确查询单个会话，避免全表扫描。"""
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return None
        conn = self._get_message_sqlite_conn()
        try:
            row = conn.execute(
                """
                SELECT conversation_id, platform, customer_id, customer_name, customer_avatar,
                       status, last_message_time, last_message_content, unread_count,
                       intent_level, purchase_intent_score, lead_score, follow_up_priority,
                       purchase_probability, estimated_deal_size, lifecycle_stage, buying_role,
                       signals_detected, risk_factors, opportunity_factors, intent_history,
                       intent_trend, last_analysis_time, next_follow_up_time,
                       last_completed_follow_up_signature, last_completed_follow_up_at,
                       last_dingtalk_notification_signature, last_dingtalk_notification_at,
                       last_dingtalk_notification_status, last_dingtalk_notification_error,
                       last_dingtalk_notification_task_id,
                       created_at, updated_at
                FROM conversations
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return {
            "conversation_id": str(row["conversation_id"] or ""),
            "platform": str(row["platform"] or "douyin"),
            "customer_id": str(row["customer_id"] or ""),
            "customer_name": str(row["customer_name"] or ""),
            "customer_avatar": str(row["customer_avatar"] or ""),
            "status": str(row["status"] or "active"),
            "last_message_time": str(row["last_message_time"] or ""),
            "last_message_content": str(row["last_message_content"] or ""),
            "unread_count": int(row["unread_count"] or 0),
            "intent_level": str(row["intent_level"] or ""),
            "purchase_intent_score": float(row["purchase_intent_score"] or 0),
            "lead_score": str(row["lead_score"] or "cold"),
            "follow_up_priority": str(row["follow_up_priority"] or "low"),
            "purchase_probability": float(row["purchase_probability"] or 0),
            "estimated_deal_size": float(row["estimated_deal_size"] or 0),
            "lifecycle_stage": str(row["lifecycle_stage"] or "prospect"),
            "buying_role": str(row["buying_role"] or "unknown"),
            "signals_detected": self._load_json_text(row["signals_detected"], default=[]),
            "risk_factors": self._load_json_text(row["risk_factors"], default=[]),
            "opportunity_factors": self._load_json_text(row["opportunity_factors"], default=[]),
            "intent_history": self._load_json_text(row["intent_history"], default=[]),
            "intent_trend": self._load_json_text(row["intent_trend"], default={}),
            "last_analysis_time": str(row["last_analysis_time"] or ""),
            "next_follow_up_time": str(row["next_follow_up_time"] or ""),
            "last_completed_follow_up_signature": str(row["last_completed_follow_up_signature"] or ""),
            "last_completed_follow_up_at": str(row["last_completed_follow_up_at"] or ""),
            "last_dingtalk_notification_signature": str(row["last_dingtalk_notification_signature"] or ""),
            "last_dingtalk_notification_at": str(row["last_dingtalk_notification_at"] or ""),
            "last_dingtalk_notification_status": str(row["last_dingtalk_notification_status"] or ""),
            "last_dingtalk_notification_error": str(row["last_dingtalk_notification_error"] or ""),
            "last_dingtalk_notification_task_id": str(row["last_dingtalk_notification_task_id"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def get_all_messages_from_sqlite(self, limit: int = None) -> list:
        """从 SQLite 侧车库读取全部消息。"""
        query = (
            "SELECT message_id, logical_message_id, source_message_id, conversation_id, customer_id, "
            "platform, direction, message_type, content, sender_id, sender_name, is_read, "
            "is_processed, ai_reply_content, created_at, "
            "customer_name, processed_status, processed_at, processed_reason "
            "FROM messages ORDER BY created_at DESC"
        )
        params: list[Any] = []
        if limit:
            query += " LIMIT ?"
            params.append(int(limit))

        conn = self._get_message_sqlite_conn()
        try:
            rows = conn.execute(query, tuple(params)).fetchall()
        finally:
            conn.close()

        return [
            {
                "message_id": str(row["message_id"] or ""),
                "logical_message_id": str(row["logical_message_id"] or ""),
                "source_message_id": str(row["source_message_id"] or ""),
                "conversation_id": str(row["conversation_id"] or ""),
                "customer_id": str(row["customer_id"] or ""),
                "customer_name": str(row["customer_name"] or ""),
                "platform": str(row["platform"] or "douyin"),
                "direction": str(row["direction"] or "inbound"),
                "message_type": str(row["message_type"] or "text"),
                "content": str(row["content"] or ""),
                "sender_id": str(row["sender_id"] or ""),
                "sender_name": str(row["sender_name"] or ""),
                "is_read": bool(row["is_read"]),
                "is_processed": bool(row["is_processed"]),
                "ai_reply_content": str(row["ai_reply_content"] or ""),
                "created_at": str(row["created_at"] or ""),
                "processed_status": str(row["processed_status"] or ""),
                "processed_at": str(row["processed_at"] or ""),
                "processed_reason": str(row["processed_reason"] or ""),
            }
            for row in rows
        ]

    def get_all_conversations_from_sqlite(self) -> list:
        """从 SQLite 侧车库读取全部会话摘要。"""
        conn = self._get_message_sqlite_conn()
        try:
            rows = conn.execute(
                """
                SELECT conversation_id, platform, customer_id, customer_name, customer_avatar,
                       status, last_message_time, last_message_content, unread_count,
                       intent_level, purchase_intent_score, lead_score, follow_up_priority,
                       purchase_probability, estimated_deal_size, lifecycle_stage, buying_role,
                       signals_detected, risk_factors, opportunity_factors, intent_history,
                       intent_trend, last_analysis_time, next_follow_up_time,
                       last_completed_follow_up_signature, last_completed_follow_up_at,
                       last_dingtalk_notification_signature, last_dingtalk_notification_at,
                       last_dingtalk_notification_status, last_dingtalk_notification_error,
                       last_dingtalk_notification_task_id,
                       created_at, updated_at
                FROM conversations
                ORDER BY updated_at DESC
                """
            ).fetchall()
        finally:
            conn.close()

        return [
            {
                "conversation_id": str(row["conversation_id"] or ""),
                "platform": str(row["platform"] or "douyin"),
                "customer_id": str(row["customer_id"] or ""),
                "customer_name": str(row["customer_name"] or ""),
                "customer_avatar": str(row["customer_avatar"] or ""),
                "status": str(row["status"] or "active"),
                "last_message_time": str(row["last_message_time"] or ""),
                "last_message_content": str(row["last_message_content"] or ""),
                "unread_count": int(row["unread_count"] or 0),
                "intent_level": str(row["intent_level"] or ""),
                "purchase_intent_score": float(row["purchase_intent_score"] or 0),
                "lead_score": str(row["lead_score"] or "cold"),
                "follow_up_priority": str(row["follow_up_priority"] or "low"),
                "purchase_probability": float(row["purchase_probability"] or 0),
                "estimated_deal_size": float(row["estimated_deal_size"] or 0),
                "lifecycle_stage": str(row["lifecycle_stage"] or "prospect"),
                "buying_role": str(row["buying_role"] or "unknown"),
                "signals_detected": self._load_json_text(row["signals_detected"], default=[]),
                "risk_factors": self._load_json_text(row["risk_factors"], default=[]),
                "opportunity_factors": self._load_json_text(row["opportunity_factors"], default=[]),
                "intent_history": self._load_json_text(row["intent_history"], default=[]),
                "intent_trend": self._load_json_text(row["intent_trend"], default={}),
                "last_analysis_time": str(row["last_analysis_time"] or ""),
                "next_follow_up_time": str(row["next_follow_up_time"] or ""),
                "last_completed_follow_up_signature": str(row["last_completed_follow_up_signature"] or ""),
                "last_completed_follow_up_at": str(row["last_completed_follow_up_at"] or ""),
                "last_dingtalk_notification_signature": str(row["last_dingtalk_notification_signature"] or ""),
                "last_dingtalk_notification_at": str(row["last_dingtalk_notification_at"] or ""),
                "last_dingtalk_notification_status": str(row["last_dingtalk_notification_status"] or ""),
                "last_dingtalk_notification_error": str(row["last_dingtalk_notification_error"] or ""),
                "last_dingtalk_notification_task_id": str(row["last_dingtalk_notification_task_id"] or ""),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
            }
            for row in rows
        ]

    def get_message_store_reconcile_report(self, *, sample_size: int = 20) -> dict:
        """返回更细粒度的 JSON/SQLite 对账报告。"""
        json_messages = list(self._get_all_messages_from_json())
        json_conversations = list(self._get_all_conversations_from_json())
        sqlite_messages = list(self.get_all_messages_from_sqlite())
        sqlite_conversations = list(self.get_all_conversations_from_sqlite())

        def _message_key(item: dict) -> tuple[str, str, str, str]:
            return (
                str(item.get("message_id", "") or ""),
                str(item.get("logical_message_id", "") or ""),
                str(item.get("conversation_id", "") or ""),
                str(item.get("created_at", "") or ""),
            )

        def _conversation_key(item: dict) -> str:
            return str(item.get("conversation_id", "") or "")

        json_message_keys = {_message_key(item) for item in json_messages}
        sqlite_message_keys = {_message_key(item) for item in sqlite_messages}
        json_conversation_keys = {_conversation_key(item) for item in json_conversations}
        sqlite_conversation_keys = {_conversation_key(item) for item in sqlite_conversations}

        json_recent_sample = [
            {
                "message_id": str(item.get("message_id", "") or ""),
                "logical_message_id": str(item.get("logical_message_id", "") or ""),
                "conversation_id": str(item.get("conversation_id", "") or ""),
                "content": str(item.get("content", "") or ""),
                "direction": str(item.get("direction", "") or ""),
                "created_at": str(item.get("created_at", "") or ""),
            }
            for item in json_messages[:sample_size]
        ]
        sqlite_recent_sample = [
            {
                "message_id": str(item.get("message_id", "") or ""),
                "logical_message_id": str(item.get("logical_message_id", "") or ""),
                "conversation_id": str(item.get("conversation_id", "") or ""),
                "content": str(item.get("content", "") or ""),
                "direction": str(item.get("direction", "") or ""),
                "created_at": str(item.get("created_at", "") or ""),
            }
            for item in sqlite_messages[:sample_size]
        ]

        report = {
            "summary": self.get_message_store_sync_summary(),
            "message_keys_only_in_json": sorted(json_message_keys - sqlite_message_keys)[:sample_size],
            "message_keys_only_in_sqlite": sorted(sqlite_message_keys - json_message_keys)[:sample_size],
            "conversation_ids_only_in_json": sorted(json_conversation_keys - sqlite_conversation_keys)[:sample_size],
            "conversation_ids_only_in_sqlite": sorted(sqlite_conversation_keys - json_conversation_keys)[:sample_size],
            "json_recent_sample": json_recent_sample,
            "sqlite_recent_sample": sqlite_recent_sample,
            "recent_sample_matches": json_recent_sample == sqlite_recent_sample,
        }
        report["in_sync"] = (
            report["summary"].get("in_sync", False)
            and not report["message_keys_only_in_json"]
            and not report["message_keys_only_in_sqlite"]
            and not report["conversation_ids_only_in_json"]
            and not report["conversation_ids_only_in_sqlite"]
            and report["recent_sample_matches"]
        )
        return report

    @staticmethod
    def _serialize_customer_tags(tags: Any) -> str:
        if isinstance(tags, list):
            return json.dumps(tags, ensure_ascii=False)
        if isinstance(tags, str):
            return tags
        return "[]"

    @staticmethod
    def _deserialize_customer_tags(raw: Any) -> list:
        if isinstance(raw, list):
            return raw
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
                return data if isinstance(data, list) else []
            except json.JSONDecodeError:
                return []
        return []

    def _customer_to_sqlite_tuple(self, customer: dict) -> Tuple[Any, ...]:
        customer = self._normalize_customer_status_fields(customer)
        return (
            customer.get("sec_uid", ""),
            customer.get("platform", "douyin"),
            customer.get("nickname", ""),
            customer.get("unique_id", ""),
            customer.get("profile_url", ""),
            customer.get("source_video_url", ""),
            customer.get("first_source_video_url", ""),
            customer.get("comment_content", ""),
            customer.get("comment_time", ""),
            customer.get("status", "pending"),
            customer.get("interact_status", "pending"),
            customer.get("intent_level", "D"),
            float(customer.get("intent_score", 0) or 0),
            self._serialize_customer_tags(customer.get("tags", [])),
            customer.get("created_at", ""),
            customer.get("updated_at", ""),
            customer.get("ip_location", ""),
            customer.get("signature", ""),
            customer.get("avatar_url", ""),
            customer.get("video_title", ""),
            customer.get("author_name", ""),
            customer.get("first_crawl_task_id", ""),
        )

    def _customer_row_to_dict(self, row: sqlite3.Row) -> dict:
        source_video_url = row["source_video_url"]
        normalized_source_video_url = self._normalize_video_url(source_video_url)
        source_video_aweme_id = self._extract_aweme_id_from_url(normalized_source_video_url)
        source_video_kind = self._extract_detail_kind_from_url(normalized_source_video_url)
        if source_video_aweme_id:
            normalized_source_video_url = self._build_canonical_detail_url(
                source_video_aweme_id,
                source_video_kind or "video",
            )
        raw_first_source_video_url = row["first_source_video_url"] if "first_source_video_url" in row.keys() else ""
        normalized_first_source_video_url = self._normalize_video_url(raw_first_source_video_url)
        first_source_aweme_id = self._extract_aweme_id_from_url(normalized_first_source_video_url)
        first_source_kind = self._extract_detail_kind_from_url(normalized_first_source_video_url)
        if first_source_aweme_id:
            normalized_first_source_video_url = self._build_canonical_detail_url(
                first_source_aweme_id,
                first_source_kind or "video",
            )
        if not normalized_first_source_video_url:
            normalized_first_source_video_url = normalized_source_video_url
        return self._normalize_customer_status_fields({
            "sec_uid": row["sec_uid"],
            "platform": row["platform"],
            "nickname": row["nickname"],
            "unique_id": row["unique_id"],
            "profile_url": row["profile_url"],
            "source_video_url": normalized_source_video_url,
            "first_source_video_url": normalized_first_source_video_url,
            "comment_content": row["comment_content"],
            "comment_time": row["comment_time"],
            "status": row["status"],
            "interact_status": row["interact_status"] if "interact_status" in row.keys() else "",
            "intent_level": row["intent_level"],
            "intent_score": row["intent_score"] or 0,
            "tags": self._deserialize_customer_tags(row["tags"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "ip_location": row["ip_location"],
            "signature": row["signature"],
            "avatar_url": row["avatar_url"],
            "video_title": row["video_title"] if "video_title" in row.keys() else "",
            "author_name": row["author_name"] if "author_name" in row.keys() else "",
            "first_crawl_task_id": row["first_crawl_task_id"] if "first_crawl_task_id" in row.keys() else "",
        })

    @staticmethod
    def _normalize_customer_status_fields(customer: dict) -> dict:
        normalized = copy.deepcopy(customer or {})
        direct_status = str(normalized.get("status", "pending") or "pending").strip().lower()
        interact_status = str(normalized.get("interact_status", "") or "").strip().lower()
        normalized["source_video_url"] = str(normalized.get("source_video_url", "") or "").strip()
        normalized["first_source_video_url"] = str(
            normalized.get("first_source_video_url") or normalized.get("source_video_url") or ""
        ).strip()

        # 兼容历史数据：旧版本把一键互动直接写到了 status=interacted。
        if direct_status == "interacted":
            direct_status = "pending"
            if not interact_status or interact_status == "pending":
                interact_status = "interacted"

        if not direct_status:
            direct_status = "pending"
        if not interact_status:
            interact_status = "pending"

        normalized["status"] = direct_status
        normalized["interact_status"] = interact_status
        return normalized

    @staticmethod
    def _parse_customer_comment_timestamp(value: Any) -> int:
        if value in (None, ""):
            return 0
        text = str(value).strip()
        if not text:
            return 0
        if text.isdigit():
            try:
                raw = int(text)
                if len(text) >= 13:
                    return raw // 1000
                return raw
            except ValueError:
                return 0
        normalized = text.replace("T", " ")
        try:
            return int(datetime.fromisoformat(normalized).timestamp())
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(text[:19], fmt).timestamp())
            except ValueError:
                continue
        return 0

    @staticmethod
    def _comment_time_to_string(value: Any) -> str:
        if value in (None, ""):
            return ""
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(int(value)).strftime('%Y-%m-%d %H:%M:%S')
            except (ValueError, OSError):
                return str(value)

        text = str(value).strip()
        if not text:
            return ""
        if text.isdigit():
            try:
                raw = int(text)
                if len(text) >= 13:
                    raw = raw // 1000
                return datetime.fromtimestamp(raw).strftime('%Y-%m-%d %H:%M:%S')
            except (ValueError, OSError):
                return text
        return text

    @staticmethod
    def _customer_comment_timestamp_sql() -> str:
        return (
            "CASE "
            "WHEN TRIM(COALESCE(comment_time, '')) = '' THEN 0 "
            "WHEN comment_time NOT GLOB '*[^0-9]*' AND LENGTH(comment_time) >= 13 "
            "THEN CAST(SUBSTR(comment_time, 1, 13) AS INTEGER) / 1000 "
            "WHEN comment_time NOT GLOB '*[^0-9]*' "
            "THEN CAST(SUBSTR(comment_time, 1, 10) AS INTEGER) "
            "ELSE COALESCE(CAST(strftime('%s', REPLACE(SUBSTR(comment_time, 1, 19), 'T', ' ')) AS INTEGER), 0) "
            "END"
        )

    def _merge_customer_record(self, existing_customer: dict, new_customer: dict) -> dict:
        merged = copy.deepcopy(existing_customer)
        now = datetime.now().isoformat()
        existing_ts = self._parse_customer_comment_timestamp(existing_customer.get("comment_time", ""))
        new_ts = self._parse_customer_comment_timestamp(new_customer.get("comment_time", ""))
        should_refresh_comment = bool(new_ts and new_ts >= existing_ts)
        first_source_video_url = (
            str(existing_customer.get("first_source_video_url", "") or "").strip()
            or str(existing_customer.get("source_video_url", "") or "").strip()
            or str(new_customer.get("first_source_video_url", "") or "").strip()
            or str(new_customer.get("source_video_url", "") or "").strip()
        )
        stable_source_video_url = (
            str(existing_customer.get("source_video_url", "") or "").strip()
            or str(existing_customer.get("first_source_video_url", "") or "").strip()
            or str(new_customer.get("source_video_url", "") or "").strip()
            or str(new_customer.get("first_source_video_url", "") or "").strip()
        )

        for field in ("nickname", "unique_id", "profile_url", "ip_location", "signature", "avatar_url"):
            if new_customer.get(field):
                merged[field] = new_customer.get(field)

        merged["source_video_url"] = stable_source_video_url
        merged["first_source_video_url"] = first_source_video_url

        if should_refresh_comment:
            for field in ("comment_content", "comment_time"):
                if new_customer.get(field):
                    merged[field] = new_customer.get(field)
        else:
            for field in ("comment_content", "comment_time"):
                if not merged.get(field) and new_customer.get(field):
                    merged[field] = new_customer.get(field)

        merged["updated_at"] = now
        merged["created_at"] = existing_customer.get("created_at", now)
        merged["status"] = existing_customer.get("status", "pending")
        merged["interact_status"] = existing_customer.get("interact_status", "pending")
        merged["intent_level"] = existing_customer.get("intent_level", "D")
        merged["intent_score"] = existing_customer.get("intent_score", 0)
        merged["tags"] = existing_customer.get("tags", [])
        merged["first_crawl_task_id"] = (
            existing_customer.get("first_crawl_task_id")
            or new_customer.get("first_crawl_task_id", "")
            or ""
        )
        return self._normalize_customer_status_fields(merged)

    def _upsert_customers_sqlite(self, customers: List[dict]):
        if not customers:
            return

        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            with conn:
                conn.executemany("""
                    INSERT INTO customers (
                        sec_uid, platform, nickname, unique_id, profile_url, source_video_url,
                        first_source_video_url, comment_content, comment_time, status, interact_status, intent_level, intent_score,
                        tags, created_at, updated_at, ip_location, signature, avatar_url, video_title, author_name,
                        first_crawl_task_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, sec_uid) DO UPDATE SET
                        nickname=excluded.nickname,
                        unique_id=excluded.unique_id,
                        profile_url=excluded.profile_url,
                        source_video_url=CASE
                            WHEN customers.source_video_url != '' THEN customers.source_video_url
                            WHEN customers.first_source_video_url != '' THEN customers.first_source_video_url
                            ELSE excluded.source_video_url
                        END,
                        first_source_video_url=CASE
                            WHEN customers.first_source_video_url != '' THEN customers.first_source_video_url
                            WHEN customers.source_video_url != '' THEN customers.source_video_url
                            ELSE excluded.first_source_video_url
                        END,
                        comment_content=excluded.comment_content,
                        comment_time=excluded.comment_time,
                        status=excluded.status,
                        interact_status=excluded.interact_status,
                        intent_level=excluded.intent_level,
                        intent_score=excluded.intent_score,
                        tags=excluded.tags,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at,
                        ip_location=excluded.ip_location,
                        signature=excluded.signature,
                        avatar_url=excluded.avatar_url,
                        video_title=excluded.video_title,
                        author_name=excluded.author_name,
                        first_crawl_task_id=CASE
                            WHEN customers.first_crawl_task_id != '' THEN customers.first_crawl_task_id
                            ELSE excluded.first_crawl_task_id
                        END
                """, [self._customer_to_sqlite_tuple(customer) for customer in customers if customer.get("sec_uid")])
        finally:
            conn.close()

    @staticmethod
    def _normalize_timestamp(value: Any) -> int:
        try:
            if value in (None, ""):
                return 0
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def upsert_crawled_videos(
        self,
        platform: str,
        videos: List[dict],
        search_keyword: str = "",
    ) -> dict:
        """批量写入/更新视频元数据，为评论爬取队列提供基础数据。"""
        if not videos:
            return {
                "total": 0,
                "inserted": 0,
                "existing": 0,
                "existing_completed": 0,
                "existing_unfinished": 0,
                "aweme_ids": [],
            }

        platform_name = (platform or "douyin").strip() or "douyin"
        now = datetime.now().isoformat()
        deduped: List[dict] = []
        seen_ids = set()
        for raw in videos:
            if not isinstance(raw, dict):
                continue
            aweme_id = str(raw.get("aweme_id") or "").strip()
            video_url = self._normalize_video_url(raw.get("url") or raw.get("video_url") or "")
            if not aweme_id:
                aweme_id = self._extract_aweme_id_from_url(video_url)
            if not aweme_id or aweme_id in seen_ids:
                continue
            seen_ids.add(aweme_id)
            detail_kind = str(raw.get("content_type") or "").strip().lower()
            if detail_kind not in {"video", "note"}:
                detail_kind = self._extract_detail_kind_from_url(video_url)
            canonical_video_url = self._build_canonical_detail_url(
                aweme_id,
                detail_kind or "video",
            )
            if canonical_video_url:
                video_url = canonical_video_url

            author = raw.get("author") or {}
            author_name = author.get("nickname") if isinstance(author, dict) else raw.get("author", "")
            author_sec_uid = ""
            author_unique_id = ""
            if isinstance(author, dict):
                author_sec_uid = str(author.get("sec_uid", "") or raw.get("author_id", "") or raw.get("author_sec_uid", "")).strip()
                author_unique_id = str(author.get("unique_id", "") or raw.get("author_unique_id", "")).strip()

            publish_timestamp = self._normalize_timestamp(
                raw.get("publish_timestamp")
                or raw.get("publish_time")
                or raw.get("create_time")
            )
            comment_count = self._normalize_timestamp(raw.get("comment_count"))
            like_count = self._normalize_timestamp(raw.get("like_count"))
            share_count = self._normalize_timestamp(raw.get("share_count"))
            play_count = self._normalize_timestamp(raw.get("play_count"))
            crawl_priority_score = float(
                publish_timestamp + min(comment_count, 100000) + min(like_count, 100000) / 10.0
            )

            deduped.append({
                "platform": platform_name,
                "aweme_id": aweme_id,
                "video_url": video_url or self._build_canonical_detail_url(aweme_id, detail_kind or "video"),
                "normalized_video_url": video_url or self._build_canonical_detail_url(aweme_id, detail_kind or "video"),
                "title": str(raw.get("title", "") or "")[:500],
                "author_name": str(author_name or raw.get("author_name", "") or "")[:200],
                "author_sec_uid": author_sec_uid[:120],
                "author_unique_id": author_unique_id[:120],
                "publish_timestamp": publish_timestamp,
                "like_count": like_count,
                "comment_count": comment_count,
                "share_count": share_count,
                "play_count": play_count,
                "search_keyword": str(search_keyword or raw.get("search_keyword", "") or "")[:200],
                "first_discovered_at": now,
                "last_discovered_at": now,
                "crawl_priority_score": crawl_priority_score,
            })

        if not deduped:
            return {
                "total": 0,
                "inserted": 0,
                "existing": 0,
                "existing_completed": 0,
                "existing_unfinished": 0,
                "aweme_ids": [],
            }

        self._init_customer_sqlite()
        aweme_ids = [item["aweme_id"] for item in deduped]
        placeholders = ",".join("?" for _ in aweme_ids)
        conn = self._get_customer_sqlite_conn()
        try:
            existing_rows = conn.execute(
                f"""
                SELECT aweme_id, comment_crawl_status
                FROM crawled_videos
                WHERE platform = ? AND aweme_id IN ({placeholders})
                """,
                [platform_name] + aweme_ids,
            ).fetchall()
            existing_map = {
                str(row["aweme_id"]): str(row["comment_crawl_status"] or self.VIDEO_STATUS_PENDING)
                for row in existing_rows
            }

            with conn:
                conn.executemany(
                    """
                    INSERT INTO crawled_videos (
                        platform, aweme_id, video_url, normalized_video_url, title,
                        author_name, author_sec_uid, author_unique_id, publish_timestamp,
                        like_count, comment_count, share_count, play_count, search_keyword,
                        first_discovered_at, last_discovered_at, crawl_priority_score
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, aweme_id) DO UPDATE SET
                        video_url=excluded.video_url,
                        normalized_video_url=excluded.normalized_video_url,
                        title=CASE WHEN excluded.title != '' THEN excluded.title ELSE crawled_videos.title END,
                        author_name=CASE WHEN excluded.author_name != '' THEN excluded.author_name ELSE crawled_videos.author_name END,
                        author_sec_uid=CASE WHEN excluded.author_sec_uid != '' THEN excluded.author_sec_uid ELSE crawled_videos.author_sec_uid END,
                        author_unique_id=CASE WHEN excluded.author_unique_id != '' THEN excluded.author_unique_id ELSE crawled_videos.author_unique_id END,
                        publish_timestamp=CASE
                            WHEN excluded.publish_timestamp > 0 THEN excluded.publish_timestamp
                            ELSE crawled_videos.publish_timestamp
                        END,
                        like_count=CASE WHEN excluded.like_count > 0 THEN excluded.like_count ELSE crawled_videos.like_count END,
                        comment_count=CASE WHEN excluded.comment_count > 0 THEN excluded.comment_count ELSE crawled_videos.comment_count END,
                        share_count=CASE WHEN excluded.share_count > 0 THEN excluded.share_count ELSE crawled_videos.share_count END,
                        play_count=CASE WHEN excluded.play_count > 0 THEN excluded.play_count ELSE crawled_videos.play_count END,
                        search_keyword=CASE WHEN excluded.search_keyword != '' THEN excluded.search_keyword ELSE crawled_videos.search_keyword END,
                        first_discovered_at=CASE
                            WHEN crawled_videos.first_discovered_at = '' THEN excluded.first_discovered_at
                            ELSE crawled_videos.first_discovered_at
                        END,
                        last_discovered_at=excluded.last_discovered_at,
                        crawl_priority_score=excluded.crawl_priority_score
                    """,
                    [
                        (
                            item["platform"],
                            item["aweme_id"],
                            item["video_url"],
                            item["normalized_video_url"],
                            item["title"],
                            item["author_name"],
                            item["author_sec_uid"],
                            item["author_unique_id"],
                            item["publish_timestamp"],
                            item["like_count"],
                            item["comment_count"],
                            item["share_count"],
                            item["play_count"],
                            item["search_keyword"],
                            item["first_discovered_at"],
                            item["last_discovered_at"],
                            item["crawl_priority_score"],
                        )
                        for item in deduped
                    ],
                )
        finally:
            conn.close()

        existing_completed = sum(1 for status in existing_map.values() if status == self.VIDEO_STATUS_COMPLETED)
        existing_unfinished = sum(
            1
            for status in existing_map.values()
            if status in {self.VIDEO_STATUS_PENDING, self.VIDEO_STATUS_IN_PROGRESS}
        )
        return {
            "total": len(deduped),
            "inserted": len(deduped) - len(existing_map),
            "existing": len(existing_map),
            "existing_completed": existing_completed,
            "existing_unfinished": existing_unfinished,
            "aweme_ids": aweme_ids,
        }

    def get_video_comment_status_counts(
        self,
        platform: str = "douyin",
        search_keyword: str = "",
    ) -> Dict[str, int]:
        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            clauses = ["platform = ?"]
            params: List[Any] = [platform or "douyin"]
            if search_keyword:
                clauses.append("search_keyword = ?")
                params.append(search_keyword)
            where_clause = f"WHERE {' AND '.join(clauses)}"
            rows = conn.execute(
                f"""
                SELECT comment_crawl_status, COUNT(*) AS total
                FROM crawled_videos
                {where_clause}
                GROUP BY comment_crawl_status
                """,
                params,
            ).fetchall()
            counts = {
                self.VIDEO_STATUS_PENDING: 0,
                self.VIDEO_STATUS_IN_PROGRESS: 0,
                self.VIDEO_STATUS_COMPLETED: 0,
                self.VIDEO_STATUS_FAILED: 0,
                self.VIDEO_STATUS_SKIPPED: 0,
            }
            for row in rows:
                counts[str(row["comment_crawl_status"] or self.VIDEO_STATUS_PENDING)] = int(row["total"] or 0)
            return counts
        finally:
            conn.close()

    def cleanup_unfinished_crawled_videos(
        self,
        platform: str = "douyin",
        search_keyword: str = "",
    ) -> Dict[str, Any]:
        """清理指定关键词下未完成的视频队列，保留已完成记录。"""
        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            clauses = [
                "platform = ?",
                "COALESCE(comment_crawl_status, ?) IN (?, ?)",
            ]
            params: List[Any] = [
                platform or "douyin",
                self.VIDEO_STATUS_PENDING,
                self.VIDEO_STATUS_PENDING,
                self.VIDEO_STATUS_IN_PROGRESS,
            ]
            if search_keyword:
                clauses.append("search_keyword = ?")
                params.append(search_keyword)
            where_clause = f"WHERE {' AND '.join(clauses)}"
            rows = conn.execute(
                f"""
                SELECT aweme_id, comment_crawl_status
                FROM crawled_videos
                {where_clause}
                """,
                params,
            ).fetchall()
            deleted_aweme_ids = [str(row["aweme_id"] or "").strip() for row in rows if str(row["aweme_id"] or "").strip()]
            deleted_status_counts: Dict[str, int] = {}
            for row in rows:
                status = str(row["comment_crawl_status"] or self.VIDEO_STATUS_PENDING)
                deleted_status_counts[status] = int(deleted_status_counts.get(status, 0) or 0) + 1
            if deleted_aweme_ids:
                with conn:
                    conn.execute(
                        f"DELETE FROM crawled_videos {where_clause}",
                        params,
                    )
            return {
                "deleted_count": len(deleted_aweme_ids),
                "deleted_aweme_ids": deleted_aweme_ids,
                "deleted_status_counts": deleted_status_counts,
            }
        finally:
            conn.close()

    def _sync_handoff_ticket_to_sqlite(self, ticket: dict) -> None:
        if not ticket or not ticket.get("ticket_id"):
            return
        conn = self._get_message_sqlite_conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO handoff_tickets (
                        ticket_id, workflow_run_id, logical_message_id, conversation_id, customer_id,
                        customer_name, platform, reason_code, handoff_level, status, owner, source,
                        suggested_reply, raw_customer_message, eligibility_snapshot, reply_analysis_snapshot,
                        schema_id, enterprise_id, claimed_at, resolved_at, escalate_at, resolution_note,
                        approved_reply, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ticket_id) DO UPDATE SET
                        workflow_run_id=excluded.workflow_run_id,
                        logical_message_id=excluded.logical_message_id,
                        conversation_id=excluded.conversation_id,
                        customer_id=excluded.customer_id,
                        customer_name=excluded.customer_name,
                        platform=excluded.platform,
                        reason_code=excluded.reason_code,
                        handoff_level=excluded.handoff_level,
                        status=excluded.status,
                        owner=excluded.owner,
                        source=excluded.source,
                        suggested_reply=excluded.suggested_reply,
                        raw_customer_message=excluded.raw_customer_message,
                        eligibility_snapshot=excluded.eligibility_snapshot,
                        reply_analysis_snapshot=excluded.reply_analysis_snapshot,
                        schema_id=excluded.schema_id,
                        enterprise_id=excluded.enterprise_id,
                        claimed_at=excluded.claimed_at,
                        resolved_at=excluded.resolved_at,
                        escalate_at=excluded.escalate_at,
                        resolution_note=excluded.resolution_note,
                        approved_reply=excluded.approved_reply,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(ticket.get("ticket_id", "") or ""),
                        str(ticket.get("workflow_run_id", "") or ""),
                        str(ticket.get("logical_message_id", "") or ""),
                        str(ticket.get("conversation_id", "") or ""),
                        str(ticket.get("customer_id", "") or ""),
                        str(ticket.get("customer_name", "") or ""),
                        str(ticket.get("platform", "") or ""),
                        str(ticket.get("reason_code", "") or ""),
                        str(ticket.get("handoff_level", "") or ""),
                        str(ticket.get("status", "") or ""),
                        str(ticket.get("owner", "") or ""),
                        str(ticket.get("source", "") or ""),
                        str(ticket.get("suggested_reply", "") or ""),
                        str(ticket.get("raw_customer_message", "") or ""),
                        self._dump_json_text(ticket.get("eligibility_snapshot", {}), default="{}"),
                        self._dump_json_text(ticket.get("reply_analysis_snapshot", {}), default="{}"),
                        str(ticket.get("schema_id", "") or ""),
                        str(ticket.get("enterprise_id", "") or ""),
                        str(ticket.get("claimed_at", "") or ""),
                        str(ticket.get("resolved_at", "") or ""),
                        str(ticket.get("escalate_at", "") or ""),
                        str(ticket.get("resolution_note", "") or ""),
                        str(ticket.get("approved_reply", "") or ""),
                        str(ticket.get("created_at", "") or ""),
                        str(ticket.get("updated_at", "") or ""),
                    ),
                )
        except Exception as e:
            logger.debug(f"handoff_tickets双写同步SQLite失败: {e}")
        finally:
            conn.close()

    def get_video_comment_crawl_queue(
        self,
        platform: str = "douyin",
        limit: int = 20,
        search_keyword: str = "",
        priority: str = VIDEO_QUEUE_PRIORITY_LATEST,
        include_completed: bool = False,
        stale_in_progress_minutes: int = 30,
    ) -> List[dict]:
        """按视频状态与优先级返回待抓评论队列。"""
        self._init_customer_sqlite()
        safe_limit = min(max(int(limit or 20), 1), 500)
        stale_cutoff = int(time.time()) - max(int(stale_in_progress_minutes or 30), 1) * 60
        conn = self._get_customer_sqlite_conn()
        try:
            clauses = ["platform = ?"]
            params: List[Any] = [platform or "douyin"]
            if search_keyword:
                clauses.append("search_keyword = ?")
                params.append(search_keyword)
            where_clause = f"WHERE {' AND '.join(clauses)}"
            rows = conn.execute(
                f"""
                SELECT *
                FROM crawled_videos
                {where_clause}
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        items: List[dict] = []
        for row in rows:
            item = dict(row)
            status = str(item.get("comment_crawl_status") or self.VIDEO_STATUS_PENDING)
            if status in {
                self.VIDEO_STATUS_COMPLETED,
                self.VIDEO_STATUS_FAILED,
                self.VIDEO_STATUS_SKIPPED,
            } and not include_completed:
                continue
            if status == self.VIDEO_STATUS_IN_PROGRESS:
                started_ts = self._iso_to_unix_seconds(item.get("comment_crawl_started_at"))
                if started_ts and started_ts > stale_cutoff:
                    continue
            items.append(item)

        def _status_priority(item: dict) -> int:
            status = str(item.get("comment_crawl_status") or self.VIDEO_STATUS_PENDING)
            if status == self.VIDEO_STATUS_IN_PROGRESS:
                return 0
            if status == self.VIDEO_STATUS_FAILED:
                return 1
            if status == self.VIDEO_STATUS_PENDING:
                return 2
            if status == self.VIDEO_STATUS_COMPLETED:
                return 3
            if status == self.VIDEO_STATUS_SKIPPED:
                return 4
            return 5

        def _sort_key(item: dict):
            publish_ts = self._normalize_timestamp(item.get("publish_timestamp"))
            comment_count = self._normalize_timestamp(item.get("comment_count"))
            like_count = self._normalize_timestamp(item.get("like_count"))
            discovered_ts = self._iso_to_unix_seconds(item.get("last_discovered_at"))
            if priority == self.VIDEO_QUEUE_PRIORITY_HOT:
                return (_status_priority(item), -comment_count, -like_count, -publish_ts, -discovered_ts)
            if priority == self.VIDEO_QUEUE_PRIORITY_DISCOVERED:
                return (_status_priority(item), -discovered_ts, -publish_ts, -comment_count)
            if priority == self.VIDEO_QUEUE_PRIORITY_PUBLISH:
                return (_status_priority(item), -publish_ts, -comment_count, -discovered_ts)
            return (_status_priority(item), -publish_ts, -discovered_ts, -comment_count, -like_count)

        items.sort(key=_sort_key)
        return items[:safe_limit]

    def get_crawled_video_status(self, platform: str, aweme_id: str) -> Optional[str]:
        """获取指定视频的评论爬取状态"""
        if not aweme_id:
            return None
        platform = platform or "douyin"
        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            row = conn.execute(
                "SELECT comment_crawl_status FROM crawled_videos WHERE platform = ? AND aweme_id = ?",
                [platform, aweme_id]
            ).fetchone()
            return row["comment_crawl_status"] if row else None
        finally:
            conn.close()

    def mark_video_comment_crawl_started(self, platform: str, aweme_id: str) -> None:
        if not aweme_id:
            return
        now = datetime.now().isoformat()
        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE crawled_videos
                    SET comment_crawl_status = ?,
                        comment_crawl_started_at = ?,
                        last_comment_crawl_at = ?,
                        comment_crawl_error = '',
                        comment_crawl_attempts = comment_crawl_attempts + 1
                    WHERE platform = ? AND aweme_id = ?
                    """,
                    [self.VIDEO_STATUS_IN_PROGRESS, now, now, platform or "douyin", aweme_id],
                )
        finally:
            conn.close()

    def finalize_video_comment_crawl(
        self,
        platform: str,
        aweme_id: str,
        status: str,
        result: Optional[dict] = None,
        error_message: str = "",
    ) -> None:
        if not aweme_id:
            return
        payload = result or {}
        now = datetime.now().isoformat()
        normalized_status = status if status in {
            self.VIDEO_STATUS_PENDING,
            self.VIDEO_STATUS_IN_PROGRESS,
            self.VIDEO_STATUS_COMPLETED,
            self.VIDEO_STATUS_FAILED,
            self.VIDEO_STATUS_SKIPPED,
        } else self.VIDEO_STATUS_FAILED
        error_text = str(error_message or payload.get("completeness_warning") or payload.get("termination_reason") or "").strip()
        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE crawled_videos
                    SET comment_crawl_status = ?,
                        comment_crawl_finished_at = ?,
                        last_comment_crawl_at = ?,
                        comment_crawl_error = ?,
                        last_crawl_matched_comments = ?,
                        last_crawl_saved_customers = ?,
                        last_crawl_total_comments = ?,
                        last_crawl_duplicate_comments = ?,
                        last_history_cutoff_timestamp = ?,
                        comment_count = CASE
                            WHEN ? > 0 THEN ?
                            ELSE comment_count
                        END
                    WHERE platform = ? AND aweme_id = ?
                    """,
                    [
                        normalized_status,
                        now,
                        now,
                        error_text[:1000],
                        self._normalize_timestamp(payload.get("matched_comments")),
                        self._normalize_timestamp(payload.get("saved_customers")),
                        self._normalize_timestamp(payload.get("total_comments")),
                        self._normalize_timestamp(payload.get("duplicate_comments")),
                        self._normalize_timestamp(payload.get("history_cutoff_timestamp")),
                        self._normalize_timestamp(payload.get("expected_comment_count")),
                        self._normalize_timestamp(payload.get("expected_comment_count")),
                        platform or "douyin",
                        aweme_id,
                    ],
                )
        finally:
            conn.close()

    def has_crawled_comment(self, platform: str, aweme_id: str, comment_id: str) -> bool:
        """检查某条评论是否已被历史爬取记录处理过。"""
        if not aweme_id or not comment_id:
            return False

        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            row = conn.execute(
                """
                SELECT 1
                FROM crawled_comments
                WHERE platform = ? AND aweme_id = ? AND comment_id = ?
                LIMIT 1
                """,
                [platform or "douyin", aweme_id, comment_id],
            ).fetchone()
            return row is not None
        except Exception as e:
            logger.warning(f"检查评论历史记录失败: {e}")
            return False
        finally:
            conn.close()

    def get_latest_crawled_comment_timestamp(self, platform: str, aweme_id: str) -> int:
        """获取某个视频最近一次已处理评论的最大时间戳。"""
        if not aweme_id:
            return 0

        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            row = conn.execute(
                """
                SELECT MAX(comment_timestamp) AS latest_ts
                FROM crawled_comments
                WHERE platform = ? AND aweme_id = ?
                """,
                [platform or "douyin", aweme_id],
            ).fetchone()
            if not row:
                return 0
            return self._normalize_timestamp(row["latest_ts"])
        except Exception as e:
            logger.warning(f"获取评论历史时间戳失败: {e}")
            return 0
        finally:
            conn.close()

    def record_crawled_comments(
        self,
        platform: str,
        aweme_id: str,
        video_url: str,
        comments: List[dict],
        video_name: str = "",
        author_name: str = "",
    ) -> int:
        """记录已经扫描过的评论，用于后续按评论粒度去重。"""
        if not aweme_id or not comments:
            return 0

        rows = []
        crawled_at = datetime.now().isoformat()
        for comment in comments:
            comment_id = str(comment.get("cid", "") or "").strip()
            if not comment_id:
                continue
            rows.append(
                (
                    platform or "douyin",
                    aweme_id,
                    comment_id,
                    comment.get("sec_uid", "") or "",
                    video_url or "",
                    video_name or "",
                    author_name or "",
                    str(comment.get("text", "") or "")[:500],
                    self._normalize_timestamp(comment.get("create_time")),
                    max(int(comment.get("comment_level", 1) or 1), 1),
                    crawled_at,
                )
            )

        if not rows:
            return 0

        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            with conn:
                before = conn.total_changes
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO crawled_comments (
                        platform, aweme_id, comment_id, sec_uid, video_url,
                        video_name, author_name,
                        comment_content, comment_timestamp, comment_level, crawled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                return conn.total_changes - before
        except Exception as e:
            logger.warning(f"写入评论历史记录失败: {e}")
            return 0
        finally:
            conn.close()

    def record_comment_facts(
        self,
        platform: str,
        aweme_id: str,
        video_url: str,
        comments: List[dict],
        capture_session_id: str = "",
        video_title: str = "",
        author_name: str = "",
    ) -> int:
        """记录评论事实，用于评论级追溯与后续客户投影。"""
        if not aweme_id or not comments:
            return 0

        rows = []
        captured_at = datetime.now().isoformat()
        for comment in comments:
            comment_id = str(comment.get("cid", "") or "").strip()
            if not comment_id:
                continue
            parent_comment_id = str(comment.get("parent_cid", "") or "").strip()
            root_comment_id = parent_comment_id or comment_id
            rows.append(
                (
                    platform or "douyin",
                    aweme_id,
                    comment_id,
                    parent_comment_id,
                    root_comment_id,
                    max(int(comment.get("comment_level", 1) or 1), 1),
                    str(comment.get("sec_uid", "") or ""),
                    str(comment.get("nickname", "") or ""),
                    str(comment.get("unique_id", "") or ""),
                    str(comment.get("text", "") or "")[:1000],
                    str(comment.get("matched_keyword", "") or "")[:200],
                    self._normalize_timestamp(comment.get("create_time")),
                    self._normalize_timestamp(comment.get("digg_count")),
                    str(comment.get("ip_location", "") or "")[:200],
                    video_url or "",
                    video_title or str(comment.get("video_title", "") or ""),
                    author_name or str(comment.get("author_name", "") or ""),
                    str(capture_session_id or "")[:120],
                    captured_at,
                    self._dump_json_text(comment, default="{}"),
                )
            )

        if not rows:
            return 0

        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            with conn:
                before = conn.total_changes
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO comment_facts (
                        platform, aweme_id, comment_id, parent_comment_id, root_comment_id,
                        comment_level, sec_uid, nickname, unique_id, comment_content,
                        matched_keyword, comment_timestamp, digg_count, ip_location,
                        video_url, video_title, author_name, capture_session_id, captured_at, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                return conn.total_changes - before
        except Exception as e:
            logger.warning(f"写入评论事实失败: {e}")
            return 0
        finally:
            conn.close()

    def upsert_comment_crawl_session(self, session_data: Dict[str, Any]) -> str:
        """创建或更新评论采集会话。"""
        session_id = str(session_data.get("session_id", "") or "").strip()
        if not session_id:
            session_id = f"comment_session_{int(time.time() * 1000)}"

        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO comment_crawl_sessions (
                        session_id, platform, aweme_id, video_url, search_task_id, crawl_mode,
                        started_at, finished_at, comment_time_start, comment_time_end,
                        reply_collection_enabled, completion_status, termination_reason, completeness_warning,
                        risk_control_detected, risk_control_reason, history_cutoff_timestamp,
                        expected_comment_count, top_level_comment_count, reply_comment_count, total_comment_count,
                        matched_comment_count, saved_customer_count, duplicate_comment_count,
                        time_filtered_count, keyword_filtered_count, history_recorded_count,
                        fact_recorded_count, customer_linked_count, api_request_count, reply_api_request_count,
                        reply_expand_clicks, dropped_batches_count, peak_pending_batches, latest_batch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        platform = excluded.platform,
                        aweme_id = excluded.aweme_id,
                        video_url = excluded.video_url,
                        search_task_id = excluded.search_task_id,
                        crawl_mode = excluded.crawl_mode,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at,
                        comment_time_start = excluded.comment_time_start,
                        comment_time_end = excluded.comment_time_end,
                        reply_collection_enabled = excluded.reply_collection_enabled,
                        completion_status = excluded.completion_status,
                        termination_reason = excluded.termination_reason,
                        completeness_warning = excluded.completeness_warning,
                        risk_control_detected = excluded.risk_control_detected,
                        risk_control_reason = excluded.risk_control_reason,
                        history_cutoff_timestamp = excluded.history_cutoff_timestamp,
                        expected_comment_count = excluded.expected_comment_count,
                        top_level_comment_count = excluded.top_level_comment_count,
                        reply_comment_count = excluded.reply_comment_count,
                        total_comment_count = excluded.total_comment_count,
                        matched_comment_count = excluded.matched_comment_count,
                        saved_customer_count = excluded.saved_customer_count,
                        duplicate_comment_count = excluded.duplicate_comment_count,
                        time_filtered_count = excluded.time_filtered_count,
                        keyword_filtered_count = excluded.keyword_filtered_count,
                        history_recorded_count = excluded.history_recorded_count,
                        fact_recorded_count = excluded.fact_recorded_count,
                        customer_linked_count = excluded.customer_linked_count,
                        api_request_count = excluded.api_request_count,
                        reply_api_request_count = excluded.reply_api_request_count,
                        reply_expand_clicks = excluded.reply_expand_clicks,
                        dropped_batches_count = excluded.dropped_batches_count,
                        peak_pending_batches = excluded.peak_pending_batches,
                        latest_batch_id = excluded.latest_batch_id
                    """,
                    [
                        session_id,
                        str(session_data.get("platform", "douyin") or "douyin"),
                        str(session_data.get("aweme_id", "") or ""),
                        str(session_data.get("video_url", "") or ""),
                        str(session_data.get("search_task_id", "") or ""),
                        str(session_data.get("crawl_mode", "incremental") or "incremental"),
                        str(session_data.get("started_at", "") or ""),
                        str(session_data.get("finished_at", "") or ""),
                        str(session_data.get("comment_time_start", "") or ""),
                        str(session_data.get("comment_time_end", "") or ""),
                        1 if bool(session_data.get("reply_collection_enabled")) else 0,
                        str(session_data.get("completion_status", "") or ""),
                        str(session_data.get("termination_reason", "") or ""),
                        str(session_data.get("completeness_warning", "") or "")[:1000],
                        1 if bool(session_data.get("risk_control_detected")) else 0,
                        str(session_data.get("risk_control_reason", "") or "")[:500],
                        self._normalize_timestamp(session_data.get("history_cutoff_timestamp")),
                        self._normalize_timestamp(session_data.get("expected_comment_count")),
                        self._normalize_timestamp(session_data.get("top_level_comment_count")),
                        self._normalize_timestamp(session_data.get("reply_comment_count")),
                        self._normalize_timestamp(session_data.get("total_comment_count")),
                        self._normalize_timestamp(session_data.get("matched_comment_count")),
                        self._normalize_timestamp(session_data.get("saved_customer_count")),
                        self._normalize_timestamp(session_data.get("duplicate_comment_count")),
                        self._normalize_timestamp(session_data.get("time_filtered_count")),
                        self._normalize_timestamp(session_data.get("keyword_filtered_count")),
                        self._normalize_timestamp(session_data.get("history_recorded_count")),
                        self._normalize_timestamp(session_data.get("fact_recorded_count")),
                        self._normalize_timestamp(session_data.get("customer_linked_count")),
                        self._normalize_timestamp(session_data.get("api_request_count")),
                        self._normalize_timestamp(session_data.get("reply_api_request_count")),
                        self._normalize_timestamp(session_data.get("reply_expand_clicks")),
                        self._normalize_timestamp(session_data.get("dropped_batches_count")),
                        self._normalize_timestamp(session_data.get("peak_pending_batches")),
                        self._normalize_timestamp(session_data.get("latest_batch_id")),
                    ],
                )
            return session_id
        except Exception as e:
            logger.warning(f"写入评论采集会话失败: {e}")
            return session_id
        finally:
            conn.close()

    def link_customer_comments(self, links: List[Dict[str, Any]]) -> int:
        """建立客户与评论事实的关联。"""
        if not links:
            return 0

        rows = []
        linked_at = datetime.now().isoformat()
        for item in links:
            platform = str(item.get("platform", "douyin") or "douyin").strip() or "douyin"
            sec_uid = str(item.get("sec_uid", "") or "").strip()
            aweme_id = str(item.get("aweme_id", "") or "").strip()
            comment_id = str(item.get("comment_id", "") or "").strip()
            if not sec_uid or not aweme_id or not comment_id:
                continue
            rows.append(
                (
                    platform,
                    sec_uid,
                    aweme_id,
                    comment_id,
                    str(item.get("matched_keyword", "") or "")[:200],
                    str(item.get("search_task_id", "") or "")[:120],
                    linked_at,
                )
            )

        if not rows:
            return 0

        self._init_customer_sqlite()
        conn = self._get_customer_sqlite_conn()
        try:
            with conn:
                before = conn.total_changes
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO customer_comment_links (
                        platform, sec_uid, aweme_id, comment_id, matched_keyword, search_task_id, linked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                return conn.total_changes - before
        except Exception as e:
            logger.warning(f"写入客户评论关联失败: {e}")
            return 0
        finally:
            conn.close()

    def _build_customer_query_filters(
        self,
        platform: Optional[str] = None,
        status: Optional[str] = None,
        interact_status: Optional[str] = None,
        intent_level: Optional[str] = None,
        keyword: Optional[str] = None,
        comment_time_start: Optional[str] = None,
        comment_time_end: Optional[str] = None,
        created_after: Optional[str] = None,
        created_by_task_id: Optional[str] = None,
    ) -> Tuple[str, List[Any]]:
        clauses = []
        params: List[Any] = []

        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if interact_status:
            clauses.append("interact_status = ?")
            params.append(interact_status)
        if intent_level:
            clauses.append("intent_level = ?")
            params.append(intent_level)
        if keyword:
            keyword_like = f"%{keyword.lower()}%"
            clauses.append(
                "(LOWER(nickname) LIKE ? OR LOWER(unique_id) LIKE ? OR LOWER(comment_content) LIKE ?)"
            )
            params.extend([keyword_like, keyword_like, keyword_like])
        comment_time_start_ts = self._parse_customer_comment_timestamp(comment_time_start)
        if comment_time_start_ts:
            clauses.append(f"{self._customer_comment_timestamp_sql()} >= ?")
            params.append(comment_time_start_ts)
        comment_time_end_ts = self._parse_customer_comment_timestamp(comment_time_end)
        if comment_time_end_ts:
            clauses.append(f"{self._customer_comment_timestamp_sql()} <= ?")
            params.append(comment_time_end_ts)
        if created_after:
            # ISO-8601 string format allows for direct lexicographical comparison.
            # We strip trailing 'Z' and ensure both are compared directly.
            clean_created_after = str(created_after).replace('Z', '')
            clauses.append("created_at >= ?")
            params.append(clean_created_after)
        if created_by_task_id:
            clauses.append("first_crawl_task_id = ?")
            params.append(str(created_by_task_id).strip())

        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_clause, params

    def get_crawled_comment_video_page(
        self,
        page: int = 1,
        page_size: int = 20,
        platform: Optional[str] = None,
        aweme_id_keyword: Optional[str] = None,
    ) -> dict:
        self._init_customer_sqlite()
        safe_page = max(int(page or 1), 1)
        safe_page_size = min(max(int(page_size or 20), 1), 100)
        clauses = []
        params: List[Any] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if aweme_id_keyword:
            clauses.append("aweme_id LIKE ?")
            params.append(f"%{str(aweme_id_keyword).strip()}%")

        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        offset = (safe_page - 1) * safe_page_size
        conn = self._get_customer_sqlite_conn()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) AS total FROM (SELECT aweme_id FROM crawled_comments {where_clause} GROUP BY aweme_id)",
                params,
            ).fetchone()["total"]
            rows = conn.execute(
                f"""
                SELECT
                    aweme_id,
                    MAX(video_url) AS video_url,
                    MAX(video_name) AS video_name,
                    MAX(author_name) AS author_name,
                    COUNT(*) AS total_comments,
                    SUM(CASE WHEN comment_level > 1 THEN 1 ELSE 0 END) AS reply_comments,
                    SUM(CASE WHEN comment_level <= 1 THEN 1 ELSE 0 END) AS top_level_comments,
                    COUNT(DISTINCT CASE WHEN sec_uid != '' THEN sec_uid END) AS commenter_count,
                    MAX(comment_timestamp) AS latest_comment_timestamp,
                    MIN(comment_timestamp) AS earliest_comment_timestamp,
                    MAX(crawled_at) AS latest_crawled_at
                FROM crawled_comments
                {where_clause}
                GROUP BY aweme_id
                ORDER BY latest_crawled_at DESC, latest_comment_timestamp DESC, aweme_id DESC
                LIMIT ? OFFSET ?
                """,
                params + [safe_page_size, offset],
            ).fetchall()
        finally:
            conn.close()

        items = []
        for row in rows:
            items.append({
                "aweme_id": row["aweme_id"],
                "video_url": row["video_url"] or "",
                "video_name": row["video_name"] or "",
                "author_name": row["author_name"] or "",
                "total_comments": int(row["total_comments"] or 0),
                "reply_comments": int(row["reply_comments"] or 0),
                "top_level_comments": int(row["top_level_comments"] or 0),
                "commenter_count": int(row["commenter_count"] or 0),
                "latest_comment_timestamp": int(row["latest_comment_timestamp"] or 0),
                "earliest_comment_timestamp": int(row["earliest_comment_timestamp"] or 0),
                "latest_crawled_at": row["latest_crawled_at"] or "",
            })

        return {
            "items": items,
            "total": int(total or 0),
            "page": safe_page,
            "page_size": safe_page_size,
            "total_pages": (int(total or 0) + safe_page_size - 1) // safe_page_size if safe_page_size else 1,
        }

    def get_crawled_comment_video_detail(
        self,
        aweme_id: str,
        platform: Optional[str] = None,
        limit: int = 50,
    ) -> dict:
        aweme_id = str(aweme_id or "").strip()
        if not aweme_id:
            return {"summary": None, "comments": []}

        self._init_customer_sqlite()
        safe_limit = min(max(int(limit or 50), 1), 200)
        clauses = ["aweme_id = ?"]
        params: List[Any] = [aweme_id]
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        where_clause = f"WHERE {' AND '.join(clauses)}"
        conn = self._get_customer_sqlite_conn()
        try:
            summary_row = conn.execute(
                f"""
                SELECT
                    aweme_id,
                    MAX(video_url) AS video_url,
                    COUNT(*) AS total_comments,
                    SUM(CASE WHEN comment_level > 1 THEN 1 ELSE 0 END) AS reply_comments,
                    SUM(CASE WHEN comment_level <= 1 THEN 1 ELSE 0 END) AS top_level_comments,
                    COUNT(DISTINCT CASE WHEN sec_uid != '' THEN sec_uid END) AS commenter_count,
                    MAX(comment_timestamp) AS latest_comment_timestamp,
                    MIN(comment_timestamp) AS earliest_comment_timestamp,
                    MAX(crawled_at) AS latest_crawled_at
                FROM crawled_comments
                {where_clause}
                GROUP BY aweme_id
                """,
                params,
            ).fetchone()
            detail_rows = conn.execute(
                f"""
                SELECT
                    comment_id,
                    sec_uid,
                    video_url,
                    comment_content,
                    comment_timestamp,
                    comment_level,
                    crawled_at
                FROM crawled_comments
                {where_clause}
                ORDER BY comment_timestamp DESC, crawled_at DESC, comment_id DESC
                LIMIT ?
                """,
                params + [safe_limit],
            ).fetchall()
        finally:
            conn.close()

        summary = None
        if summary_row:
            summary = {
                "aweme_id": summary_row["aweme_id"],
                "video_url": summary_row["video_url"] or "",
                "total_comments": int(summary_row["total_comments"] or 0),
                "reply_comments": int(summary_row["reply_comments"] or 0),
                "top_level_comments": int(summary_row["top_level_comments"] or 0),
                "commenter_count": int(summary_row["commenter_count"] or 0),
                "latest_comment_timestamp": int(summary_row["latest_comment_timestamp"] or 0),
                "earliest_comment_timestamp": int(summary_row["earliest_comment_timestamp"] or 0),
                "latest_crawled_at": summary_row["latest_crawled_at"] or "",
            }

        comments = []
        for row in detail_rows:
            comments.append({
                "comment_id": row["comment_id"],
                "sec_uid": row["sec_uid"] or "",
                "video_url": row["video_url"] or "",
                "comment_content": row["comment_content"] or "",
                "comment_timestamp": int(row["comment_timestamp"] or 0),
                "comment_level": int(row["comment_level"] or 1),
                "crawled_at": row["crawled_at"] or "",
            })

        return {"summary": summary, "comments": comments}

    def _get_customers_page_from_sqlite(
        self,
        page: int,
        page_size: int,
        platform: Optional[str] = None,
        status: Optional[str] = None,
        interact_status: Optional[str] = None,
        intent_level: Optional[str] = None,
        keyword: Optional[str] = None,
        comment_time_start: Optional[str] = None,
        comment_time_end: Optional[str] = None,
        created_after: Optional[str] = None,
        created_by_task_id: Optional[str] = None,
    ) -> dict:
        self._init_customer_sqlite()
        where_clause, params = self._build_customer_query_filters(
            platform,
            status,
            interact_status,
            intent_level,
            keyword,
            comment_time_start,
            comment_time_end,
            created_after,
            created_by_task_id,
        )
        offset = max(page - 1, 0) * page_size
        conn = self._get_customer_sqlite_conn()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) AS total FROM customers {where_clause}",
                params
            ).fetchone()["total"]
            rows = conn.execute(
                f"""
                SELECT sec_uid, platform, nickname, unique_id, profile_url, source_video_url, first_source_video_url,
                       comment_content, comment_time, status, interact_status, intent_level, intent_score,
                       tags, created_at, updated_at, ip_location, signature, avatar_url, video_title, author_name,
                       first_crawl_task_id
                FROM customers
                {where_clause}
                ORDER BY created_at DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                params + [page_size, offset]
            ).fetchall()
            summary_row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
                    SUM(CASE WHEN status='replied' THEN 1 ELSE 0 END) AS replied,
                    SUM(CASE WHEN interact_status='interacted' THEN 1 ELSE 0 END) AS interacted,
                    SUM(CASE WHEN intent_level='A' THEN 1 ELSE 0 END) AS intent_a,
                    SUM(CASE WHEN intent_level='B' THEN 1 ELSE 0 END) AS intent_b,
                    SUM(CASE WHEN intent_level='C' THEN 1 ELSE 0 END) AS intent_c,
                    SUM(CASE WHEN intent_level='D' THEN 1 ELSE 0 END) AS intent_d,
                    SUM(CASE WHEN intent_level='E' THEN 1 ELSE 0 END) AS intent_e
                FROM customers
                {where_clause}
                """,
                params
            ).fetchone()
        finally:
            conn.close()

        total_pages = (total + page_size - 1) // page_size if page_size else 1
        return {
            "items": [self._customer_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "summary": {
                "total": summary_row["total"] or 0,
                "pending": summary_row["pending"] or 0,
                "sent": summary_row["sent"] or 0,
                "replied": summary_row["replied"] or 0,
                "interacted": summary_row["interacted"] or 0,
                "intent_distribution": {
                    "A": summary_row["intent_a"] or 0,
                    "B": summary_row["intent_b"] or 0,
                    "C": summary_row["intent_c"] or 0,
                    "D": summary_row["intent_d"] or 0,
                    "E": summary_row["intent_e"] or 0,
                }
            }
        }

    def _read_data_snapshot_unlocked(self) -> dict:
        """读取主快照文件（不含增量日志）。"""
        try:
            with open(self.db_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {"customers": data, "platforms": {}, "send_modes": {}}
                if not isinstance(data, dict):
                    return self._default_customer_data()
                data.setdefault("customers", [])
                data.setdefault("platforms", {})
                data.setdefault("send_modes", {})
                return data
        except FileNotFoundError:
            return self._default_customer_data()
        except json.JSONDecodeError as e:
            logger.critical(f"数据库文件损坏(JSON解析失败): {e}，请手动检查 {self.db_path}")
            raise RuntimeError(f"数据库文件损坏，拒绝用空数据覆盖: {e}")
        except Exception as e:
            logger.critical(f"读取数据库失败(非文件缺失): {e}，拒绝用空数据覆盖")
            raise RuntimeError(f"数据库读取失败，拒绝用空数据覆盖: {e}")

    def _apply_customer_journal_unlocked(self, data: dict) -> dict:
        """合并客户增量日志，兼容现有 `customers.json` 读取方。"""
        journal_path = self.get_customer_journal_path()
        if not os.path.exists(journal_path):
            return data

        customers = data.setdefault("customers", [])
        existing_keys = {
            (item.get("platform", "douyin"), item.get("sec_uid"))
            for item in customers
            if item.get("sec_uid")
        }

        try:
            with open(journal_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.warning(f"客户增量日志存在损坏记录，已跳过: {e}")
                        continue

                    customer = entry.get("customer")
                    if entry.get("op") != "upsert_customer" or not isinstance(customer, dict):
                        continue

                    sec_uid = customer.get("sec_uid")
                    platform = customer.get("platform", "douyin")
                    if not sec_uid:
                        continue

                    key = (platform, sec_uid)
                    if key in existing_keys:
                        for idx, existing_customer in enumerate(customers):
                            if (
                                existing_customer.get("platform", "douyin") == platform
                                and existing_customer.get("sec_uid") == sec_uid
                            ):
                                customers[idx] = customer
                                break
                        continue

                    customers.append(customer)
                    existing_keys.add(key)
        except Exception as e:
            logger.error(f"读取客户增量日志失败: {e}")
            raise RuntimeError(f"读取客户增量日志失败: {e}") from e

        return data

    def _append_customer_journal_entries_unlocked(self, customers: List[dict]):
        """批量追加客户增量日志并强制刷盘，减少异常退出时的数据丢失。"""
        if not customers:
            return

        journal_path = self.get_customer_journal_path()
        dir_path = os.path.dirname(journal_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path)

        with open(journal_path, 'a', encoding='utf-8') as f:
            for customer in customers:
                entry = {"op": "upsert_customer", "customer": customer}
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _should_compact_customer_storage_unlocked(self) -> bool:
        """控制 journal 大小，避免查询时长期回放过多增量记录。"""
        journal_path = self.get_customer_journal_path()
        if not os.path.exists(journal_path):
            return False

        try:
            return os.path.getsize(journal_path) >= self.CUSTOMER_JOURNAL_COMPACT_BYTES
        except OSError as e:
            logger.debug(f"获取客户增量日志大小失败: {e}")
            return False

    def _clear_customer_journal_unlocked(self):
        """主快照落盘后清理已合并的增量日志。"""
        journal_path = self.get_customer_journal_path()
        if os.path.exists(journal_path):
            os.remove(journal_path)
    
    def _load_data(self) -> dict:
        """读取数据（加锁）"""
        with DatabaseManager._lock:
            return self._load_data_unlocked()
    
    def _load_data_unlocked(self) -> dict:
        """读取数据（不加锁，调用方需自行加锁）"""
        current_time = time.time()
        if DatabaseManager._customer_cache is not None and \
           current_time - DatabaseManager._customer_cache_time < DatabaseManager._customer_cache_ttl:
            return DatabaseManager._customer_cache

        data = self._read_data_snapshot_unlocked()
        data = self._apply_customer_journal_unlocked(data)
        DatabaseManager._customer_cache = data
        DatabaseManager._customer_cache_time = current_time
        return data
    
    def _save_data(self, data: dict):
        """保存数据（加锁，原子写入）"""
        with DatabaseManager._lock:
            self._save_data_unlocked(data)
    
    def _save_data_unlocked(self, data: dict):
        """保存数据（不加锁，调用方需自行加锁，原子写入）"""
        try:
            import tempfile
            import os
            dir_path = os.path.dirname(self.db_path) or '.'
            fd, tmp_path = tempfile.mkstemp(suffix='.tmp', dir=dir_path)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.db_path)
                self._clear_customer_journal_unlocked()
                DatabaseManager._customer_cache = data
                DatabaseManager._customer_cache_time = time.time()
            except Exception:
                try:
                    os.unlink(tmp_path)
                except Exception as e:
                    logger.debug(f"清理临时文件失败: {e}")
                raise
        except Exception as e:
            logger.error(f"保存数据库失败: {e}")
    
    def add_customer(self, customer_data: dict):
        """添加单个潜在客户。"""
        return self.add_customers_batch([customer_data])

    def _build_customer_record(self, customer_data: dict) -> Optional[dict]:
        """标准化客户数据，复用单条/批量写入逻辑。"""
        sec_uid = customer_data.get('sec_uid')
        platform = customer_data.get('platform', 'douyin')

        if not sec_uid:
            logger.warning("尝试添加无效客户(无sec_uid)")
            return None

        c_time = self._comment_time_to_string(customer_data.get('comment_time', ''))

        now = datetime.now().isoformat()
        return {
            "sec_uid": sec_uid,
            "nickname": customer_data.get('nickname', ''),
            "unique_id": customer_data.get('unique_id', ''),
            "profile_url": customer_data.get('profile_url', ''),
            "source_video_url": customer_data.get('source_video_url', ''),
            "first_source_video_url": customer_data.get('first_source_video_url', '') or customer_data.get('source_video_url', ''),
            "comment_content": customer_data.get('comment_content', ''),
            "comment_time": str(c_time),
            "platform": platform,
            "status": "pending",
            "interact_status": "pending",
            "intent_level": "D",
            "intent_score": 0,
            "tags": [],
            "created_at": now,
            "updated_at": now,
            "ip_location": customer_data.get("ip_location", ""),
            "signature": customer_data.get("signature", ""),
            "avatar_url": customer_data.get("avatar_url", ""),
            "video_title": customer_data.get("video_title", ""),
            "author_name": customer_data.get("author_name", ""),
            "first_crawl_task_id": str(customer_data.get("first_crawl_task_id", "") or "").strip(),
        }

    def add_customers_batch(self, customers_data: List[dict]) -> int:
        """
        批量添加潜在客户。

        爬取场景下优先走增量日志，避免每条评论都整文件重写。
        """
        if not customers_data:
            return 0

        with DatabaseManager._lock:
            data = self._load_data_unlocked()
            customers = data.setdefault("customers", [])
            for idx, customer in enumerate(list(customers)):
                customers[idx] = self._normalize_customer_status_fields(customer)
            existing_map = {
                (item.get('platform', 'douyin'), item.get('sec_uid')): item
                for item in customers
                if item.get("sec_uid")
            }
            added_count = 0
            upsert_customers = []

            for customer_data in customers_data:
                new_customer = self._build_customer_record(customer_data)
                if not new_customer:
                    continue

                customer_key = (new_customer["platform"], new_customer["sec_uid"])
                existing_customer = existing_map.get(customer_key)
                if existing_customer:
                    merged_customer = self._merge_customer_record(existing_customer, new_customer)
                    if merged_customer != existing_customer:
                        existing_customer.clear()
                        existing_customer.update(merged_customer)
                        upsert_customers.append(copy.deepcopy(merged_customer))
                    continue

                customers.append(new_customer)
                existing_map[customer_key] = new_customer
                upsert_customers.append(new_customer)
                added_count += 1

            if upsert_customers:
                self._append_customer_journal_entries_unlocked(upsert_customers)
                DatabaseManager._customer_cache = data
                DatabaseManager._customer_cache_time = time.time()
                try:
                    self._upsert_customers_sqlite(upsert_customers)
                except Exception as e:
                    logger.warning(f"客户 SQLite 增量写入失败，已保留 JSON/journal 数据: {e}")
                if self._should_compact_customer_storage_unlocked():
                    self._save_data_unlocked(data)
                logger.info(f"批量新增客户 {added_count} 条，同步更新客户 {max(len(upsert_customers) - added_count, 0)} 条")

            return added_count
    
    def get_pending_customers(self, platform: str = None, limit: int = 10) -> List[dict]:
        """获取待发送私信的客户"""
        try:
            page = self._get_customers_page_from_sqlite(
                page=1,
                page_size=max(limit, 1),
                platform=platform,
                status="pending"
            )
            return page["items"]
        except Exception as e:
            logger.warning(f"从 SQLite 读取待发送客户失败，回退 JSON: {e}")
            data = self._load_data()
            pending = [
                self._normalize_customer_status_fields(c)
                for c in data.get("customers", [])
                if self._normalize_customer_status_fields(c).get('status') == 'pending'
            ]
            
            if platform:
                pending = [c for c in pending if c.get('platform', 'douyin') == platform]
            
            return copy.deepcopy(pending[:limit])

    def get_all_customers(self, platform: str = None) -> List[dict]:
        """获取所有客户"""
        try:
            page_size = 1000000
            return self._get_customers_page_from_sqlite(
                page=1,
                page_size=page_size,
                platform=platform
            )["items"]
        except Exception as e:
            logger.warning(f"从 SQLite 读取客户列表失败，回退 JSON: {e}")
            data = self._load_data()
            customers = [self._normalize_customer_status_fields(c) for c in data.get("customers", [])]

            if platform:
                customers = [c for c in customers if c.get('platform') == platform]

            return copy.deepcopy(customers)

    def get_customers_page(
        self,
        page: int = 1,
        page_size: int = 20,
        platform: str = None,
        status: str = None,
        interact_status: str = None,
        intent_level: str = None,
        comment_time_start: str = None,
        comment_time_end: str = None,
        created_after: str = None,
        created_by_task_id: str = None,
    ) -> dict:
        """分页获取客户列表，优先使用 SQLite。"""
        safe_page = max(int(page or 1), 1)
        safe_page_size = min(max(int(page_size or 20), 1), 500)
        try:
            return self._get_customers_page_from_sqlite(
                page=safe_page,
                page_size=safe_page_size,
                platform=platform,
                status=status,
                interact_status=interact_status,
                intent_level=intent_level,
                comment_time_start=comment_time_start,
                comment_time_end=comment_time_end,
                created_after=created_after,
                created_by_task_id=created_by_task_id,
            )
        except Exception as e:
            logger.warning(f"客户分页查询回退 JSON: {e}")
            data = self._load_data()
            customers = [self._normalize_customer_status_fields(c) for c in data.get("customers", [])]
            if platform:
                customers = [c for c in customers if c.get('platform') == platform]
            if status:
                customers = [c for c in customers if c.get('status') == status]
            if interact_status:
                customers = [c for c in customers if c.get('interact_status') == interact_status]
            if intent_level:
                customers = [c for c in customers if c.get('intent_level') == intent_level]
            comment_time_start_ts = self._parse_customer_comment_timestamp(comment_time_start)
            if comment_time_start_ts:
                customers = [
                    c for c in customers
                    if self._parse_customer_comment_timestamp(c.get('comment_time', '')) >= comment_time_start_ts
                ]
            comment_time_end_ts = self._parse_customer_comment_timestamp(comment_time_end)
            if comment_time_end_ts:
                customers = [
                    c for c in customers
                    if self._parse_customer_comment_timestamp(c.get('comment_time', '')) <= comment_time_end_ts
                ]
            if created_after:
                try:
                    clean_created_after = str(created_after).replace('Z', '')
                    customers = [
                        c for c in customers
                        if c.get('created_at')
                        and str(c.get('created_at')).replace('Z', '') >= clean_created_after
                    ]
                except Exception as e:
                    logger.warning(f"Filter created_after failed: {e}")
            if created_by_task_id:
                filter_task_id = str(created_by_task_id).strip()
                customers = [
                    c for c in customers
                    if str(c.get('first_crawl_task_id', '') or '').strip() == filter_task_id
                ]

            customers = list(reversed(customers))
            total = len(customers)
            offset = (safe_page - 1) * safe_page_size
            items = customers[offset:offset + safe_page_size]
            return {
                "items": copy.deepcopy(items),
                "total": total,
                "page": safe_page,
                "page_size": safe_page_size,
                "total_pages": (total + safe_page_size - 1) // safe_page_size if safe_page_size else 1,
                "summary": {
                    "total": total,
                    "pending": len([c for c in customers if c.get('status') == 'pending']),
                    "sent": len([c for c in customers if c.get('status') == 'sent']),
                    "replied": len([c for c in customers if c.get('status') == 'replied']),
                    "interacted": len([c for c in customers if c.get('interact_status') == 'interacted']),
                    "intent_distribution": {
                        level: len([c for c in customers if c.get('intent_level') == level])
                        for level in ['A', 'B', 'C', 'D', 'E']
                    }
                }
            }

    def search_customers_page(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        platform: str = None,
        status: str = None,
        interact_status: str = None,
        comment_time_start: str = None,
        comment_time_end: str = None,
        created_after: str = None,
        created_by_task_id: str = None,
    ) -> dict:
        """分页搜索客户。"""
        keyword_value = (keyword or "").strip()
        if not keyword_value:
            return {
                "items": [],
                "total": 0,
                "page": max(int(page or 1), 1),
                "page_size": min(max(int(page_size or 20), 1), 500),
                "total_pages": 0,
                "summary": {
                    "total": 0,
                    "pending": 0,
                    "sent": 0,
                    "replied": 0,
                    "interacted": 0,
                    "intent_distribution": {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}
                }
            }

        safe_page = max(int(page or 1), 1)
        safe_page_size = min(max(int(page_size or 20), 1), 500)
        try:
            return self._get_customers_page_from_sqlite(
                page=safe_page,
                page_size=safe_page_size,
                platform=platform,
                status=status,
                interact_status=interact_status,
                keyword=keyword_value,
                comment_time_start=comment_time_start,
                comment_time_end=comment_time_end,
                created_after=created_after,
                created_by_task_id=created_by_task_id,
            )
        except Exception as e:
            logger.warning(f"客户搜索分页回退 JSON: {e}")
            results = self.search_customers(keyword_value, platform=platform)
            results = [self._normalize_customer_status_fields(c) for c in results]
            if status:
                results = [c for c in results if c.get('status') == status]
            if interact_status:
                results = [c for c in results if c.get('interact_status') == interact_status]
            comment_time_start_ts = self._parse_customer_comment_timestamp(comment_time_start)
            if comment_time_start_ts:
                results = [
                    c for c in results
                    if self._parse_customer_comment_timestamp(c.get('comment_time', '')) >= comment_time_start_ts
                ]
            comment_time_end_ts = self._parse_customer_comment_timestamp(comment_time_end)
            if comment_time_end_ts:
                results = [
                    c for c in results
                    if self._parse_customer_comment_timestamp(c.get('comment_time', '')) <= comment_time_end_ts
                ]
            if created_after:
                try:
                    clean_created_after = str(created_after).replace('Z', '')
                    results = [
                        c for c in results
                        if c.get('created_at')
                        and str(c.get('created_at')).replace('Z', '') >= clean_created_after
                    ]
                except Exception as e:
                    logger.warning(f"Filter created_after failed: {e}")
            if created_by_task_id:
                filter_task_id = str(created_by_task_id).strip()
                results = [
                    c for c in results
                    if str(c.get('first_crawl_task_id', '') or '').strip() == filter_task_id
                ]
            results = list(reversed(results))
            total = len(results)
            offset = (safe_page - 1) * safe_page_size
            items = results[offset:offset + safe_page_size]
            return {
                "items": copy.deepcopy(items),
                "total": total,
                "page": safe_page,
                "page_size": safe_page_size,
                "total_pages": (total + safe_page_size - 1) // safe_page_size if safe_page_size else 1,
                "summary": {
                    "total": total,
                    "pending": len([c for c in results if c.get('status') == 'pending']),
                    "sent": len([c for c in results if c.get('status') == 'sent']),
                    "replied": len([c for c in results if c.get('status') == 'replied']),
                    "interacted": len([c for c in results if c.get('interact_status') == 'interacted']),
                    "intent_distribution": {
                        level: len([c for c in results if c.get('intent_level') == level])
                        for level in ['A', 'B', 'C', 'D', 'E']
                    }
                }
            }

    def get_customer_overview(self, platform: str = None) -> dict:
        """获取客户总览统计，优先使用 SQLite 聚合。"""
        try:
            page = self._get_customers_page_from_sqlite(
                page=1,
                page_size=1,
                platform=platform
            )
            return page["summary"]
        except Exception as e:
            logger.warning(f"客户总览统计回退 JSON: {e}")
            data = self._load_data()
            customers = [
                self._normalize_customer_status_fields(c)
                for c in data.get("customers", [])
            ]
            if platform:
                customers = [c for c in customers if c.get('platform') == platform]
            return {
                "total": len(customers),
                "pending": len([c for c in customers if c.get('status') == 'pending']),
                "sent": len([c for c in customers if c.get('status') == 'sent']),
                "replied": len([c for c in customers if c.get('status') == 'replied']),
                "interacted": len([c for c in customers if c.get('interact_status') == 'interacted']),
                "intent_distribution": {
                    level: len([c for c in customers if c.get('intent_level') == level])
                    for level in ['A', 'B', 'C', 'D', 'E']
                }
            }

    def get_top_customers_by_intent(self, limit: int = 10, platform: str = None) -> List[dict]:
        """按意向分数获取客户 Top N，优先使用 SQLite。"""
        safe_limit = min(max(int(limit or 10), 1), 200)
        try:
            self._init_customer_sqlite()
            params: List[Any] = []
            where_clause = ""
            if platform:
                where_clause = "WHERE platform = ?"
                params.append(platform)
            conn = self._get_customer_sqlite_conn()
            try:
                rows = conn.execute(
                    f"""
                    SELECT sec_uid, platform, nickname, unique_id, profile_url, source_video_url, first_source_video_url,
                           comment_content, comment_time, status, interact_status, intent_level, intent_score,
                           tags, created_at, updated_at, ip_location, signature, avatar_url
                    FROM customers
                    {where_clause}
                    ORDER BY intent_score DESC, updated_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    params + [safe_limit]
                ).fetchall()
                return [self._customer_row_to_dict(row) for row in rows]
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Top 客户查询回退 JSON: {e}")
            customers = self.get_all_customers(platform=platform)
            customers.sort(key=lambda item: item.get("intent_score", 0), reverse=True)
            return customers[:safe_limit]
    
    def get_customer(self, customer_id: str) -> Optional[dict]:
        """
        根据ID获取单个客户
        
        支持多种ID格式：
        - sec_uid (主要标识)
        - id
        - customerId
        - nickname
        """
        by_id = self.get_user_by_id(customer_id)
        if by_id:
            return by_id

        try:
            self._init_customer_sqlite()
            conn = self._get_customer_sqlite_conn()
            try:
                row = conn.execute(
                    """
                    SELECT * FROM customers
                    WHERE nickname = ? OR unique_id = ?
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    [customer_id, customer_id]
                ).fetchone()
                return self._customer_row_to_dict(row) if row else None
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"获取单个客户回退 JSON: {e}")
            customers = self.get_all_customers()
            for customer in customers:
                if (customer.get("id") == customer_id or 
                    customer.get("customerId") == customer_id or
                    customer.get("nickname") == customer_id):
                    return copy.deepcopy(customer)
            return None
    
    def get_customer_messages(self, customer_id: str, limit: int = 100) -> List[dict]:
        """
        获取客户消息历史
        
        支持多种匹配方式：
        - customer_id
        - sec_uid
        - customerId
        - customer_name (会话中的客户名称)
        - conversation_id (会话ID)
        """
        messages = []
        all_messages = self.get_all_messages()
        
        customer = self.get_customer(customer_id)
        customer_nickname = customer.get("nickname", "") if customer else ""
        
        possible_ids = [customer_id]
        if customer_nickname:
            possible_ids.append(customer_nickname)
            possible_ids.append(f"douyin_{customer_nickname}")
            possible_ids.append(f"douyin_{customer_nickname}_{hashlib.md5(customer_nickname.encode('utf-8')).hexdigest()[:8]}")
        
        for msg in all_messages:
            msg_customer_id = (
                msg.get("customer_id") or 
                msg.get("sec_uid") or 
                msg.get("customerId") or
                msg.get("sender_name") or
                ""
            )
            msg_conversation_id = msg.get("conversation_id", "")
            
            for pid in possible_ids:
                if msg_customer_id == pid or msg_conversation_id == pid or msg_conversation_id.endswith(f"_{pid}"):
                    messages.append(msg)
                    break
        
        return messages[:limit]

    def get_customer_by_nickname(self, nickname: str, platform: str = None) -> Optional[dict]:
        """根据昵称查找客户"""
        try:
            self._init_customer_sqlite()
            conn = self._get_customer_sqlite_conn()
            try:
                if platform:
                    row = conn.execute(
                        """
                        SELECT * FROM customers
                        WHERE platform = ? AND (nickname = ? OR unique_id = ?)
                        ORDER BY updated_at DESC, rowid DESC
                        LIMIT 1
                        """,
                        [platform, nickname, nickname]
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT * FROM customers
                        WHERE nickname = ? OR unique_id = ?
                        ORDER BY updated_at DESC, rowid DESC
                        LIMIT 1
                        """,
                        [nickname, nickname]
                    ).fetchone()
                return self._customer_row_to_dict(row) if row else None
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"根据昵称查询客户回退 JSON: {e}")
            customers = self.get_all_customers(platform)
            for customer in customers:
                if customer.get('nickname') == nickname:
                    return customer
                if customer.get('unique_id') == nickname:
                    return copy.deepcopy(customer)
            return None

    def update_customer_status(self, sec_uid: str, platform: str, status: str):
        """更新客户状态"""
        with DatabaseManager._lock:
            data = self._load_data_unlocked()
            
            for item in data.get("customers", []):
                if item.get('sec_uid') == sec_uid and item.get('platform') == platform:
                    normalized = self._normalize_customer_status_fields(item)
                    item.clear()
                    item.update(normalized)
                    item['status'] = str(status or 'pending').strip().lower() or 'pending'
                    item['updated_at'] = datetime.now().isoformat()
                    break
            
            self._save_data_unlocked(data)
            try:
                self._upsert_customers_sqlite([
                    item for item in data.get("customers", [])
                    if item.get('sec_uid') == sec_uid and item.get('platform') == platform
                ])
            except Exception as e:
                logger.warning(f"同步客户状态到 SQLite 失败: {e}")

    def update_customer_interact_status(self, sec_uid: str, platform: str, interact_status: str):
        """更新客户互动状态"""
        with DatabaseManager._lock:
            data = self._load_data_unlocked()

            for item in data.get("customers", []):
                if item.get('sec_uid') == sec_uid and item.get('platform') == platform:
                    normalized = self._normalize_customer_status_fields(item)
                    item.clear()
                    item.update(normalized)
                    item['interact_status'] = str(interact_status or 'pending').strip().lower() or 'pending'
                    item['updated_at'] = datetime.now().isoformat()
                    break

            self._save_data_unlocked(data)
            try:
                self._upsert_customers_sqlite([
                    item for item in data.get("customers", [])
                    if item.get('sec_uid') == sec_uid and item.get('platform') == platform
                ])
            except Exception as e:
                logger.warning(f"同步客户互动状态到 SQLite 失败: {e}")
    
    def update_customer_intent(self, sec_uid: str, platform: str, intent_level: str, intent_score: float):
        """更新客户意向等级"""
        with DatabaseManager._lock:
            data = self._load_data_unlocked()
            
            for item in data.get("customers", []):
                if item.get('sec_uid') == sec_uid and item.get('platform') == platform:
                    item['intent_level'] = intent_level
                    item['intent_score'] = intent_score
                    item['updated_at'] = datetime.now().isoformat()
                    break
            
            self._save_data_unlocked(data)
            try:
                self._upsert_customers_sqlite([
                    item for item in data.get("customers", [])
                    if item.get('sec_uid') == sec_uid and item.get('platform') == platform
                ])
            except Exception as e:
                logger.warning(f"同步客户意向到 SQLite 失败: {e}")
    
    def add_customer_tag(self, sec_uid: str, platform: str, tag: str):
        """为客户添加标签"""
        with DatabaseManager._lock:
            data = self._load_data_unlocked()
            
            for item in data.get("customers", []):
                if item.get('sec_uid') == sec_uid and item.get('platform') == platform:
                    tags = item.get('tags', [])
                    if tag not in tags:
                        tags.append(tag)
                        item['tags'] = tags
                        item['updated_at'] = datetime.now().isoformat()
                    break
            
            self._save_data_unlocked(data)
            try:
                self._upsert_customers_sqlite([
                    item for item in data.get("customers", [])
                    if item.get('sec_uid') == sec_uid and item.get('platform') == platform
                ])
            except Exception as e:
                logger.warning(f"同步客户标签到 SQLite 失败: {e}")

    def update_customer_tags(self, sec_uid: str, platform: str, tags: list):
        """整体更新客户标签"""
        normalized_tags = []
        for tag in tags or []:
            tag_text = str(tag).strip()
            if tag_text and tag_text not in normalized_tags:
                normalized_tags.append(tag_text)

        with DatabaseManager._lock:
            data = self._load_data_unlocked()

            for item in data.get("customers", []):
                if item.get('sec_uid') == sec_uid and item.get('platform') == platform:
                    item['tags'] = normalized_tags
                    item['updated_at'] = datetime.now().isoformat()
                    break

            self._save_data_unlocked(data)
            try:
                self._upsert_customers_sqlite([
                    item for item in data.get("customers", [])
                    if item.get('sec_uid') == sec_uid and item.get('platform') == platform
                ])
            except Exception as e:
                logger.warning(f"同步客户标签到 SQLite 失败: {e}")
    
    def get_user_by_id(self, sec_uid: str, platform: str = None) -> Optional[dict]:
        """根据ID获取用户信息"""
        try:
            self._init_customer_sqlite()
            conn = self._get_customer_sqlite_conn()
            try:
                if platform:
                    row = conn.execute(
                        "SELECT * FROM customers WHERE sec_uid = ? AND platform = ? LIMIT 1",
                        [sec_uid, platform]
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM customers WHERE sec_uid = ? LIMIT 1",
                        [sec_uid]
                    ).fetchone()
                return self._customer_row_to_dict(row) if row else None
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"根据ID查询客户回退 JSON: {e}")
            data = self._load_data()
            
            for item in data.get("customers", []):
                if platform:
                    if item.get('sec_uid') == sec_uid and item.get('platform') == platform:
                        return self._normalize_customer_status_fields(item)
                else:
                    if item.get('sec_uid') == sec_uid:
                        return self._normalize_customer_status_fields(item)
            
            return None
    
    def get_platform_stats(self) -> dict:
        """获取各平台统计数据"""
        data = self._load_data()
        customers = [self._normalize_customer_status_fields(c) for c in data.get("customers", [])]
        
        stats = {}
        for customer in customers:
            platform = customer.get('platform', 'unknown')
            if platform not in stats:
                stats[platform] = {
                    "total": 0,
                    "pending": 0,
                    "sent": 0,
                    "replied": 0,
                    "interacted": 0,
                }
            
            stats[platform]["total"] += 1
            status = customer.get('status', 'pending')
            if status == 'pending':
                stats[platform]["pending"] += 1
            elif status == 'sent':
                stats[platform]["sent"] += 1
            elif status == 'replied':
                stats[platform]["replied"] += 1
            if customer.get('interact_status') == 'interacted':
                stats[platform]["interacted"] += 1
        
        return stats
    
    def search_customers(self, keyword: str, platform: str = None) -> List[dict]:
        """搜索客户"""
        keyword_lower = (keyword or "").strip().lower()
        if not keyword_lower:
            return []

        try:
            page = self._get_customers_page_from_sqlite(
                page=1,
                page_size=1000000,
                platform=platform,
                keyword=keyword_lower
            )
            return page["items"]
        except Exception as e:
            logger.warning(f"搜索客户回退 JSON: {e}")
            data = self._load_data()
            customers = [self._normalize_customer_status_fields(c) for c in data.get("customers", [])]
            
            results = []
            for customer in customers:
                if platform and customer.get('platform') != platform:
                    continue
                
                if (keyword_lower in customer.get('nickname', '').lower() or
                    keyword_lower in customer.get('unique_id', '').lower() or
                    keyword_lower in customer.get('comment_content', '').lower()):
                    results.append(customer)
            
            return copy.deepcopy(results)
    
    # ========== 消息存储相关方法 ==========
    
    def get_messages_path(self) -> str:
        """获取消息存储文件路径"""
        dir_path = os.path.dirname(self.db_path)
        return os.path.join(dir_path, "messages.json")

    @classmethod
    def _looks_like_assistant_outbound_message(cls, content: str, sender_name: str = "") -> bool:
        text = str(content or "").strip()
        sender = str(sender_name or "").strip()
        if not text:
            return False
        if sender in {"我", "我发送的", "self", "assistant", "bot"}:
            return True
        if any(marker in text for marker in cls.MISCLASSIFIED_ASSISTANT_PATTERNS):
            return True
        if text.startswith(("亲爱的", "您好", "您好！")) and any(
            marker in text for marker in ("服务", "资料", "联系方式", "客服", "转账", "远程协助")
        ):
            return True
        if "哈喽~ 很高兴见到您！" in text or "请问有什么需要帮助的吗" in text:
            return True
        return False

    @classmethod
    def _normalize_message_direction_fields(cls, message_data: dict) -> dict:
        normalized = dict(message_data or {})
        direction = str(normalized.get("direction", "inbound") or "inbound").strip().lower()
        content = normalized.get("content", "")
        sender_name = normalized.get("sender_name", "")
        sender_id = normalized.get("sender_id", "")

        if direction != "outbound" and cls._looks_like_assistant_outbound_message(content, sender_name):
            normalized["direction"] = "outbound"
            if not sender_name:
                normalized["sender_name"] = "我"
            if not sender_id:
                normalized["sender_id"] = "self"
            return normalized

        if direction == "outbound":
            normalized.setdefault("sender_name", sender_name or "我")
            normalized.setdefault("sender_id", sender_id or "self")

        return normalized
    
    def _load_messages_data(self) -> dict:
        """读取消息数据（加锁，带缓存，返回深拷贝防止缓存污染）"""
        import time
        import copy
        current_time = time.time()
        if DatabaseManager._messages_cache is not None and \
           current_time - DatabaseManager._messages_cache_time < DatabaseManager._messages_cache_ttl:
            return copy.deepcopy(DatabaseManager._messages_cache)
        with DatabaseManager._msg_lock:
            current_time = time.time()
            if DatabaseManager._messages_cache is not None and \
               current_time - DatabaseManager._messages_cache_time < DatabaseManager._messages_cache_ttl:
                return copy.deepcopy(DatabaseManager._messages_cache)
            data = self._load_messages_data_unlocked()
            DatabaseManager._messages_cache = data
            DatabaseManager._messages_cache_time = current_time
            return copy.deepcopy(data)
    
    def _load_messages_data_unlocked(self) -> dict:
        """读取消息数据（不加锁，调用方需自行加锁）"""
        msg_path = self.get_messages_path()
        if not os.path.exists(msg_path):
            return {
                "conversations": [],
                "messages": [],
                "reply_reservations": [],
                "processed_events": [],
                "outbox_events": [],
                "workflow_runs": [],
                "handoff_tickets": [],
            }
        try:
            with open(msg_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    return {
                        "conversations": [],
                        "messages": [],
                        "reply_reservations": [],
                        "processed_events": [],
                        "outbox_events": [],
                        "workflow_runs": [],
                        "handoff_tickets": [],
                    }
                data.setdefault("conversations", [])
                data.setdefault("messages", [])
                data.setdefault("reply_reservations", [])
                data.setdefault("processed_events", [])
                data.setdefault("outbox_events", [])
                data.setdefault("workflow_runs", [])
                data.setdefault("handoff_tickets", [])
                return data
        except json.JSONDecodeError as e:
            logger.error(f"消息数据文件损坏: {e}")
            raise RuntimeError(f"消息数据文件损坏，请手动修复: {msg_path}") from e
        except Exception as e:
            logger.error(f"读取消息数据失败: {e}")
            raise RuntimeError(f"读取消息数据失败: {e}") from e
    
    def _save_messages_data_unlocked(self, data: dict):
        """保存消息数据（不加锁，调用方需自行加锁，原子写入）"""
        msg_path = self.get_messages_path()
        try:
            import tempfile
            import os
            dir_path = os.path.dirname(msg_path) or '.'
            fd, tmp_path = tempfile.mkstemp(suffix='.tmp', dir=dir_path)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, msg_path)
                DatabaseManager._messages_cache = data
                DatabaseManager._messages_cache_time = __import__('time').time()
            except Exception:
                try:
                    os.unlink(tmp_path)
                except Exception as e:
                    logger.debug(f"清理临时文件失败: {e}")
                raise
        except Exception as e:
            logger.error(f"保存消息数据失败: {e}")
            raise

    def _persist_message_store_and_sidecar_unlocked(
        self,
        data: dict,
        *,
        messages: Optional[List[dict]] = None,
        conversations: Optional[List[dict]] = None,
        processed_events: Optional[List[dict]] = None,
        outbox_events: Optional[List[dict]] = None,
        workflow_runs: Optional[List[dict]] = None,
        handoff_tickets: Optional[List[dict]] = None,
    ) -> None:
        """先落 JSON 主链，再按需同步 SQLite 侧车。调用方需已持有 `_msg_lock`。"""
        self._save_messages_data_unlocked(data)
        for message in messages or []:
            if message:
                self._sync_message_to_sqlite(message)
        for conversation in conversations or []:
            if conversation:
                self._sync_conversation_to_sqlite(conversation)
        for event in processed_events or []:
            if event:
                self._sync_processed_event_to_sqlite(event)
        for event in outbox_events or []:
            if event:
                self._sync_outbox_event_to_sqlite(event)
        for run in workflow_runs or []:
            if run:
                self._sync_workflow_run_to_sqlite(run)
        for ticket in handoff_tickets or []:
            if ticket:
                self._sync_handoff_ticket_to_sqlite(ticket)

    @staticmethod
    def _is_reservation_message(message: dict) -> bool:
        """判断一条消息是否为历史 reservation 记录。"""
        msg_type = message.get("message_type", "")
        msg_id = message.get("message_id", "")
        return msg_type == "reservation" or (isinstance(msg_id, str) and msg_id.startswith("_reservation_"))

    def _get_business_messages(self, data: dict) -> list:
        """获取正式业务消息，自动过滤历史 reservation 污染数据。"""
        return [m for m in data.get("messages", []) if not self._is_reservation_message(m)]
    
    def save_message(self, message_data: dict):
        """
        保存消息 - 深度修复版
        
        关键修复：
        1. 添加消息去重逻辑，基于 message_id 和 content 判断
        2. 确保每条消息只保存一次
        3. 读-改-写在同一把锁内完成，保证原子性
        """
        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            message_data = self._normalize_message_direction_fields(message_data)
        
            message_id = message_data.get("message_id", "")
            logical_message_id = message_data.get("logical_message_id", "")
            source_message_id = message_data.get("source_message_id", "")
            content = message_data.get("content", "")
            conversation_id = message_data.get("conversation_id", "")
        
            for existing_msg in data["messages"]:
                if message_id and existing_msg.get("message_id") == message_id:
                    logger.debug(f"消息已存在(通过ID): {message_id}")
                    return

                if logical_message_id and existing_msg.get("logical_message_id") == logical_message_id:
                    logger.debug(f"消息已存在(通过逻辑ID): {logical_message_id}")
                    return
                
                if (existing_msg.get("conversation_id") == conversation_id and 
                    existing_msg.get("content") == content and
                    existing_msg.get("direction") == message_data.get("direction", "inbound")):
                    existing_time = existing_msg.get("created_at", "")
                    try:
                        from datetime import datetime as dt
                        time_diff = (dt.now() - dt.fromisoformat(existing_time)).total_seconds()
                        if time_diff < 2:
                            logger.debug(f"消息已存在(2秒内重复): {content[:30]}...")
                            return
                    except (ValueError, TypeError):
                        logger.debug(f"时间解析失败，允许写入新消息: {content[:30]}...")
        
            message = {
                "message_id": message_id,
                "logical_message_id": logical_message_id,
                "source_message_id": source_message_id,
                "conversation_id": conversation_id,
                "customer_id": message_data.get("customer_id", ""),
                "customer_name": message_data.get("customer_name", message_data.get("sender_name", "")),
                "platform": message_data.get("platform", "douyin"),
                "direction": message_data.get("direction", "inbound"),
                "message_type": message_data.get("message_type", "text"),
                "content": content,
                "sender_id": message_data.get("sender_id", ""),
                "sender_name": message_data.get("sender_name", ""),
                "is_read": message_data.get("is_read", False),
                "is_processed": message_data.get("is_processed", False),
                "ai_reply_content": message_data.get("ai_reply_content", ""),
                "created_at": message_data.get("created_at", datetime.now().isoformat()),
                "processed_status": message_data.get("processed_status", ""),
                "processed_at": message_data.get("processed_at", ""),
                "processed_reason": message_data.get("processed_reason", ""),
            }
        
            data["messages"].append(message)
            data["messages"].sort(key=lambda x: x.get("created_at", ""), reverse=False)
        
            self._persist_message_store_and_sidecar_unlocked(data, messages=[message])
            logger.info(f"保存消息: {content[:30]}... (会话: {conversation_id})")

    def sanitize_message_records(
        self,
        messages: Optional[List[dict]],
        *,
        rewrite_direction: bool = True,
    ) -> List[dict]:
        """清洗消息记录，统一纠正被误标成 inbound 的助手话术。"""
        sanitized: List[dict] = []
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            normalized = dict(msg)
            if rewrite_direction:
                normalized = self._normalize_message_direction_fields(normalized)
            sanitized.append(normalized)
        return sanitized

    def repair_misclassified_messages(self, conversation_id: str = "") -> dict:
        """修复历史消息中被误标为 inbound 的助手话术，并重建会话尾部摘要。"""
        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            updated_messages = 0
            touched_conversations = set()
            rewritten_messages = []
            rewritten_conversations = []

            for msg in data.get("messages", []):
                if conversation_id and msg.get("conversation_id") != conversation_id:
                    continue
                before_direction = str(msg.get("direction", "")).strip().lower()
                before_sender_name = msg.get("sender_name", "")
                before_sender_id = msg.get("sender_id", "")
                normalized = self._normalize_message_direction_fields(msg)
                if (
                    normalized.get("direction") != before_direction
                    or normalized.get("sender_name", "") != before_sender_name
                    or normalized.get("sender_id", "") != before_sender_id
                ):
                    msg.update(normalized)
                    rewritten_messages.append(msg)
                    updated_messages += 1
                    if msg.get("conversation_id"):
                        touched_conversations.add(msg.get("conversation_id"))

            if conversation_id:
                touched_conversations.add(conversation_id)

            if touched_conversations:
                for conv in data.get("conversations", []):
                    conv_id = conv.get("conversation_id", "")
                    if conv_id not in touched_conversations:
                        continue
                    conv_messages = [
                        m for m in data.get("messages", [])
                        if m.get("conversation_id") == conv_id
                    ]
                    conv_messages.sort(key=lambda x: x.get("created_at", ""))
                    if conv_messages:
                        last_msg = conv_messages[-1]
                        conv["last_message_content"] = last_msg.get("content", conv.get("last_message_content", ""))
                        conv["last_message_time"] = last_msg.get("created_at", conv.get("last_message_time", ""))
                        conv["updated_at"] = datetime.now().isoformat()
                        rewritten_conversations.append(conv)

            if updated_messages:
                self._persist_message_store_and_sidecar_unlocked(
                    data,
                    messages=rewritten_messages,
                    conversations=rewritten_conversations,
                )
                logger.info(
                    f"修复误标消息完成: updated_messages={updated_messages}, "
                    f"touched_conversations={len(touched_conversations)}"
                )

            return {
                "updated_messages": updated_messages,
                "touched_conversations": len(touched_conversations),
            }

    def repair_conversation_identity_labels(self, conversation_id: str = "") -> dict:
        """按同一会话中稳定的 inbound sender_name 回填真实会话名，修复被会话预览污染的摘要字段。"""
        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            messages = data.get("messages", [])
            conversations = data.get("conversations", [])
            processed_events = data.get("processed_events", [])
            outbox_events = data.get("outbox_events", [])
            workflow_runs = data.get("workflow_runs", [])

            inbound_names_by_conversation = {}
            for msg in messages:
                conv_id = str(msg.get("conversation_id", "") or "").strip()
                if not conv_id or (conversation_id and conv_id != conversation_id):
                    continue
                if str(msg.get("direction", "") or "").strip() != "inbound":
                    continue
                sender_name = str(msg.get("sender_name", "") or "").strip()
                if not sender_name:
                    continue
                inbound_names_by_conversation.setdefault(conv_id, [])
                if sender_name not in inbound_names_by_conversation[conv_id]:
                    inbound_names_by_conversation[conv_id].append(sender_name)

            updated_conversations = []
            updated_processed_events = []
            updated_outbox_events = []
            updated_workflow_runs = []
            updated_messages = []
            repaired_count = 0

            for index, conv in enumerate(conversations):
                conv_id = str(conv.get("conversation_id", "") or "").strip()
                if not conv_id or (conversation_id and conv_id != conversation_id):
                    continue
                inbound_names = inbound_names_by_conversation.get(conv_id, [])
                if len(inbound_names) != 1:
                    continue
                inferred_name = inbound_names[0]
                current_name = str(conv.get("customer_name", "") or "").strip()
                current_customer_id = str(conv.get("customer_id", "") or "").strip()
                should_update_name = bool(inferred_name) and inferred_name != current_name
                should_update_customer_id = (
                    not current_customer_id
                    or current_customer_id.startswith("temp_")
                    or current_customer_id == current_name
                ) and current_customer_id != inferred_name
                if not should_update_name and not should_update_customer_id:
                    continue

                updated_conv = dict(conv)
                if should_update_name:
                    updated_conv["customer_name"] = inferred_name
                if should_update_customer_id:
                    updated_conv["customer_id"] = inferred_name
                updated_conv["updated_at"] = datetime.now().isoformat()
                conversations[index] = updated_conv
                updated_conversations.append(updated_conv)
                repaired_count += 1

                for msg in messages:
                    if str(msg.get("conversation_id", "") or "").strip() != conv_id:
                        continue
                    if str(msg.get("customer_name", "") or "").strip() in {"", current_name}:
                        msg["customer_name"] = inferred_name
                        updated_messages.append(msg)

                for event in processed_events:
                    if str(event.get("conversation_id", "") or "").strip() != conv_id:
                        continue
                    if str(event.get("customer_name", "") or "").strip() in {"", current_name}:
                        event["customer_name"] = inferred_name
                        updated_processed_events.append(event)

                for event in outbox_events:
                    if str(event.get("conversation_id", "") or "").strip() != conv_id:
                        continue
                    if str(event.get("customer_name", "") or "").strip() in {"", current_name}:
                        event["customer_name"] = inferred_name
                        updated_outbox_events.append(event)

                for run in workflow_runs:
                    if str(run.get("conversation_id", "") or "").strip() != conv_id:
                        continue
                    if str(run.get("customer_name", "") or "").strip() in {"", current_name}:
                        run["customer_name"] = inferred_name
                    if should_update_customer_id and str(run.get("customer_id", "") or "").strip() in {"", current_name}:
                        run["customer_id"] = inferred_name
                    updated_workflow_runs.append(run)

            if repaired_count:
                self._persist_message_store_and_sidecar_unlocked(
                    data,
                    messages=updated_messages,
                    conversations=updated_conversations,
                    processed_events=updated_processed_events,
                    outbox_events=updated_outbox_events,
                    workflow_runs=updated_workflow_runs,
                )
                logger.info(
                    f"修复会话身份标签完成: repaired_conversations={repaired_count}, "
                    f"updated_messages={len(updated_messages)}"
                )

            return {
                "repaired_conversations": repaired_count,
                "updated_messages": len(updated_messages),
                "updated_processed_events": len(updated_processed_events),
                "updated_outbox_events": len(updated_outbox_events),
                "updated_workflow_runs": len(updated_workflow_runs),
            }

    def find_best_conversation_id(
        self,
        *,
        customer_name: str,
        platform: str = "douyin",
    ) -> str:
        """为同名客户选择最可信的会话 ID，优先消息量更多且最近更新的记录。"""
        normalized_name = str(customer_name or "").strip()
        if not normalized_name:
            return ""

        data = self._get_json_store_snapshot()
        candidates = []
        messages = self._get_json_messages_from_snapshot(data)
        for conv in self._get_json_conversations_from_snapshot(data):
            if str(conv.get("platform", "douyin")).strip() != platform:
                continue
            if str(conv.get("customer_name", "")).strip() != normalized_name:
                continue
            conv_id = str(conv.get("conversation_id", "")).strip()
            if not conv_id:
                continue
            message_count = sum(
                1 for msg in messages
                if msg.get("conversation_id") == conv_id
            )
            candidates.append(
                (
                    message_count,
                    str(conv.get("updated_at", conv.get("last_message_time", ""))),
                    conv_id,
                )
            )

        if not candidates:
            return ""
        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return candidates[0][2]

    def find_best_conversation_id_by_customer_id(
        self,
        *,
        customer_id: str,
        platform: str = "douyin",
    ) -> str:
        """为同一 customer_id 选择最可信的会话 ID，优先消息量更多且最近更新的记录。"""
        normalized_customer_id = str(customer_id or "").strip()
        if not normalized_customer_id:
            return ""

        candidates = []
        try:
            from src.common.chat_store import ChatStoreFacade

            chat_store = ChatStoreFacade(self)
            messages = chat_store.get_all_messages_dicts()
            conversations = chat_store.get_all_conversations_dicts()
        except Exception:
            data = self._get_json_store_snapshot()
            messages = self._get_json_messages_from_snapshot(data)
            conversations = self._get_json_conversations_from_snapshot(data)

        for conv in conversations:
            if str(conv.get("platform", "douyin")).strip() != platform:
                continue
            if str(conv.get("customer_id", "")).strip() != normalized_customer_id:
                continue
            conv_id = str(conv.get("conversation_id", "")).strip()
            if not conv_id:
                continue
            message_count = sum(
                1 for msg in messages
                if msg.get("conversation_id") == conv_id
            )
            candidates.append(
                (
                    message_count,
                    str(conv.get("updated_at", conv.get("last_message_time", ""))),
                    conv_id,
                )
            )

        if not candidates:
            return ""
        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return candidates[0][2]

    def merge_customer_conversations(
        self,
        *,
        customer_name: str,
        platform: str = "douyin",
        primary_conversation_id: str = "",
    ) -> dict:
        """将同一客户的多条会话合并到一个主会话 ID。"""
        normalized_name = str(customer_name or "").strip()
        if not normalized_name:
            return {"merged": False, "primary_conversation_id": "", "alias_count": 0}

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            conversations = [
                conv for conv in data.get("conversations", [])
                if str(conv.get("platform", "douyin")).strip() == platform
                and str(conv.get("customer_name", "")).strip() == normalized_name
            ]
            conversation_ids = []
            for conv in conversations:
                conv_id = str(conv.get("conversation_id", "")).strip()
                if conv_id and conv_id not in conversation_ids:
                    conversation_ids.append(conv_id)
            if len(conversation_ids) <= 1 and not primary_conversation_id:
                return {
                    "merged": False,
                    "primary_conversation_id": conversation_ids[0] if conversation_ids else "",
                    "alias_count": 0,
                }

            primary_id = (primary_conversation_id or "").strip() or self.find_best_conversation_id(
                customer_name=normalized_name,
                platform=platform,
            )
            if not primary_id and conversation_ids:
                primary_id = conversation_ids[0]
            if not primary_id:
                return {"merged": False, "primary_conversation_id": "", "alias_count": 0}

            if primary_id not in conversation_ids:
                conversation_ids.insert(0, primary_id)
            alias_ids = [cid for cid in conversation_ids if cid != primary_id]
            if not alias_ids:
                return {"merged": False, "primary_conversation_id": primary_id, "alias_count": 0}

            touched_sections = 0
            touched_messages = 0
            for msg in data.get("messages", []):
                if msg.get("conversation_id") in alias_ids:
                    msg["conversation_id"] = primary_id
                    touched_messages += 1
            for msg in data.get("reply_reservations", []):
                if msg.get("conversation_id") in alias_ids:
                    msg["conversation_id"] = primary_id
                    touched_sections += 1
            for event in data.get("processed_events", []):
                if event.get("conversation_id") in alias_ids:
                    event["conversation_id"] = primary_id
                    touched_sections += 1
            for event in data.get("outbox_events", []):
                if event.get("conversation_id") in alias_ids:
                    event["conversation_id"] = primary_id
                    touched_sections += 1
            for run in data.get("workflow_runs", []):
                if run.get("conversation_id") in alias_ids:
                    run["conversation_id"] = primary_id
                    touched_sections += 1

            deduped_messages = []
            seen_keys = set()
            for msg in sorted(data.get("messages", []), key=lambda item: item.get("created_at", "")):
                key = (
                    str(msg.get("message_id", "")).strip(),
                    str(msg.get("logical_message_id", "")).strip(),
                    str(msg.get("conversation_id", "")).strip(),
                    str(msg.get("direction", "")).strip(),
                    str(msg.get("created_at", "")).strip(),
                    str(msg.get("content", "")).strip(),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped_messages.append(msg)
            data["messages"] = deduped_messages

            conv_by_id = {str(conv.get("conversation_id", "")).strip(): conv for conv in conversations}
            merged_conversation = copy.deepcopy(conv_by_id.get(primary_id, {}))
            merged_conversation["conversation_id"] = primary_id
            merged_conversation["customer_name"] = normalized_name
            merged_conversation["platform"] = platform
            merged_conversation["updated_at"] = datetime.now().isoformat()

            primary_messages = [
                m for m in data.get("messages", [])
                if m.get("conversation_id") == primary_id
            ]
            primary_messages.sort(key=lambda item: item.get("created_at", ""))
            if primary_messages:
                last_msg = primary_messages[-1]
                merged_conversation["last_message_content"] = last_msg.get(
                    "content", merged_conversation.get("last_message_content", "")
                )
                merged_conversation["last_message_time"] = last_msg.get(
                    "created_at", merged_conversation.get("last_message_time", "")
                )
                merged_conversation["message_count"] = len(primary_messages)

            best_score = -1.0
            best_conv = None
            for cid in [primary_id] + alias_ids:
                conv = conv_by_id.get(cid)
                if not conv:
                    continue
                score = float(conv.get("purchase_intent_score", 0) or 0)
                updated_at = str(conv.get("updated_at", conv.get("last_message_time", "")))
                if best_conv is None or score > best_score or (
                    score == best_score and updated_at > str(best_conv.get("updated_at", best_conv.get("last_message_time", "")))
                ):
                    best_score = score
                    best_conv = conv
            if best_conv:
                for key, value in best_conv.items():
                    if key in {"conversation_id", "customer_name", "platform"}:
                        continue
                    if value not in (None, "", [], {}):
                        merged_conversation[key] = copy.deepcopy(value)

            deduped_conversations = []
            appended_primary = False
            for conv in data.get("conversations", []):
                conv_id = str(conv.get("conversation_id", "")).strip()
                if conv_id in alias_ids:
                    continue
                if conv_id == primary_id:
                    if not appended_primary:
                        deduped_conversations.append(merged_conversation)
                        appended_primary = True
                    continue
                deduped_conversations.append(conv)
            if not appended_primary:
                deduped_conversations.append(merged_conversation)
            data["conversations"] = deduped_conversations

            self._persist_message_store_and_sidecar_unlocked(data, conversations=[merged_conversation])
            logger.info(
                f"归并客户会话完成: customer={normalized_name}, primary={primary_id}, "
                f"aliases={alias_ids}, touched_messages={touched_messages}, touched_meta={touched_sections}"
            )
            return {
                "merged": True,
                "primary_conversation_id": primary_id,
                "alias_ids": alias_ids,
                "touched_messages": touched_messages,
                "touched_meta": touched_sections,
            }

    def merge_customer_conversations_by_customer_id(
        self,
        *,
        customer_id: str,
        platform: str = "douyin",
        primary_conversation_id: str = "",
        preferred_customer_name: str = "",
    ) -> dict:
        """将同一 customer_id 的多条会话合并到一个主会话 ID。"""
        normalized_customer_id = str(customer_id or "").strip()
        if not normalized_customer_id:
            return {"merged": False, "primary_conversation_id": "", "alias_count": 0}

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            conversations = [
                conv for conv in data.get("conversations", [])
                if str(conv.get("platform", "douyin")).strip() == platform
                and str(conv.get("customer_id", "")).strip() == normalized_customer_id
            ]
            conversation_ids = []
            for conv in conversations:
                conv_id = str(conv.get("conversation_id", "")).strip()
                if conv_id and conv_id not in conversation_ids:
                    conversation_ids.append(conv_id)
            if len(conversation_ids) <= 1 and not primary_conversation_id:
                return {
                    "merged": False,
                    "primary_conversation_id": conversation_ids[0] if conversation_ids else "",
                    "alias_count": 0,
                }

            primary_id = (primary_conversation_id or "").strip() or self.find_best_conversation_id_by_customer_id(
                customer_id=normalized_customer_id,
                platform=platform,
            )
            if not primary_id and conversation_ids:
                primary_id = conversation_ids[0]
            if not primary_id:
                return {"merged": False, "primary_conversation_id": "", "alias_count": 0}

            if primary_id not in conversation_ids:
                conversation_ids.insert(0, primary_id)
            alias_ids = [cid for cid in conversation_ids if cid != primary_id]
            if not alias_ids:
                return {"merged": False, "primary_conversation_id": primary_id, "alias_count": 0}

            touched_sections = 0
            touched_messages = 0
            for msg in data.get("messages", []):
                if msg.get("conversation_id") in alias_ids:
                    msg["conversation_id"] = primary_id
                    touched_messages += 1
            for msg in data.get("reply_reservations", []):
                if msg.get("conversation_id") in alias_ids:
                    msg["conversation_id"] = primary_id
                    touched_sections += 1
            for event in data.get("processed_events", []):
                if event.get("conversation_id") in alias_ids:
                    event["conversation_id"] = primary_id
                    if preferred_customer_name:
                        event["customer_name"] = preferred_customer_name
                    touched_sections += 1
            for event in data.get("outbox_events", []):
                if event.get("conversation_id") in alias_ids:
                    event["conversation_id"] = primary_id
                    if preferred_customer_name:
                        event["customer_name"] = preferred_customer_name
                    touched_sections += 1
            for run in data.get("workflow_runs", []):
                if run.get("conversation_id") in alias_ids:
                    run["conversation_id"] = primary_id
                    if preferred_customer_name:
                        run["customer_name"] = preferred_customer_name
                    if normalized_customer_id:
                        run["customer_id"] = normalized_customer_id
                    touched_sections += 1

            deduped_messages = []
            seen_keys = set()
            for msg in sorted(data.get("messages", []), key=lambda item: item.get("created_at", "")):
                key = (
                    str(msg.get("message_id", "")).strip(),
                    str(msg.get("logical_message_id", "")).strip(),
                    str(msg.get("conversation_id", "")).strip(),
                    str(msg.get("direction", "")).strip(),
                    str(msg.get("created_at", "")).strip(),
                    str(msg.get("content", "")).strip(),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped_messages.append(msg)
            data["messages"] = deduped_messages

            conv_by_id = {str(conv.get("conversation_id", "")).strip(): conv for conv in conversations}
            merged_conversation = copy.deepcopy(conv_by_id.get(primary_id, {}))
            merged_conversation["conversation_id"] = primary_id
            merged_conversation["customer_id"] = normalized_customer_id
            merged_conversation["platform"] = platform
            merged_conversation["updated_at"] = datetime.now().isoformat()

            primary_messages = [
                m for m in data.get("messages", [])
                if m.get("conversation_id") == primary_id
            ]
            primary_messages.sort(key=lambda item: item.get("created_at", ""))
            if primary_messages:
                last_msg = primary_messages[-1]
                merged_conversation["last_message_content"] = last_msg.get(
                    "content", merged_conversation.get("last_message_content", "")
                )
                merged_conversation["last_message_time"] = last_msg.get(
                    "created_at", merged_conversation.get("last_message_time", "")
                )
                merged_conversation["message_count"] = len(primary_messages)

            best_score = -1.0
            best_conv = None
            for cid in [primary_id] + alias_ids:
                conv = conv_by_id.get(cid)
                if not conv:
                    continue
                score = float(conv.get("purchase_intent_score", 0) or 0)
                updated_at = str(conv.get("updated_at", conv.get("last_message_time", "")))
                if best_conv is None or score > best_score or (
                    score == best_score and updated_at > str(best_conv.get("updated_at", best_conv.get("last_message_time", "")))
                ):
                    best_score = score
                    best_conv = conv
            if best_conv:
                for key, value in best_conv.items():
                    if key in {"conversation_id", "customer_id", "platform"}:
                        continue
                    if value not in (None, "", [], {}):
                        merged_conversation[key] = copy.deepcopy(value)

            if preferred_customer_name:
                merged_conversation["customer_name"] = preferred_customer_name
            elif not merged_conversation.get("customer_name"):
                for cid in [primary_id] + alias_ids:
                    conv = conv_by_id.get(cid)
                    name = str((conv or {}).get("customer_name", "")).strip()
                    if name:
                        merged_conversation["customer_name"] = name
                        break

            deduped_conversations = []
            appended_primary = False
            for conv in data.get("conversations", []):
                conv_id = str(conv.get("conversation_id", "")).strip()
                if conv_id in alias_ids:
                    continue
                if conv_id == primary_id:
                    if not appended_primary:
                        deduped_conversations.append(merged_conversation)
                        appended_primary = True
                    continue
                deduped_conversations.append(conv)
            if not appended_primary:
                deduped_conversations.append(merged_conversation)
            data["conversations"] = deduped_conversations

            self._persist_message_store_and_sidecar_unlocked(data, conversations=[merged_conversation])
            logger.info(
                f"按 customer_id 归并会话完成: customer_id={normalized_customer_id}, primary={primary_id}, "
                f"aliases={alias_ids}, touched_messages={touched_messages}, touched_meta={touched_sections}"
            )
            return {
                "merged": True,
                "primary_conversation_id": primary_id,
                "alias_ids": alias_ids,
                "touched_messages": touched_messages,
                "touched_meta": touched_sections,
            }

    def try_reserve_inbound_event(
        self,
        logical_message_id: str,
        *,
        conversation_id: str,
        customer_name: str,
        content: str,
        source_message_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        """预留入站逻辑事件，避免跨入口重复消费。"""
        if not logical_message_id:
            return True

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            events = data.get("processed_events", [])
            for event in events:
                if event.get("logical_message_id") != logical_message_id:
                    continue
                status = event.get("status", "")
                if status in {"processing", "done", "skipped"}:
                    return False
                event.update(
                    {
                        "status": "processing",
                        "updated_at": datetime.now().isoformat(),
                    }
                )
                self._persist_message_store_and_sidecar_unlocked(data, processed_events=[event])
                return True

            new_event = {
                "logical_message_id": logical_message_id,
                "conversation_id": conversation_id,
                "customer_name": customer_name,
                "content_hash": hashlib.md5((content or "").encode("utf-8")).hexdigest()[:16],
                "source_message_id": source_message_id,
                "platform": platform,
                "status": "processing",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            events.append(new_event)
            self._persist_message_store_and_sidecar_unlocked(data, processed_events=[new_event])
            return True

    def finalize_inbound_event(
        self,
        logical_message_id: str,
        *,
        status: str,
        message_id: str = "",
        source_message_id: str = "",
        reply_message_id: str = "",
        reason: str = "",
    ):
        """更新入站逻辑事件状态。"""
        if not logical_message_id:
            return

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            updated_at = datetime.now().isoformat()
            updated_event = None
            for event in data.get("processed_events", []):
                if event.get("logical_message_id") == logical_message_id:
                    event["status"] = status
                    event["updated_at"] = updated_at
                    normalized_source_message_id = str(source_message_id or "").strip()
                    normalized_reply_message_id = str(reply_message_id or message_id or "").strip()
                    if normalized_source_message_id:
                        event["source_message_id"] = normalized_source_message_id
                    if normalized_reply_message_id:
                        event["reply_message_id"] = normalized_reply_message_id
                        event["message_id"] = normalized_reply_message_id
                    if reason:
                        event["reason"] = reason
                    updated_event = event
                    break

            message_processed = status in {"done", "skipped"}
            affected_messages = []
            for msg in data.get("messages", []):
                if msg.get("direction") != "inbound":
                    continue
                if msg.get("logical_message_id") != logical_message_id:
                    continue
                msg["is_processed"] = message_processed
                msg["processed_status"] = status
                msg["processed_at"] = updated_at
                if reason:
                    msg["processed_reason"] = reason
                affected_messages.append(msg)
            self._persist_message_store_and_sidecar_unlocked(
                data,
                messages=affected_messages,
                processed_events=[updated_event] if updated_event else None,
            )

    def get_inbound_event(self, logical_message_id: str) -> dict:
        """按 logical_message_id 读取入站逻辑事件。"""
        if not logical_message_id:
            return {}

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            for event in data.get("processed_events", []):
                if str(event.get("logical_message_id", "") or "").strip() == logical_message_id:
                    return dict(event)
        return {}

    def get_inbound_event_by_source_message_id(
        self,
        source_message_id: str,
        *,
        conversation_id: str = "",
    ) -> dict:
        """按 source_message_id 读取已存在的入站逻辑事件。"""
        source_message_id = str(source_message_id or "").strip()
        conversation_id = str(conversation_id or "").strip()
        if not source_message_id:
            return {}

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            for event in data.get("processed_events", []):
                if str(event.get("source_message_id", "") or "").strip() != source_message_id:
                    continue
                event_conversation_id = str(event.get("conversation_id", "") or "").strip()
                if conversation_id and event_conversation_id and event_conversation_id != conversation_id:
                    continue
                return dict(event)
        return {}

    def sync_inbound_message_processing_state(self) -> dict:
        """按 processed_events 回填历史入站消息处理状态。"""
        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            processed_events_by_logical = {
                str(item.get("logical_message_id", "") or ""): item
                for item in data.get("processed_events", [])
                if str(item.get("logical_message_id", "") or "").strip()
            }
            updated = 0
            for msg in data.get("messages", []):
                if msg.get("direction") != "inbound":
                    continue
                logical_message_id = str(msg.get("logical_message_id", "") or "").strip()
                if not logical_message_id:
                    continue
                event = processed_events_by_logical.get(logical_message_id)
                if not event:
                    continue
                status = str(event.get("status", "") or "").strip().lower()
                should_processed = status in {"done", "skipped"}
                before = (
                    bool(msg.get("is_processed", False)),
                    str(msg.get("processed_status", "") or "").strip().lower(),
                )
                after = (should_processed, status)
                if before == after:
                    continue
                msg["is_processed"] = should_processed
                msg["processed_status"] = status
                msg["processed_at"] = str(event.get("updated_at", "") or "")
                if event.get("reason"):
                    msg["processed_reason"] = event.get("reason")
                updated += 1

            if updated:
                self._persist_message_store_and_sidecar_unlocked(data)
            return {"updated_messages": updated, "events": len(processed_events_by_logical)}

    def create_outbox_event(
        self,
        *,
        outbox_id: str,
        logical_message_id: str,
        conversation_id: str,
        customer_name: str,
        reply_content: str,
        platform: str,
        source_message_id: str = "",
        outbound_source: str = "",
        outbound_trigger: str = "",
    ):
        """创建出站事件记录。"""
        if not outbox_id:
            return

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            events = data.get("outbox_events", [])
            for event in events:
                if event.get("outbox_id") == outbox_id:
                    return
            new_event = {
                "outbox_id": outbox_id,
                "logical_message_id": logical_message_id,
                "conversation_id": conversation_id,
                "customer_name": customer_name,
                "platform": platform,
                "reply_content": reply_content,
                "reply_hash": hashlib.md5((reply_content or "").encode("utf-8")).hexdigest()[:16],
                "source_message_id": source_message_id,
                "outbound_source": str(outbound_source or "").strip(),
                "outbound_trigger": str(outbound_trigger or "").strip(),
                "status": "reserved",
                "retry_count": 0,
                "next_retry_at": "",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            events.append(new_event)
            self._persist_message_store_and_sidecar_unlocked(data, outbox_events=[new_event])

    def update_outbox_event(
        self,
        outbox_id: str,
        *,
        status: str,
        message_id: str = "",
        reason: Optional[str] = None,
        retry_count: Optional[int] = None,
        next_retry_at: Optional[str] = None,
        workflow_run_id: str = "",
        outbound_source: Optional[str] = None,
        outbound_trigger: Optional[str] = None,
        delivery_channel: Optional[str] = None,
    ):
        """更新出站事件状态。"""
        if not outbox_id:
            return

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            updated_event = None
            for event in data.get("outbox_events", []):
                if event.get("outbox_id") == outbox_id:
                    event["status"] = status
                    event["updated_at"] = datetime.now().isoformat()
                    if message_id:
                        event["message_id"] = message_id
                    if reason is not None:
                        event["reason"] = reason
                    if retry_count is not None:
                        event["retry_count"] = retry_count
                    if next_retry_at is not None:
                        event["next_retry_at"] = next_retry_at
                    if workflow_run_id:
                        event["workflow_run_id"] = workflow_run_id
                    if outbound_source is not None:
                        event["outbound_source"] = str(outbound_source or "").strip()
                    if outbound_trigger is not None:
                        event["outbound_trigger"] = str(outbound_trigger or "").strip()
                    if delivery_channel is not None:
                        event["delivery_channel"] = str(delivery_channel or "").strip()
                    updated_event = event
                    break
            self._persist_message_store_and_sidecar_unlocked(
                data,
                outbox_events=[updated_event] if updated_event else None,
            )

    def get_outbox_event(self, outbox_id: str) -> Optional[dict]:
        """获取单个出站事件。"""
        data = self._get_json_store_snapshot()
        for event in self._get_json_outbox_events_from_snapshot(data):
            if event.get("outbox_id") == outbox_id:
                return copy.deepcopy(event)
        return None

    def list_outbox_events(self, statuses: Optional[List[str]] = None, limit: int = 100) -> List[dict]:
        """列出出站事件。"""
        data = self._get_json_store_snapshot()
        events = self._get_json_outbox_events_from_snapshot(data)
        if statuses:
            status_set = set(statuses)
            events = [item for item in events if item.get("status") in status_set]
        events.sort(key=lambda x: x.get("updated_at", x.get("created_at", "")), reverse=True)
        return copy.deepcopy(events[:limit])

    def upsert_workflow_run(self, workflow_run: dict):
        """创建或更新工作流运行记录。"""
        workflow_run_id = workflow_run.get("workflow_run_id", "")
        if not workflow_run_id:
            return

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            runs = data.get("workflow_runs", [])
            synced_run = None
            found = False
            for index, existing in enumerate(runs):
                if existing.get("workflow_run_id") == workflow_run_id:
                    merged = copy.deepcopy(existing)
                    merged.update(workflow_run)
                    merged["updated_at"] = datetime.now().isoformat()
                    runs[index] = merged
                    synced_run = merged
                    found = True
                    break

            if not found:
                payload = copy.deepcopy(workflow_run)
                now = datetime.now().isoformat()
                payload.setdefault("created_at", now)
                payload["updated_at"] = now
                payload.setdefault("nodes", {})
                payload.setdefault("history", [])
                runs.append(payload)
                synced_run = payload

            self._persist_message_store_and_sidecar_unlocked(
                data,
                workflow_runs=[synced_run] if synced_run else None,
            )

    def get_workflow_run(self, workflow_run_id: str) -> Optional[dict]:
        """获取工作流运行记录。"""
        data = self._get_json_store_snapshot()
        for run in self._get_json_workflow_runs_from_snapshot(data):
            if run.get("workflow_run_id") == workflow_run_id:
                return copy.deepcopy(run)
        return None

    def list_workflow_runs(self, statuses: Optional[List[str]] = None, limit: int = 100) -> List[dict]:
        """列出工作流运行记录。"""
        data = self._get_json_store_snapshot()
        runs = self._get_json_workflow_runs_from_snapshot(data)
        if statuses:
            status_set = set(statuses)
            runs = [item for item in runs if item.get("status") in status_set]
        runs.sort(key=lambda x: x.get("updated_at", x.get("created_at", "")), reverse=True)
        return copy.deepcopy(runs[:limit])

    def upsert_handoff_ticket(self, handoff_ticket: dict):
        """创建或更新人工接管单。"""
        ticket_id = str((handoff_ticket or {}).get("ticket_id", "") or "").strip()
        if not ticket_id:
            return

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            tickets = data.get("handoff_tickets", [])
            synced_ticket = None
            found = False
            for index, existing in enumerate(tickets):
                if existing.get("ticket_id") == ticket_id:
                    merged = copy.deepcopy(existing)
                    merged.update(handoff_ticket)
                    merged["updated_at"] = datetime.now().isoformat()
                    tickets[index] = merged
                    synced_ticket = merged
                    found = True
                    break

            if not found:
                payload = copy.deepcopy(handoff_ticket)
                now = datetime.now().isoformat()
                payload.setdefault("created_at", now)
                payload["updated_at"] = now
                payload.setdefault("eligibility_snapshot", {})
                payload.setdefault("reply_analysis_snapshot", {})
                tickets.append(payload)
                synced_ticket = payload

            self._persist_message_store_and_sidecar_unlocked(
                data,
                handoff_tickets=[synced_ticket] if synced_ticket else None,
            )

    def get_handoff_ticket(self, ticket_id: str) -> Optional[dict]:
        data = self._get_json_store_snapshot()
        for ticket in self._get_json_handoff_tickets_from_snapshot(data):
            if ticket.get("ticket_id") == ticket_id:
                return copy.deepcopy(ticket)
        return None

    def list_handoff_tickets(
        self,
        statuses: Optional[List[str]] = None,
        owner: str = "",
        limit: int = 100,
    ) -> List[dict]:
        data = self._get_json_store_snapshot()
        tickets = self._get_json_handoff_tickets_from_snapshot(data)
        if statuses:
            status_set = set(statuses)
            tickets = [item for item in tickets if item.get("status") in status_set]
        normalized_owner = str(owner or "").strip()
        if normalized_owner:
            tickets = [item for item in tickets if str(item.get("owner", "") or "").strip() == normalized_owner]
        tickets.sort(key=lambda x: x.get("updated_at", x.get("created_at", "")), reverse=True)
        return copy.deepcopy(tickets[:limit])
    
    def save_conversation(self, conversation_data: dict):
        """保存或更新会话（合并更新策略，保留未传入字段的旧值，读-改-写原子操作）"""
        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
        
            conversation = {
                "conversation_id": conversation_data.get("conversation_id", ""),
                "platform": conversation_data.get("platform", "douyin"),
                "customer_id": conversation_data.get("customer_id", ""),
                "customer_name": conversation_data.get("customer_name", ""),
                "customer_avatar": conversation_data.get("customer_avatar", ""),
                "status": conversation_data.get("status", "active"),
                "last_message_time": conversation_data.get("last_message_time", datetime.now().isoformat()),
                "last_message_content": conversation_data.get("last_message_content", ""),
                "unread_count": conversation_data.get("unread_count", 0),
                "intent_level": conversation_data.get("intent_level", ""),
                "created_at": conversation_data.get("created_at", datetime.now().isoformat()),
                "updated_at": datetime.now().isoformat(),
                "purchase_intent_score": conversation_data.get("purchase_intent_score", 0),
                "lead_score": conversation_data.get("lead_score", "cold"),
                "follow_up_priority": conversation_data.get("follow_up_priority", "low"),
                "purchase_probability": conversation_data.get("purchase_probability", 0),
                "estimated_deal_size": conversation_data.get("estimated_deal_size", 0),
                "lifecycle_stage": conversation_data.get("lifecycle_stage", "prospect"),
                "buying_role": conversation_data.get("buying_role", "unknown"),
                "signals_detected": conversation_data.get("signals_detected", []),
                "risk_factors": conversation_data.get("risk_factors", []),
                "opportunity_factors": conversation_data.get("opportunity_factors", []),
                "intent_history": conversation_data.get("intent_history", []),
                "intent_trend": conversation_data.get("intent_trend", {}),
                "last_analysis_time": conversation_data.get("last_analysis_time", ""),
                "next_follow_up_time": conversation_data.get("next_follow_up_time", ""),
                "last_completed_follow_up_signature": conversation_data.get("last_completed_follow_up_signature", ""),
                "last_completed_follow_up_at": conversation_data.get("last_completed_follow_up_at", ""),
                "last_dingtalk_notification_signature": conversation_data.get("last_dingtalk_notification_signature", ""),
                "last_dingtalk_notification_at": conversation_data.get("last_dingtalk_notification_at", ""),
                "last_dingtalk_notification_status": conversation_data.get("last_dingtalk_notification_status", ""),
                "last_dingtalk_notification_error": conversation_data.get("last_dingtalk_notification_error", ""),
                "last_dingtalk_notification_task_id": conversation_data.get("last_dingtalk_notification_task_id", ""),
            }
        
            found = False
            for i, conv in enumerate(data["conversations"]):
                if conv.get("conversation_id") == conversation.get("conversation_id"):
                    for key in conv:
                        if key not in conversation_data:
                            if conv.get(key) is not None and conv.get(key) != "":
                                conversation[key] = conv[key]
                        elif conversation_data.get(key) is None:
                            if conv.get(key) is not None and conv.get(key) != "":
                                conversation[key] = conv[key]
                    conversation["created_at"] = conv.get("created_at", conversation["created_at"])
                    data["conversations"][i] = conversation
                    found = True
                    break
        
            if not found:
                data["conversations"].append(conversation)
        
            self._persist_message_store_and_sidecar_unlocked(data, conversations=[conversation])
    
    def update_conversation_intent(self, conversation_id: str, intent_data: dict):
        """
        更新会话的意向分析数据
        
        Args:
            conversation_id: 会话ID
            intent_data: 意向分析数据
        """
        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            updated_conversation = None
            
            for i, conv in enumerate(data["conversations"]):
                if conv.get("conversation_id") == conversation_id:
                    total_score = round(float(intent_data.get("total_score", 0) or 0), 2)
                    lead_score = intent_data.get("lead_score", "cold")
                    mapped_intent_level = (
                        str(intent_data.get("intent_level") or "").strip().upper()
                        or score_to_intent_level(total_score)
                        or self._map_lead_score_to_intent(lead_score)
                    )
                    purchase_probability = round(float(intent_data.get("purchase_probability", 0) or 0), 4)
                    now_iso = datetime.now().isoformat()
                    history = self._normalize_intent_history(conv.get("intent_history", []))
                    latest_point = {
                        "time": now_iso,
                        "score": total_score,
                        "level": mapped_intent_level,
                        "lead_score": lead_score,
                        "purchase_probability": purchase_probability,
                        "follow_up_priority": intent_data.get("follow_up_priority", "low"),
                        "reasons": self._build_intent_reason_list(intent_data),
                        "recommended_action": intent_data.get("predicted_next_action")
                        or intent_data.get("recommended_response_type")
                        or lead_score,
                    }
                    if history:
                        prev = history[-1]
                        if (
                            round(float(prev.get("score", 0) or 0), 2) == total_score
                            and prev.get("level") == mapped_intent_level
                            and prev.get("lead_score") == lead_score
                        ):
                            history[-1] = latest_point
                        else:
                            history.append(latest_point)
                    else:
                        history.append(latest_point)
                    history = history[-12:]
                    trend = self._build_intent_trend(history)

                    data["conversations"][i].update({
                        "purchase_intent_score": total_score,
                        "lead_score": lead_score,
                        "follow_up_priority": intent_data.get("follow_up_priority", "low"),
                        "purchase_probability": purchase_probability,
                        "estimated_deal_size": intent_data.get("estimated_deal_size", 0),
                        "lifecycle_stage": intent_data.get("lifecycle_stage", "prospect"),
                        "buying_role": intent_data.get("buying_role", "unknown"),
                        "signals_detected": intent_data.get("signals_detected", []),
                        "risk_factors": intent_data.get("risk_factors", []),
                        "opportunity_factors": intent_data.get("opportunity_factors", []),
                        "intent_history": history,
                        "intent_trend": trend,
                        "last_analysis_time": now_iso,
                        "intent_level": mapped_intent_level,
                        "next_follow_up_time": intent_data.get("suggested_follow_up_time", conv.get("next_follow_up_time", "")),
                        "updated_at": now_iso
                    })
                    updated_conversation = dict(data["conversations"][i])
                    break
            
            self._persist_message_store_and_sidecar_unlocked(
                data,
                conversations=[updated_conversation] if updated_conversation else None,
            )
            logger.info(f"更新会话意向: {conversation_id} - {intent_data.get('lead_score', 'cold')}")

    def mark_follow_up_completed(self, conversation_id: str, signature: str) -> bool:
        """记录待跟进项已完成，基于签名避免同一提醒重复出现。"""
        normalized_conversation_id = str(conversation_id or "").strip()
        normalized_signature = str(signature or "").strip()
        if not normalized_conversation_id or not normalized_signature:
            return False

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            for index, conv in enumerate(data.get("conversations", [])):
                if str(conv.get("conversation_id") or "").strip() != normalized_conversation_id:
                    continue

                updated = dict(conv)
                updated["last_completed_follow_up_signature"] = normalized_signature
                updated["last_completed_follow_up_at"] = datetime.now().isoformat()
                updated["updated_at"] = datetime.now().isoformat()
                data["conversations"][index] = updated
                self._persist_message_store_and_sidecar_unlocked(data, conversations=[updated])
                return True

        return False

    def mark_dingtalk_notification_sent(self, conversation_id: str, signature: str, task_id: str = "") -> bool:
        normalized_conversation_id = str(conversation_id or "").strip()
        normalized_signature = str(signature or "").strip()
        if not normalized_conversation_id or not normalized_signature:
            return False

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            for index, conv in enumerate(data.get("conversations", [])):
                if str(conv.get("conversation_id") or "").strip() != normalized_conversation_id:
                    continue

                updated = dict(conv)
                updated["last_dingtalk_notification_signature"] = normalized_signature
                updated["last_dingtalk_notification_at"] = datetime.now().isoformat()
                updated["last_dingtalk_notification_status"] = "sent"
                updated["last_dingtalk_notification_error"] = ""
                updated["last_dingtalk_notification_task_id"] = str(task_id or "").strip()
                updated["updated_at"] = datetime.now().isoformat()
                data["conversations"][index] = updated
                self._persist_message_store_and_sidecar_unlocked(data, conversations=[updated])
                return True

        return False

    def mark_dingtalk_notification_failed(self, conversation_id: str, signature: str, error: str) -> bool:
        normalized_conversation_id = str(conversation_id or "").strip()
        normalized_signature = str(signature or "").strip()
        if not normalized_conversation_id or not normalized_signature:
            return False

        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            for index, conv in enumerate(data.get("conversations", [])):
                if str(conv.get("conversation_id") or "").strip() != normalized_conversation_id:
                    continue

                updated = dict(conv)
                updated["last_dingtalk_notification_signature"] = normalized_signature
                updated["last_dingtalk_notification_at"] = datetime.now().isoformat()
                updated["last_dingtalk_notification_status"] = "failed"
                updated["last_dingtalk_notification_error"] = str(error or "").strip()
                updated["last_dingtalk_notification_task_id"] = ""
                updated["updated_at"] = datetime.now().isoformat()
                data["conversations"][index] = updated
                self._persist_message_store_and_sidecar_unlocked(data, conversations=[updated])
                return True

        return False
    
    def _map_lead_score_to_intent(self, lead_score: str) -> str:
        """将线索评分映射到意向等级"""
        mapping = {
            "hot": "A",
            "warm": "B",
            "cool": "C",
            "cold": "E"
        }
        return mapping.get(str(lead_score or "").strip().lower(), "C")

    def _normalize_intent_history(self, history) -> list:
        """清洗并截断意向历史，确保序列可安全序列化。"""
        normalized = []
        if not isinstance(history, list):
            return normalized

        for item in history:
            if not isinstance(item, dict):
                continue
            normalized.append({
                "time": item.get("time", ""),
                "score": round(float(item.get("score", 0) or 0), 2),
                "level": item.get("level", "D"),
                "lead_score": item.get("lead_score", "cold"),
                "purchase_probability": round(float(item.get("purchase_probability", 0) or 0), 4),
                "follow_up_priority": item.get("follow_up_priority", "low"),
                "reasons": item.get("reasons", []) if isinstance(item.get("reasons", []), list) else [],
                "recommended_action": item.get("recommended_action", ""),
            })

        return normalized[-12:]

    def _build_intent_reason_list(self, intent_data: dict) -> list:
        """抽取每次分析的主要原因，用于时间轴与变化说明。"""
        reasons = []
        signals = intent_data.get("signals_detected", []) or []
        opportunities = intent_data.get("opportunity_factors", []) or []
        risks = intent_data.get("risk_factors", []) or []
        lifecycle_stage = intent_data.get("lifecycle_stage")
        lead_score = intent_data.get("lead_score")

        if lead_score:
            reasons.append(f"线索等级: {lead_score}")
        if lifecycle_stage:
            reasons.append(f"阶段: {lifecycle_stage}")
        reasons.extend([f"信号: {item}" for item in signals[:2]])
        reasons.extend([f"机会: {item}" for item in opportunities[:2]])
        reasons.extend([f"风险: {item}" for item in risks[:2]])
        return reasons[:5]

    def _build_intent_trend(self, history: list) -> dict:
        """基于历史分数生成会话意向趋势摘要。"""
        if not history:
            return {
                "direction": "stable",
                "delta": 0,
                "label": "暂无趋势",
                "momentum": "none",
                "comparison_text": "暂时没有可对比的历史分析",
                "follow_up_suggestion": "建议先完成一次意向分析",
            }

        if len(history) == 1:
            return {
                "direction": "stable",
                "delta": 0,
                "label": "首次分析",
                "momentum": "new",
                "current_score": history[-1].get("score", 0),
                "comparison_text": "首次形成意向基线，建议尽快继续跟进",
                "follow_up_suggestion": "建议 24 小时内跟进，验证客户真实需求",
            }

        window = history[-3:]
        first_score = float(window[0].get("score", 0) or 0)
        latest_score = float(window[-1].get("score", 0) or 0)
        delta = round(latest_score - first_score, 2)
        prev_score = float(history[-2].get("score", 0) or 0)
        delta_prev = round(latest_score - prev_score, 2)
        current_priority = history[-1].get("follow_up_priority", "low")

        if delta >= 8:
            direction = "up"
            label = "明显升温"
            momentum = "strong"
        elif delta >= 3:
            direction = "up"
            label = "持续升温"
            momentum = "medium"
        elif delta <= -8:
            direction = "down"
            label = "明显降温"
            momentum = "strong"
        elif delta <= -3:
            direction = "down"
            label = "轻微降温"
            momentum = "medium"
        else:
            direction = "stable"
            label = "相对稳定"
            momentum = "light"

        if delta_prev >= 0.5:
            comparison_text = f"较上次提升 {abs(delta_prev):.1f} 分"
        elif delta_prev <= -0.5:
            comparison_text = f"较上次下降 {abs(delta_prev):.1f} 分"
        else:
            comparison_text = "较上次基本持平"

        if direction == "up" and current_priority in {"urgent", "high"}:
            follow_up_suggestion = "建议 24 小时内跟进，趁热推进转化"
        elif direction == "up":
            follow_up_suggestion = "建议 48 小时内跟进，继续放大购买信号"
        elif direction == "down":
            follow_up_suggestion = "建议优先排查顾虑点，必要时当天回访"
        else:
            follow_up_suggestion = "建议按当前节奏保持触达，继续观察变化"

        return {
            "direction": direction,
            "delta": delta,
            "delta_prev": delta_prev,
            "label": label,
            "momentum": momentum,
            "current_score": latest_score,
            "baseline_score": first_score,
            "previous_score": prev_score,
            "comparison_text": comparison_text,
            "follow_up_suggestion": follow_up_suggestion,
            "updated_at": history[-1].get("time", ""),
        }
    
    def get_high_intent_conversations(self, min_score: float = 50) -> list:
        """
        获取高意向会话列表
        
        Args:
            min_score: 最低意向分数阈值
            
        Returns:
            list: 高意向会话列表
        """
        conversations = self._get_all_conversations_from_json()
        
        # 筛选高意向会话
        high_intent = [
            conv for conv in conversations
            if conv.get("purchase_intent_score", 0) >= min_score
        ]
        
        # 按意向分数排序
        high_intent.sort(key=lambda x: x.get("purchase_intent_score", 0), reverse=True)
        
        return high_intent
    
    def get_conversations_by_lead_score(self, lead_score: str) -> list:
        """
        按线索评分获取会话
        
        Args:
            lead_score: 线索评分 (hot/warm/cool/cold)
            
        Returns:
            list: 会话列表
        """
        conversations = self._get_all_conversations_from_json()
        return [
            conv for conv in conversations
            if conv.get("lead_score", "cold") == lead_score
        ]
    
    def get_pending_follow_ups(self) -> list:
        """
        获取待跟进的会话
        
        Returns:
            list: 需要跟进的会话列表
        """
        conversations = self._get_all_conversations_from_json()
        now = datetime.now()
        
        pending = []
        for conv in conversations:
            next_follow_up = conv.get("next_follow_up_time", "")
            if next_follow_up:
                try:
                    follow_up_time = datetime.fromisoformat(next_follow_up.replace("Z", "+00:00"))
                    if follow_up_time <= now:
                        pending.append(conv)
                except (ValueError, TypeError) as e:
                    logger.debug(f"跟进时间解析失败: {next_follow_up}, 错误: {e}")
        
        return pending
    
    def get_conversation_messages(self, conversation_id: str, limit: int = 9999) -> list:
        """获取会话消息历史"""
        return self._get_conversation_messages_from_json(conversation_id, limit=limit)

    def _get_conversation_messages_from_json(
        self,
        conversation_id: str,
        limit: int = 9999,
        *,
        exclude_preview_noise: bool = True,
    ) -> list:
        """显式从 JSON 主链读取会话消息，供主链兼容与对账逻辑复用。"""
        data = self._load_messages_data()
        
        system_messages_exact = [
            "你们已互相关注对方",
            "撤回了一条消息",
            "个人页卡片",
            "对方回复或关注你之前，只能发送一条文字消息",
            "请礼貌发言，自觉遵守《抖音自律公约》"
        ]
        system_messages_contains = [
            "互相关注",
        ]
        
        messages = [
            m for m in self._get_business_messages(data)
            if m.get("conversation_id") == conversation_id 
            and (
                not exclude_preview_noise
                or (
                    not any(m.get("content", "") == sys_msg for sys_msg in system_messages_exact)
                    and not any(sys_msg in m.get("content", "") and len(m.get("content", "")) < 20 for sys_msg in system_messages_contains)
                )
            )
        ]
        
        messages.sort(key=lambda x: x.get("created_at", ""), reverse=False)
        return copy.deepcopy(messages[-limit:] if limit < len(messages) else messages)

    def get_all_messages(self, limit: int = None) -> list:
        """获取所有消息"""
        return self._get_all_messages_from_json(limit=limit)

    def _get_all_messages_from_json(self, limit: int = None) -> list:
        """显式从 JSON 主链读取全部业务消息。"""
        data = self._load_messages_data()
        messages = self._get_business_messages(data)
        messages.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        if limit:
            return copy.deepcopy(messages[:limit])
        return copy.deepcopy(messages)

    def get_all_conversations(self) -> list:
        """获取所有会话"""
        return self._get_all_conversations_from_json()

    def _get_all_conversations_from_json(self) -> list:
        """显式从 JSON 主链读取全部会话摘要。"""
        data = self._load_messages_data()
        return copy.deepcopy(data.get("conversations", []))

    def _get_json_store_snapshot(self) -> dict:
        """显式读取 JSON 主链原始状态快照，供依赖事件/保留区的内部逻辑使用。"""
        return self._load_messages_data()

    @staticmethod
    def _get_json_messages_from_snapshot(snapshot: Optional[dict]) -> list:
        return list((snapshot or {}).get("messages", []))

    @staticmethod
    def _get_json_conversations_from_snapshot(snapshot: Optional[dict]) -> list:
        return list((snapshot or {}).get("conversations", []))

    @staticmethod
    def _get_json_processed_events_from_snapshot(snapshot: Optional[dict]) -> list:
        return list((snapshot or {}).get("processed_events", []))

    @staticmethod
    def _get_json_outbox_events_from_snapshot(snapshot: Optional[dict]) -> list:
        return list((snapshot or {}).get("outbox_events", []))

    @staticmethod
    def _get_json_workflow_runs_from_snapshot(snapshot: Optional[dict]) -> list:
        return list((snapshot or {}).get("workflow_runs", []))

    @staticmethod
    def _get_json_handoff_tickets_from_snapshot(snapshot: Optional[dict]) -> list:
        return list((snapshot or {}).get("handoff_tickets", []))

    def get_unread_messages(self) -> list:
        """获取所有未读消息"""
        messages = self._get_all_messages_from_json()
        return [m for m in messages if not m.get("is_read", True)]
    
    def get_unread_message_count(self) -> int:
        """获取未读消息数量（高性能单次迭代版）"""
        data = self._get_json_store_snapshot()
        messages = self._get_json_messages_from_snapshot(data)
        count = 0
        for m in messages:
            # 内联 _is_reservation_message 和业务逻辑判断，减少函数调用开销
            if m.get("is_read", True):
                continue
            if self._is_reservation_message(m):
                continue
            count += 1
        return count
    
    def mark_message_read(self, message_id: str):
        """标记消息为已读（原子操作）"""
        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            updated_messages = []
            for msg in data["messages"]:
                if msg.get("message_id") == message_id:
                    msg["is_read"] = True
                    updated_messages.append(dict(msg))
            self._persist_message_store_and_sidecar_unlocked(
                data,
                messages=updated_messages or None,
            )
    
    def mark_conversation_read(self, conversation_id: str):
        """标记会话所有消息为已读（原子操作）"""
        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            updated_messages = []
            updated_conversation = None
            for msg in data["messages"]:
                if msg.get("conversation_id") == conversation_id:
                    msg["is_read"] = True
                    updated_messages.append(dict(msg))
            for conv in data["conversations"]:
                if conv.get("conversation_id") == conversation_id:
                    conv["unread_count"] = 0
                    updated_conversation = dict(conv)
            self._persist_message_store_and_sidecar_unlocked(
                data,
                messages=updated_messages or None,
                conversations=[updated_conversation] if updated_conversation else None,
            )
    
    def update_conversation_status(self, conversation_id: str, status: str):
        """更新会话状态（原子操作）"""
        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            updated_conversation = None
            for conv in data["conversations"]:
                if conv.get("conversation_id") == conversation_id:
                    conv["status"] = status
                    conv["updated_at"] = datetime.now().isoformat()
                    updated_conversation = dict(conv)
            self._persist_message_store_and_sidecar_unlocked(
                data,
                conversations=[updated_conversation] if updated_conversation else None,
            )
    
    def update_intent_level(self, conversation_id: str, intent_level: str):
        """更新客户意向等级（原子操作）"""
        with DatabaseManager._msg_lock:
            data = self._load_messages_data_unlocked()
            updated_conversation = None
            for conv in data["conversations"]:
                if conv.get("conversation_id") == conversation_id:
                    conv["intent_level"] = intent_level
                    conv["updated_at"] = datetime.now().isoformat()
                    updated_conversation = dict(conv)
            self._persist_message_store_and_sidecar_unlocked(
                data,
                conversations=[updated_conversation] if updated_conversation else None,
            )
    
    def get_pending_conversations(self) -> list:
        """获取待处理的会话"""
        data = self._get_json_store_snapshot()
        pending = []
        processed_events_by_logical = {
            str(item.get("logical_message_id", "") or ""): item
            for item in self._get_json_processed_events_from_snapshot(data)
            if str(item.get("logical_message_id", "") or "").strip()
        }
        
        conversations = self._get_json_conversations_from_snapshot(data)
        messages = self._get_json_messages_from_snapshot(data)
        for conv in conversations:
            if conv.get("status") == "active":
                has_unprocessed = False
                for m in messages:
                    if m.get("conversation_id") != conv.get("conversation_id"):
                        continue
                    if m.get("direction") != "inbound":
                        continue

                    logical_message_id = str(m.get("logical_message_id", "") or "").strip()
                    event = processed_events_by_logical.get(logical_message_id) if logical_message_id else None
                    if event:
                        event_status = str(event.get("status", "") or "").strip().lower()
                        if event_status not in {"done", "skipped"}:
                            has_unprocessed = True
                            break
                        continue

                    if not m.get("is_processed", True):
                        has_unprocessed = True
                        break
                if has_unprocessed:
                    pending.append(conv)
        
        return pending
    
    def get_last_outbound_message(self, conversation_id: str) -> Optional[dict]:
        """获取会话中最后一条发送的消息"""
        messages = [
            m for m in self._get_conversation_messages_from_json(
                conversation_id,
                exclude_preview_noise=False,
            )
            if m.get("direction") == "outbound"
        ]
        if not messages:
            return None
        messages.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return messages[0]
    
    def get_last_inbound_message(self, conversation_id: str) -> Optional[dict]:
        """获取会话中最后一条收到的消息"""
        messages = [
            m for m in self._get_conversation_messages_from_json(
                conversation_id,
                exclude_preview_noise=False,
            )
            if m.get("direction") == "inbound"
        ]
        if not messages:
            return None
        messages.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return messages[0]
    
    def can_send_reply(
        self,
        conversation_id: str,
        min_interval_seconds: int = 5,
        reply_content: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
        allow_duplicate_content: bool = False,
        customer_name: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> tuple:
        """
        检查是否可以发送回复（频率限制 + 重复内容检测 + 原子预留）

        使用_msg_lock保证检查与预留的原子性，
        防止并发线程同时通过检查导致重复发送。
        预留机制：检查通过后记录待发送内容，后续can_send_reply调用
        会看到这条预留记录，从而阻止并发重复。
        """
        def safe_parse_datetime(dt_str: str) -> Optional[datetime]:
            """安全解析时间字符串"""
            if not dt_str:
                return None
            try:
                return datetime.fromisoformat(dt_str)
            except (ValueError, TypeError):
                return None

        duplicate_reply_window_seconds = max(float(min_interval_seconds), 60.0)

        with self._msg_lock:
            data = self._load_messages_data_unlocked()
            outbound_messages = [
                m for m in self._get_business_messages(data)
                if m.get("conversation_id") == conversation_id and m.get("direction") == "outbound"
            ]
            reservation_messages = [
                r for r in data.get("reply_reservations", [])
                if r.get("conversation_id") == conversation_id
            ]
            recent_outbound_records = outbound_messages + reservation_messages
            last_outbound = None
            if recent_outbound_records:
                recent_outbound_records.sort(key=lambda x: x.get("created_at", ""), reverse=True)
                last_outbound = recent_outbound_records[0]

            if last_outbound:
                last_outbound_time = safe_parse_datetime(last_outbound.get("created_at", ""))
                last_outbound_content = last_outbound.get("content", "")
                last_logical_message_id = str(last_outbound.get("logical_message_id", "")).strip()
                last_source_message_id = str(last_outbound.get("source_message_id", "")).strip()
                time_since_last_outbound = None

                if last_outbound_time:
                    time_since_last_outbound = (datetime.now() - last_outbound_time).total_seconds()

                    if time_since_last_outbound < min_interval_seconds:
                        return (False, f"发送间隔过短({time_since_last_outbound:.1f}s < {min_interval_seconds}s)，防止并发重复")

                if logical_message_id and last_logical_message_id and logical_message_id == last_logical_message_id:
                    return (False, "逻辑消息ID与最近发送记录相同，防止重复")

                if source_message_id and last_source_message_id and source_message_id == last_source_message_id:
                    return (False, "源消息ID与最近发送记录相同，防止重复")

                if reply_content and last_outbound_content and not allow_duplicate_content:
                    within_duplicate_window = (
                        time_since_last_outbound is None
                        or time_since_last_outbound < duplicate_reply_window_seconds
                    )
                    if within_duplicate_window and reply_content == last_outbound_content:
                        return (False, "回复内容与最近发送的完全相同，防止重复")
                    if (
                        within_duplicate_window
                        and len(reply_content) > 20
                        and len(last_outbound_content) > 20
                        and self._calculate_similarity(reply_content, last_outbound_content) >= 0.95
                    ):
                        return (False, "回复内容与最近发送的高度相似，防止重复")

            if reply_content:
                reservation_msg = {
                    "message_id": f"_reservation_{conversation_id}_{hashlib.md5(reply_content.encode('utf-8')).hexdigest()[:8]}",
                    "logical_message_id": logical_message_id,
                    "source_message_id": source_message_id,
                    "conversation_id": conversation_id,
                    "customer_name": customer_name,
                    "customer_id": customer_id,
                    "platform": platform or "douyin",
                    "direction": "outbound",
                    "message_type": "reservation",
                    "content": reply_content,
                    "sender_id": "system",
                    "sender_name": "system",
                    "is_read": True,
                    "is_processed": True,
                    "ai_reply_content": "",
                    "created_at": datetime.now().isoformat()
                }
                data.setdefault("reply_reservations", [])
                data["reply_reservations"] = [
                    r for r in data["reply_reservations"]
                    if r.get("message_id") != reservation_msg["message_id"]
                ]
                data["reply_reservations"].append(reservation_msg)
                self._persist_message_store_and_sidecar_unlocked(data)

            return (True, "可以发送")

    def confirm_reply_sent(self, conversation_id: str, reply_content: str, message_id: str = ""):
        """确认回复已发送，将预留记录替换为正式记录

        Args:
            conversation_id: 会话ID
            reply_content: 回复内容
            message_id: 正式消息ID
        """
        with self._msg_lock:
            data = self._load_messages_data_unlocked()
            reservation_key = f"_reservation_{conversation_id}_{hashlib.md5(reply_content.encode('utf-8')).hexdigest()[:8]}"
            reservation = None
            confirmed_message = None
            for i, msg in enumerate(data.get("reply_reservations", [])):
                if msg.get("message_id") == reservation_key:
                    reservation = data["reply_reservations"].pop(i)
                    break
            if reservation is None:
                for i, msg in enumerate(data["messages"]):
                    if msg.get("message_id") == reservation_key:
                        reservation = data["messages"].pop(i)
                        break
            if reservation is not None:
                confirmed_message = {
                    "message_id": message_id or reservation_key,
                    "logical_message_id": reservation.get("logical_message_id", ""),
                    "source_message_id": reservation.get("source_message_id", ""),
                    "conversation_id": conversation_id,
                    "customer_name": reservation.get("customer_name", ""),
                    "customer_id": reservation.get("customer_id", ""),
                    "platform": reservation.get("platform", "douyin"),
                    "direction": "outbound",
                    "message_type": "text",
                    "content": reply_content,
                    "sender_id": reservation.get("sender_id", "self"),
                    "sender_name": reservation.get("sender_name", "我"),
                    "is_read": True,
                    "is_processed": True,
                    "ai_reply_content": "",
                    "created_at": reservation.get("created_at") if reservation.get("created_at") else datetime.now().isoformat(),
                    "processed_status": "done",
                    "processed_at": datetime.now().isoformat(),
                    "processed_reason": "reply_confirmed",
                }
                data["messages"].append(confirmed_message)
            self._persist_message_store_and_sidecar_unlocked(
                data,
                messages=[confirmed_message] if confirmed_message else None,
            )

    def cancel_reply_reservation(self, conversation_id: str, reply_content: str):
        """取消回复预留记录（发送失败时调用）

        Args:
            conversation_id: 会话ID
            reply_content: 回复内容
        """
        with self._msg_lock:
            data = self._load_messages_data_unlocked()
            reservation_key = f"_reservation_{conversation_id}_{hashlib.md5(reply_content.encode('utf-8')).hexdigest()[:8]}"
            data["reply_reservations"] = [m for m in data.get("reply_reservations", []) if m.get("message_id") != reservation_key]
            data["messages"] = [m for m in data["messages"] if m.get("message_id") != reservation_key]
            self._persist_message_store_and_sidecar_unlocked(data)

    def reply_reservation_context(self, conversation_id: str, reply_content: str):
        """预留机制上下文管理器（自动处理确认/取消，避免遗漏）

        使用方式：
            with db.reply_reservation_context(conv_id, content) as ctx:
                success = send_message(content)
                if not success:
                    raise Exception("发送失败")
                ctx.message_id = "msg_xxx"  # 设置正式消息ID

        Args:
            conversation_id: 会话ID
            reply_content: 回复内容

        Returns:
            ReplyReservationContext: 上下文管理器
        """
        return ReplyReservationContext(self, conversation_id, reply_content)

    def is_duplicate_reply(self, conversation_id: str, content: str, similarity_threshold: float = 0.8) -> bool:
        """检查是否是重复回复"""
        if not conversation_id or conversation_id.endswith('_@') or conversation_id.endswith('_'):
            return False
        data = self._get_json_store_snapshot()
        messages = [
            m for m in self._get_json_messages_from_snapshot(data)
            if m.get("conversation_id") == conversation_id and m.get("direction") == "outbound"
        ]
        if not messages:
            return False
        messages.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        recent_messages = messages[:3]
        for msg in recent_messages:
            existing_content = msg.get("content", "")
            if existing_content == content:
                return True
            if self._calculate_similarity(content, existing_content) >= similarity_threshold:
                return True
        return False

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算两个文本的词级Jaccard相似度（使用jieba分词）"""
        if not text1 or not text2:
            return 0.0
        try:
            from src.common.utils import _jieba_module as jieba
            words1 = set(jieba.cut(text1))
            words2 = set(jieba.cut(text2))
            words1.discard(' ')
            words2.discard(' ')
            if not words1 or not words2:
                return 0.0
            intersection = len(words1 & words2)
            union = len(words1 | words2)
            if union == 0:
                return 0.0
            return intersection / union
        except Exception:
            if len(text1) == len(text2) and text1 == text2:
                return 1.0
            return 0.0

    def is_sent(self, sec_uid: str) -> bool:
        """检查是否已发送私信"""
        data = self._load_data()
        for customer in data.get("customers", []):
            normalized = self._normalize_customer_status_fields(customer)
            if normalized.get("sec_uid") == sec_uid and normalized.get("status") == "sent":
                return True
        return False

    def mark_sent(self, sec_uid: str, platform: str = "douyin"):
        """标记为已发送"""
        self.update_customer_status(sec_uid, platform, "sent")

    def mark_interacted(self, sec_uid: str, platform: str = "douyin"):
        """标记为已互动"""
        self.update_customer_interact_status(sec_uid, platform, "interacted")

    # ========== 发送模式管理 ==========

    def get_send_mode_status(self, platform: str = "douyin") -> dict:
        """获取发送模式状态"""
        data = self._load_data()
        send_modes = data.get("send_modes", {})
        platform_modes = send_modes.get(platform, {})
        default_modes = {
            "dom": {"success_count": 0, "failure_count": 0, "last_success": None, "last_failure": None, "success_rate": 0.0},
            "visual": {"success_count": 0, "failure_count": 0, "last_success": None, "last_failure": None, "success_rate": 0.0},
            "smart": {"success_count": 0, "failure_count": 0, "last_success": None, "last_failure": None, "success_rate": 0.0},
        }
        for mode in default_modes:
            if mode not in platform_modes:
                platform_modes[mode] = default_modes[mode]
            else:
                for key, value in default_modes[mode].items():
                    if key not in platform_modes[mode]:
                        platform_modes[mode][key] = value
        for mode, stats in platform_modes.items():
            total = stats["success_count"] + stats["failure_count"]
            stats["success_rate"] = stats["success_count"] / total if total > 0 else 0.0
        return platform_modes

    def update_send_mode_status(self, platform: str, mode: str, success: bool):
        """更新发送模式状态"""
        with DatabaseManager._lock:
            data = self._load_data_unlocked()
            send_modes = data.get("send_modes", {})
            platform_modes = send_modes.get(platform, {})
            if mode not in platform_modes:
                platform_modes[mode] = {
                    "success_count": 0, "failure_count": 0,
                    "last_success": None, "last_failure": None, "success_rate": 0.0,
                }
            if success:
                platform_modes[mode]["success_count"] += 1
                platform_modes[mode]["last_success"] = datetime.now().isoformat()
            else:
                platform_modes[mode]["failure_count"] += 1
                platform_modes[mode]["last_failure"] = datetime.now().isoformat()
            total = platform_modes[mode]["success_count"] + platform_modes[mode]["failure_count"]
            if total > 0:
                platform_modes[mode]["success_rate"] = platform_modes[mode]["success_count"] / total
            send_modes[platform] = platform_modes
            data["send_modes"] = send_modes
            self._save_data_unlocked(data)
            logger.info(f"更新发送模式状态: {platform}.{mode} -> {'成功' if success else '失败'}")

    def get_best_send_mode(self, platform: str = "douyin") -> str:
        """获取最佳发送模式（根据成功率和最近成功时间选择）"""
        modes = self.get_send_mode_status(platform)
        scores = {}
        for mode, stats in modes.items():
            score = stats["success_rate"]
            if stats["last_success"]:
                try:
                    last_success_time = datetime.fromisoformat(stats["last_success"])
                    time_diff = (datetime.now() - last_success_time).total_seconds()
                    if time_diff < 3600:
                        score += 0.3
                    elif time_diff < 86400:
                        score += 0.1
                except (ValueError, TypeError):
                    pass
            if stats["last_failure"]:
                try:
                    last_failure_time = datetime.fromisoformat(stats["last_failure"])
                    time_diff = (datetime.now() - last_failure_time).total_seconds()
                    if time_diff < 3600:
                        score -= 0.2
                except (ValueError, TypeError):
                    pass
            scores[mode] = score
        if scores:
            best_mode = max(scores, key=scores.get)
            logger.info(f"选择最佳发送模式: {best_mode} (得分: {scores[best_mode]:.2f})")
            return best_mode
        return "dom"

    def reset_send_mode_stats(self, platform: str = "douyin"):
        """重置发送模式统计"""
        with DatabaseManager._lock:
            data = self._load_data_unlocked()
            send_modes = data.get("send_modes", {})
            send_modes[platform] = {
                "dom": {"success_count": 0, "failure_count": 0, "last_success": None, "last_failure": None, "success_rate": 0.0},
                "visual": {"success_count": 0, "failure_count": 0, "last_success": None, "last_failure": None, "success_rate": 0.0},
                "smart": {"success_count": 0, "failure_count": 0, "last_success": None, "last_failure": None, "success_rate": 0.0},
            }
            data["send_modes"] = send_modes
            self._save_data_unlocked(data)
            logger.info(f"重置发送模式统计: {platform}")

    def cleanup_orphan_reservations(self):
        """清理孤立的预留记录（启动时调用，防止重启后预留记录阻止正常回复）

        孤立预留记录产生于：程序崩溃或强制终止时，can_send_reply已创建预留记录
        但confirm_reply_sent或cancel_reply_reservation未被调用。
        这些记录会阻止后续相同内容的回复发送。

        Returns:
            int: 清理的孤立预留记录数量
        """
        cleaned = 0
        with self._msg_lock:
            data = self._load_messages_data_unlocked()
            now = datetime.now()
            removed_ids = []
            for msg in data.get('reply_reservations', []):
                if self._is_reservation_message(msg):
                    mid = msg.get("message_id", "")
                    if mid:
                        removed_ids.append(mid)
            for msg in data.get('messages', []):
                if self._is_reservation_message(msg):
                    mid = msg.get("message_id", "")
                    if mid:
                        removed_ids.append(mid)

            retained_reservations = []
            for msg in data.get('reply_reservations', []):
                created_at_str = msg.get('created_at', '')
                try:
                    if created_at_str:
                        msg_time = datetime.fromisoformat(created_at_str)
                        age_seconds = (now - msg_time).total_seconds()
                        if age_seconds > 60:
                            cleaned += 1
                            continue
                    else:
                        cleaned += 1
                        continue
                except Exception:
                    cleaned += 1
                    continue
                retained_reservations.append(msg)
            data['reply_reservations'] = retained_reservations

            legacy_messages = []
            for msg in data.get('messages', []):
                if self._is_reservation_message(msg):
                    cleaned += 1
                    continue
                legacy_messages.append(msg)
            data['messages'] = legacy_messages

            if cleaned > 0:
                self._persist_message_store_and_sidecar_unlocked(data)
                try:
                    conn = self._get_sqlite_connection()
                    if conn:
                        for mid in removed_ids:
                            conn.execute("DELETE FROM messages WHERE message_id = ?", (mid,))
                        conn.commit()
                        conn.close()
                except Exception as sync_e:
                    logger.debug(f"清理预留记录同步SQLite失败: {sync_e}")
                logger.info(f"清理了 {cleaned} 条孤立预留记录")

        return cleaned

    def get_conversation_history(self, customer_id: str, limit: int = 50) -> list:
        """获取客户会话历史（兼容路由模块调用）"""
        messages = self.get_customer_messages(customer_id, limit=limit)
        return messages if messages else []

    def delete_user(self, sec_uid: str, platform: str = "douyin") -> bool:
        """删除客户记录"""
        with DatabaseManager._lock:
            data = self._load_data_unlocked()
            original_len = len(data.get("customers", []))
            data["customers"] = [
                c for c in data.get("customers", [])
                if not (c.get("sec_uid") == sec_uid and c.get("platform") == platform)
            ]
            if len(data["customers"]) < original_len:
                self._save_data_unlocked(data)
                try:
                    self._init_customer_sqlite()
                    conn = self._get_customer_sqlite_conn()
                    try:
                        with conn:
                            conn.execute(
                                "DELETE FROM customers WHERE sec_uid = ? AND platform = ?",
                                [sec_uid, platform]
                            )
                    finally:
                        conn.close()
                except Exception as e:
                    logger.warning(f"从 SQLite 删除客户失败: {e}")
                logger.info(f"删除客户: {sec_uid} ({platform})")
                return True
            return False

    def get_recent_messages(self, limit: int = 1000) -> list:
        """获取最近的消息（兼容路由模块调用）"""
        return self.get_all_messages(limit=limit)

    async def async_get_all_customers(self, platform: str = None) -> List[dict]:
        """异步获取所有客户，避免阻塞事件循环"""
        import asyncio
        return await asyncio.to_thread(self.get_all_customers, platform)

    async def async_get_customers_page(
        self,
        page: int = 1,
        page_size: int = 20,
        platform: str = None,
        status: str = None,
        interact_status: str = None,
        intent_level: str = None,
        comment_time_start: str = None,
        comment_time_end: str = None,
        created_after: str = None,
        created_by_task_id: str = None,
    ) -> dict:
        """异步分页获取客户列表。"""
        import asyncio
        return await asyncio.to_thread(
            self.get_customers_page,
            page=page,
            page_size=page_size,
            platform=platform,
            status=status,
            interact_status=interact_status,
            intent_level=intent_level,
            comment_time_start=comment_time_start,
            comment_time_end=comment_time_end,
            created_after=created_after,
            created_by_task_id=created_by_task_id,
        )

    async def async_search_customers_page(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        platform: str = None,
        status: str = None,
        interact_status: str = None,
        comment_time_start: str = None,
        comment_time_end: str = None,
        created_after: str = None,
        created_by_task_id: str = None,
    ) -> dict:
        """异步分页搜索客户。"""
        import asyncio
        return await asyncio.to_thread(
            self.search_customers_page,
            keyword=keyword,
            page=page,
            page_size=page_size,
            platform=platform,
            status=status,
            interact_status=interact_status,
            comment_time_start=comment_time_start,
            comment_time_end=comment_time_end,
            created_after=created_after,
            created_by_task_id=created_by_task_id,
        )

    async def async_get_customer(self, customer_id: str) -> Optional[dict]:
        """异步获取单个客户"""
        import asyncio
        return await asyncio.to_thread(self.get_customer, customer_id)

    async def async_get_all_messages(self, limit: int = None) -> list:
        """异步获取所有消息"""
        import asyncio
        return await asyncio.to_thread(self.get_all_messages, limit)

    async def async_get_all_conversations(self) -> list:
        """异步获取所有会话"""
        import asyncio
        return await asyncio.to_thread(self.get_all_conversations)

    async def async_get_pending_customers(self, platform: str = None, limit: int = 10) -> List[dict]:
        """异步获取待发送私信的客户"""
        import asyncio
        return await asyncio.to_thread(self.get_pending_customers, platform, limit)

    async def async_get_conversation_messages(self, conversation_id: str, limit: int = 9999) -> list:
        """异步获取会话消息"""
        import asyncio
        return await asyncio.to_thread(self.get_conversation_messages, conversation_id, limit)


class ReplyReservationContext:
    """回复预留上下文管理器

    自动管理预留记录的生命周期：
    - 进入时：can_send_reply检查+预留
    - 退出时：成功→confirm_reply_sent，异常→cancel_reply_reservation
    """

    def __init__(self, db_manager, conversation_id: str, reply_content: str):
        self._db = db_manager
        self._conversation_id = conversation_id
        self._reply_content = reply_content
        self.message_id = ""
        self.can_send = False
        self.reason = ""

    def __enter__(self):
        self.can_send, self.reason = self._db.can_send_reply(
            self._conversation_id,
            min_interval_seconds=5,
            reply_content=self._reply_content
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None or not self.can_send:
            try:
                self._db.cancel_reply_reservation(self._conversation_id, self._reply_content)
            except Exception:
                pass
        elif self.can_send and self.message_id:
            try:
                self._db.confirm_reply_sent(
                    self._conversation_id,
                    self._reply_content,
                    self.message_id
                )
            except Exception:
                pass
        return False


# 兼容性别名
Database = DatabaseManager
