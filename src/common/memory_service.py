"""
持久化记忆服务

提供用户级和会话级的持久化记忆能力
支持偏好记忆、事实记忆、对话摘要
数据持久化到SQLite，服务重启后记忆不丢失

参考：Mem0 (github.com/mem0ai/mem0) 的记忆分层设计
"""

import json
import time
import sqlite3
import logging
import hashlib
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict

from src.infrastructure.runtime_paths import get_memory_db_path

logger = logging.getLogger(__name__)


@dataclass
class MemoryItem:
    """记忆条目"""
    memory_id: str
    user_id: str
    content: str
    memory_type: str  # fact, preference, summary, conversation
    importance: float = 0.5
    access_count: int = 0
    created_at: float = 0.0
    last_accessed: float = 0.0
    metadata: Dict = field(default_factory=dict)


class MemoryService:
    """
    持久化记忆服务

    特点：
    1. 基于SQLite的持久化存储
    2. 支持事实记忆、偏好记忆、对话摘要
    3. 记忆检索基于关键词匹配
    4. 记忆合并和压缩机制
    5. 重要性衰减和淘汰策略
    """

    DB_PATH = str(get_memory_db_path())

    MEMORY_TYPES = ["fact", "preference", "summary", "conversation"]

    def __init__(self, db_path: str = None):
        """
        初始化记忆服务

        Args:
            db_path: 数据库文件路径
        """
        self.db_path = db_path or self.DB_PATH
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                importance REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                created_at REAL,
                last_accessed REAL,
                metadata TEXT DEFAULT '{}'
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_id ON memories(user_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_type ON memories(memory_type)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_type ON memories(user_id, memory_type)
        """)

        conn.commit()
        conn.close()
        logger.info(f"记忆服务数据库初始化完成: {self.db_path}")

    def add_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str = "fact",
        importance: float = 0.5,
        metadata: Dict = None
    ) -> str:
        """
        添加记忆

        Args:
            user_id: 用户ID
            content: 记忆内容
            memory_type: 记忆类型 (fact/preference/summary/conversation)
            importance: 重要性 (0-1)
            metadata: 额外元数据

        Returns:
            记忆ID
        """
        if memory_type not in self.MEMORY_TYPES:
            memory_type = "fact"

        memory_id = hashlib.md5(f"{user_id}:{content}:{memory_type}".encode()).hexdigest()[:12]

        existing = self._get_by_id(memory_id)
        if existing:
            self._update_access(memory_id)
            return memory_id

        now = time.time()

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT OR REPLACE INTO memories
                (memory_id, user_id, content, memory_type, importance, access_count, created_at, last_accessed, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                memory_id, user_id, content, memory_type,
                importance, 0, now, now,
                json.dumps(metadata or {}, ensure_ascii=False)
            ))
            conn.commit()
            logger.debug(f"添加记忆: [{memory_type}] {content[:50]}...")
        except Exception as e:
            logger.error(f"添加记忆失败: {e}")
        finally:
            conn.close()

        return memory_id

    def search_memories(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        memory_type: str = None
    ) -> List[MemoryItem]:
        """
        搜索相关记忆

        Args:
            user_id: 用户ID
            query: 查询文本
            top_k: 返回数量
            memory_type: 可选的记忆类型过滤

        Returns:
            记忆列表
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            if memory_type:
                cursor.execute("""
                    SELECT memory_id, user_id, content, memory_type, importance, access_count, created_at, last_accessed, metadata
                    FROM memories
                    WHERE user_id = ? AND memory_type = ?
                    ORDER BY importance DESC, last_accessed DESC
                    LIMIT ?
                """, (user_id, memory_type, top_k * 3))
            else:
                cursor.execute("""
                    SELECT memory_id, user_id, content, memory_type, importance, access_count, created_at, last_accessed, metadata
                    FROM memories
                    WHERE user_id = ?
                    ORDER BY importance DESC, last_accessed DESC
                    LIMIT ?
                """, (user_id, top_k * 3))

            rows = cursor.fetchall()

            results = []
            query_lower = query.lower()

            for row in rows:
                item = MemoryItem(
                    memory_id=row[0],
                    user_id=row[1],
                    content=row[2],
                    memory_type=row[3],
                    importance=row[4],
                    access_count=row[5],
                    created_at=row[6],
                    last_accessed=row[7],
                    metadata=json.loads(row[8]) if row[8] else {}
                )

                relevance = self._calculate_relevance(query_lower, item.content.lower())
                if relevance > 0:
                    item.importance *= relevance
                    results.append(item)

            results.sort(key=lambda x: x.importance, reverse=True)

            for item in results[:top_k]:
                self._update_access(item.memory_id)

            return results[:top_k]

        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return []
        finally:
            conn.close()

    def get_user_summary(self, user_id: str) -> str:
        """
        获取用户摘要

        Args:
            user_id: 用户ID

        Returns:
            用户记忆摘要文本
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT content, memory_type FROM memories
                WHERE user_id = ? AND memory_type IN ('fact', 'preference', 'summary')
                ORDER BY importance DESC
                LIMIT 10
            """, (user_id,))

            rows = cursor.fetchall()

            if not rows:
                return ""

            parts = []
            for content, mem_type in rows:
                type_label = {"fact": "事实", "preference": "偏好", "summary": "摘要"}.get(mem_type, "")
                parts.append(f"[{type_label}] {content}")

            return "\n".join(parts)

        except Exception as e:
            logger.error(f"获取用户摘要失败: {e}")
            return ""
        finally:
            conn.close()

    def consolidate_memories(self, user_id: str):
        """
        合并和压缩记忆

        删除重复记忆，合并相似记忆，降低低重要性记忆的权重

        Args:
            user_id: 用户ID
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT memory_id, content, memory_type FROM memories
                WHERE user_id = ?
                ORDER BY created_at
            """, (user_id,))

            rows = cursor.fetchall()
            seen_contents = {}
            duplicates = []

            for memory_id, content, mem_type in rows:
                content_key = content.strip().lower()
                if content_key in seen_contents:
                    duplicates.append(memory_id)
                else:
                    seen_contents[content_key] = memory_id

            for dup_id in duplicates:
                cursor.execute("DELETE FROM memories WHERE memory_id = ?", (dup_id,))

            now = time.time()
            cursor.execute("""
                UPDATE memories
                SET importance = importance * 0.9
                WHERE user_id = ? AND last_accessed < ?
            """, (user_id, now - 7 * 24 * 3600))

            cursor.execute("""
                DELETE FROM memories
                WHERE user_id = ? AND importance < 0.1 AND last_accessed < ?
            """, (user_id, now - 30 * 24 * 3600))

            conn.commit()
            logger.info(f"记忆合并完成: 用户{user_id}, 删除{len(duplicates)}条重复, 衰减旧记忆")

        except Exception as e:
            logger.error(f"记忆合并失败: {e}")
        finally:
            conn.close()

    def _calculate_relevance(self, query: str, content: str) -> float:
        """计算查询与记忆内容的相关性"""
        if not query or not content:
            return 0.0

        query_words = set(query.split())
        content_words = set(content.split())

        overlap = query_words & content_words
        if not overlap:
            for q_word in query_words:
                if len(q_word) >= 2 and q_word in content:
                    overlap.add(q_word)

        if not overlap:
            return 0.0

        return len(overlap) / max(len(query_words), 1)

    def _get_by_id(self, memory_id: str) -> Optional[MemoryItem]:
        """根据ID获取记忆"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT memory_id, user_id, content, memory_type, importance, access_count, created_at, last_accessed, metadata
                FROM memories WHERE memory_id = ?
            """, (memory_id,))

            row = cursor.fetchone()
            if row:
                return MemoryItem(
                    memory_id=row[0], user_id=row[1], content=row[2],
                    memory_type=row[3], importance=row[4], access_count=row[5],
                    created_at=row[6], last_accessed=row[7],
                    metadata=json.loads(row[8]) if row[8] else {}
                )
            return None
        finally:
            conn.close()

    def _update_access(self, memory_id: str):
        """更新记忆访问信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                UPDATE memories
                SET access_count = access_count + 1, last_accessed = ?
                WHERE memory_id = ?
            """, (time.time(), memory_id))
            conn.commit()
        finally:
            conn.close()

    def get_statistics(self, user_id: str = None) -> Dict:
        """获取记忆统计"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            if user_id:
                cursor.execute("SELECT COUNT(*) FROM memories WHERE user_id = ?", (user_id,))
                total = cursor.fetchone()[0]

                cursor.execute("""
                    SELECT memory_type, COUNT(*) FROM memories
                    WHERE user_id = ? GROUP BY memory_type
                """, (user_id,))
                by_type = dict(cursor.fetchall())
            else:
                cursor.execute("SELECT COUNT(*) FROM memories")
                total = cursor.fetchone()[0]

                cursor.execute("SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type")
                by_type = dict(cursor.fetchall())

            return {
                "total_memories": total,
                "by_type": by_type,
                "user_id": user_id
            }
        finally:
            conn.close()


_memory_service: Optional[MemoryService] = None


def get_memory_service(db_path: str = None) -> MemoryService:
    """获取记忆服务单例"""
    global _memory_service
    if _memory_service is None:
        _memory_service = MemoryService(db_path)
    return _memory_service
