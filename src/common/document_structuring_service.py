from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.common.database import DatabaseManager
from src.common.document_structuring_models import (
    DocumentSourceRecord,
    DocumentStructuringJobRecord,
    KnowledgeDraftRecord,
)
from src.common.document_structuring_schema import ensure_document_structuring_tables
from src.common.unified_knowledge_service import build_trigger_keywords, get_unified_knowledge_service
from src.infrastructure.runtime_paths import get_document_structuring_dir
from src.rag.enterprise_ingestion_pipeline import DocumentParser, OCRRequiredError


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_filename(name: str) -> str:
    normalized = str(name or "").strip() or "document.bin"
    return "".join(ch if ch.isalnum() or ch in {".", "-", "_"} else "_" for ch in normalized)


def _load_json_text(value: Any, default: Any) -> Any:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _dump_json_text(value: Any, default: str) -> str:
    try:
        return json.dumps(value if value is not None else json.loads(default), ensure_ascii=False)
    except Exception:
        return default


_KEYWORD_NOISE_LINE_PATTERN = re.compile(
    r"^[【\[\(（]?\s*(?:关键词|关键字|标签|tag|tags)\s*[】\]\)）]?\s*[：:]\s*",
    re.IGNORECASE,
)
class DocumentStructuringService:
    def __init__(
        self,
        *,
        sqlite_path: Optional[str] = None,
        storage_root: Optional[str | Path] = None,
    ) -> None:
        database = DatabaseManager() if sqlite_path is None else None
        self.sqlite_path = str(sqlite_path or database.get_customer_sqlite_path())
        self.storage_root = Path(storage_root or get_document_structuring_dir())
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._parser = DocumentParser()
        self._ensure_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        conn = self._get_conn()
        try:
            with conn:
                ensure_document_structuring_tables(conn)
        finally:
            conn.close()

    def _build_document_storage_path(self, enterprise_id: str, document_source_id: str, filename: str) -> Path:
        suffix = Path(filename).suffix or ".bin"
        safe_name = _safe_filename(Path(filename).stem) + suffix.lower()
        target_dir = self.storage_root / (enterprise_id or "default") / document_source_id
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / safe_name

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> DocumentSourceRecord:
        return DocumentSourceRecord(
            id=row["id"],
            enterprise_id=row["enterprise_id"],
            file_id=row["file_id"],
            content_hash=row["content_hash"],
            original_name=row["original_name"],
            stored_path=row["stored_path"],
            file_type=row["file_type"],
            file_size=int(row["file_size"] or 0),
            category_hint=row["category_hint"] or "other",
            tags=_load_json_text(row["tags_json"], []),
            source_channel=row["source_channel"] or "document_upload",
            upload_status=row["upload_status"] or "uploaded",
            parse_status=row["parse_status"] or "pending",
            structuring_status=row["structuring_status"] or "pending",
            publish_status=row["publish_status"] or "draft_only",
            version_no=int(row["version_no"] or 1),
            uploaded_by=row["uploaded_by"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            draft_count=int(row["draft_count"] or 0),
            published_count=int(row["published_count"] or 0),
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> DocumentStructuringJobRecord:
        return DocumentStructuringJobRecord(
            id=row["id"],
            document_source_id=row["document_source_id"],
            job_type=row["job_type"],
            job_status=row["job_status"] or "pending",
            current_step=row["current_step"] or "",
            progress=float(row["progress"] or 0),
            error_message=row["error_message"] or "",
            metrics=_load_json_text(row["metrics_json"], {}),
            started_at=row["started_at"] or "",
            finished_at=row["finished_at"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    @staticmethod
    def _row_to_draft(row: sqlite3.Row) -> KnowledgeDraftRecord:
        return KnowledgeDraftRecord(
            id=row["id"],
            enterprise_id=row["enterprise_id"],
            document_source_id=row["document_source_id"],
            artifact_id=row["artifact_id"] or "",
            draft_type=row["draft_type"] or "auto",
            question=row["question"] or "",
            answer=row["answer"] or "",
            category=row["category"] or "other",
            domain=row["domain"] or "",
            topic=row["topic"] or "",
            tags=_load_json_text(row["tags_json"], []),
            keywords=_load_json_text(row["keywords_json"], []),
            aliases=_load_json_text(row["aliases_json"], []),
            source_excerpt=row["source_excerpt"] or "",
            source_page_no=int(row["source_page_no"] or 0),
            source_section=row["source_section"] or "",
            confidence=float(row["confidence"] or 0),
            review_status=row["review_status"] or "pending",
            review_comment=row["review_comment"] or "",
            edited_by=row["edited_by"] or "",
            reviewed_by=row["reviewed_by"] or "",
            published_knowledge_id=row["published_knowledge_id"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    @classmethod
    def _clean_keyword_source_text(cls, text: str, *, kind: str = "answer") -> str:
        normalized = cls._strip_qa_marker(text, kind=kind)
        if not normalized:
            return ""
        cleaned_lines: List[str] = []
        for raw_line in normalized.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = str(raw_line or "").strip()
            if not line:
                continue
            if _KEYWORD_NOISE_LINE_PATTERN.match(line):
                continue
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines).strip()

    @classmethod
    def _extract_keywords(cls, question: str, answer: str = "") -> List[str]:
        question_text = cls._clean_keyword_source_text(question, kind="question")
        answer_text = cls._clean_keyword_source_text(answer, kind="answer")
        return build_trigger_keywords(question_text, answer_text)

    @staticmethod
    def _generate_question_from_content(content: str) -> str:
        text = str(content or "").strip()
        if not text:
            return ""
        keywords = DocumentStructuringService._extract_keywords(text)
        if keywords:
            return f"关于{keywords[0]}的说明"
        if len(text) > 30:
            return text[:30] + "..."
        return text

    @staticmethod
    def _is_valid_qa(question: str, answer: str) -> bool:
        if not question or not answer:
            return False
        normalized_question = str(question or "").strip()
        normalized_answer = str(answer or "").strip()
        if len(normalized_question) < 3 or len(normalized_answer) < 5:
            return False
        separator_chars = "─═_*#`. \n\r\t-—–"
        if normalized_question[0] in "─═_":
            return False
        if len(normalized_question.strip(separator_chars)) < 3:
            return False
        if sum(normalized_question.count(ch) for ch in "─═_") > len(normalized_question) * 0.5:
            return False
        if len(normalized_answer.strip(separator_chars)) < 5:
            return False
        invalid_patterns = (r"^[─═_\-—–]+$", r"^\.{3,}$", r"^\*+$", r"^#+$")
        return not any(
            re.match(pattern, normalized_question) or re.match(pattern, normalized_answer)
            for pattern in invalid_patterns
        )

    @staticmethod
    def _strip_qa_marker(text: str, *, kind: str) -> str:
        normalized = str(text or "").strip()
        if not normalized:
            return ""
        if kind == "question":
            patterns = [
                r"^[\[\(（【]\s*(?:问|问题|q|ｑ)\s*\d*\s*[\]\)）】]\s*",
                r"^(?:问|问题|q|ｑ)\s*\d*\s*[：:．\.\-、]\s*",
            ]
        else:
            patterns = [
                r"^[\[\(（【]\s*(?:答|答案|a|ａ)\s*\d*\s*[\]\)）】]\s*",
                r"^(?:答|答案|a|ａ)\s*\d*\s*[：:．\.\-、]\s*",
            ]
        cleaned = normalized
        for pattern in patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    @staticmethod
    def _extract_prefixed_line_value(line: str, *, kind: str) -> Optional[str]:
        normalized = str(line or "").strip()
        if not normalized:
            return None
        if kind == "question":
            tokens = r"(?:问|问题|q|ｑ)"
        else:
            tokens = r"(?:答|答案|a|ａ)"
        patterns = [
            rf"^[\[\(（【]\s*{tokens}\s*\d*\s*[\]\)）】]\s*(.+)$",
            rf"^{tokens}\s*\d*\s*[：:．\.\-、]\s*(.+)$",
        ]
        for pattern in patterns:
            match = re.match(pattern, normalized, flags=re.IGNORECASE)
            if match:
                return str(match.group(1) or "").strip()
        return None

    def _extract_explicit_qa_blocks(self, content: str) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()

        def append_block(question: str, answer: str, start: int, end: int) -> None:
            normalized_question = self._strip_qa_marker(question, kind="question")
            normalized_answer = self._strip_qa_marker(answer, kind="answer")
            pair = (normalized_question, normalized_answer)
            if pair in seen_pairs or not self._is_valid_qa(normalized_question, normalized_answer):
                return
            seen_pairs.add(pair)
            blocks.append(
                {
                    "question": normalized_question,
                    "answer": normalized_answer,
                    "content": f"问：{normalized_question}\n答：{normalized_answer}",
                    "start": max(int(start or 0), 0),
                    "end": max(int(end or 0), 0),
                    "mode": "explicit",
                }
            )

        patterns = [
            r"(?:Q|Ｑ)\s*\d*\s*[：:]\s*(.+?)\s*(?:A|Ａ)\s*\d*\s*[：:]\s*(.+?)(?=(?:Q|Ｑ)\s*\d*\s*[：:]|$)",
            r"问题\s*\d*\s*[：:]\s*(.+?)\s*答案\s*\d*\s*[：:]\s*(.+?)(?=问题\s*\d*\s*[：:]|$)",
            r"问\s*\d*\s*[：:]\s*(.+?)\s*答\s*\d*\s*[：:]\s*(.+?)(?=问\s*\d*\s*[：:]|$)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, str(content or ""), re.DOTALL | re.IGNORECASE):
                append_block(match.group(1), match.group(2), match.start(), match.end())
        if blocks:
            return sorted(blocks, key=lambda item: item["start"])

        lines = str(content or "").splitlines(keepends=True)
        current_question: Optional[str] = None
        current_answer: List[str] = []
        block_start: Optional[int] = None
        cursor = 0
        for raw_line in lines:
            line = raw_line.strip()
            line_start = cursor
            cursor += len(raw_line)
            if not line:
                continue
            question_text = self._extract_prefixed_line_value(line, kind="question")
            if question_text is not None:
                if current_question and current_answer and block_start is not None:
                    append_block(current_question, "\n".join(current_answer), block_start, line_start)
                current_question = question_text
                current_answer = []
                block_start = line_start
                continue
            answer_text = self._extract_prefixed_line_value(line, kind="answer")
            if answer_text is not None:
                if current_question:
                    current_answer.append(answer_text)
                continue
            if current_question:
                current_answer.append(line)

        if current_question and current_answer and block_start is not None:
            append_block(current_question, "\n".join(current_answer), block_start, cursor)
        return sorted(blocks, key=lambda item: item["start"])

    def _extract_qa_pairs(self, content: str, *, max_drafts: int = 10) -> List[Dict[str, Any]]:
        explicit_blocks = self._extract_explicit_qa_blocks(content)
        if explicit_blocks:
            return explicit_blocks[:max_drafts]

        qa_pairs: List[Dict[str, Any]] = []
        lines = str(content or "").split("\n")
        current_title: Optional[str] = None
        current_content: List[str] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            if len(line) < 50 and (
                line.endswith("？")
                or line.endswith("?")
                or line.startswith(("如何", "怎么", "什么是", "为什么", "能不能", "是否"))
            ):
                if current_title and current_content:
                    answer = self._strip_qa_marker("\n".join(current_content), kind="answer")
                    question = self._strip_qa_marker(current_title, kind="question")
                    if self._is_valid_qa(question, answer):
                        qa_pairs.append(
                            {
                                "question": question,
                                "answer": answer,
                                "content": f"问：{question}\n答：{answer}",
                                "start": 0,
                                "end": 0,
                                "mode": "title_section",
                            }
                        )
                current_title = self._strip_qa_marker(line, kind="question")
                current_content = []
                continue
            if current_title:
                current_content.append(self._strip_qa_marker(line, kind="answer"))
            elif len(line) > 50:
                question = self._generate_question_from_content(line)
                if self._is_valid_qa(question, line):
                    qa_pairs.append(
                        {
                            "question": question,
                            "answer": line,
                            "content": f"问：{question}\n答：{line}",
                            "start": 0,
                            "end": 0,
                            "mode": "long_line",
                        }
                    )
        if current_title and current_content:
            answer = self._strip_qa_marker("\n".join(current_content), kind="answer")
            question = self._strip_qa_marker(current_title, kind="question")
            if self._is_valid_qa(question, answer):
                qa_pairs.append(
                    {
                        "question": question,
                        "answer": answer,
                        "content": f"问：{question}\n答：{answer}",
                        "start": 0,
                        "end": 0,
                        "mode": "title_section",
                    }
                )
        if qa_pairs:
            return qa_pairs[:max_drafts]

        paragraphs = [para.strip() for para in str(content or "").split("\n\n") if para.strip()]
        for para in paragraphs:
            if len(para) <= 50:
                continue
            question = self._generate_question_from_content(para)
            if self._is_valid_qa(question, para):
                qa_pairs.append(
                    {
                        "question": question,
                        "answer": para,
                        "content": f"问：{question}\n答：{para}",
                        "start": 0,
                        "end": 0,
                        "mode": "paragraph",
                    }
                )
            if len(qa_pairs) >= max_drafts:
                break
        return qa_pairs[:max_drafts]

    @staticmethod
    def _preclean_parsed_text(text: str, *, file_type: str = "") -> str:
        normalized_file_type = str(file_type or "").lower()
        cleaned = str(text or "").replace("\ufeff", "")
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
        cleaned = re.sub(r"[ \u3000]+", " ", cleaned)
        cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = re.sub(r"(?m)^第\s*\d+\s*页(?:\s*/\s*共\s*\d+\s*页)?\s*$", "", cleaned)
        cleaned = re.sub(r"(?m)^\s*[—_=]{4,}\s*$", "", cleaned)
        deduped_lines: List[str] = []
        previous_line = ""
        for raw_line in cleaned.split("\n"):
            line = raw_line.strip()
            if (
                line == previous_line
                and line
                and len(line) <= 80
                and normalized_file_type in {".pdf", ".docx", ".doc"}
            ):
                continue
            deduped_lines.append(line)
            previous_line = line or previous_line
        cleaned = "\n".join(deduped_lines)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    def _create_job(
        self,
        conn: sqlite3.Connection,
        *,
        document_source_id: str,
        job_type: str,
        job_status: str,
        current_step: str,
        progress: float,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> DocumentStructuringJobRecord:
        now = _utc_now_iso()
        job = DocumentStructuringJobRecord(
            id=f"job_{uuid.uuid4().hex[:16]}",
            document_source_id=document_source_id,
            job_type=job_type,
            job_status=job_status,
            current_step=current_step,
            progress=progress,
            metrics=dict(metrics or {}),
            created_at=now,
            updated_at=now,
            started_at=now if progress > 0 else "",
            finished_at=now if job_status == "completed" else "",
        )
        conn.execute(
            """
            INSERT INTO document_structuring_jobs (
                id, document_source_id, job_type, job_status, current_step,
                progress, error_message, metrics_json, started_at, finished_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.document_source_id,
                job.job_type,
                job.job_status,
                job.current_step,
                job.progress,
                job.error_message,
                json.dumps(job.metrics, ensure_ascii=False),
                job.started_at,
                job.finished_at,
                job.created_at,
                job.updated_at,
            ),
        )
        return job

    def _get_document_row(
        self,
        conn: sqlite3.Connection,
        *,
        document_source_id: str,
        enterprise_id: str = "",
    ) -> Optional[sqlite3.Row]:
        params: List[Any] = [document_source_id]
        sql = "SELECT * FROM document_sources WHERE id = ?"
        if enterprise_id:
            sql += " AND enterprise_id = ?"
            params.append(enterprise_id)
        return conn.execute(sql + " LIMIT 1", params).fetchone()

    def _update_job_status(
        self,
        conn: sqlite3.Connection,
        *,
        document_source_id: str,
        job_type: str,
        job_status: str,
        current_step: str,
        progress: float,
        error_message: str = "",
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = _utc_now_iso()
        row = conn.execute(
            """
            SELECT id, started_at
            FROM document_structuring_jobs
            WHERE document_source_id = ? AND job_type = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (document_source_id, job_type),
        ).fetchone()
        metrics_json = _dump_json_text(metrics or {}, "{}")
        started_at = now if (row is None or not row["started_at"]) else row["started_at"]
        finished_at = now if job_status in {"completed", "failed", "cancelled"} else ""
        if row is None:
            self._create_job(
                conn,
                document_source_id=document_source_id,
                job_type=job_type,
                job_status=job_status,
                current_step=current_step,
                progress=progress,
                metrics=metrics,
            )
            return
        conn.execute(
            """
            UPDATE document_structuring_jobs
            SET job_status = ?, current_step = ?, progress = ?, error_message = ?,
                metrics_json = ?, started_at = ?, finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                job_status,
                current_step,
                progress,
                error_message,
                metrics_json,
                started_at,
                finished_at,
                now,
                row["id"],
            ),
        )

    def _replace_artifact(
        self,
        conn: sqlite3.Connection,
        *,
        document_source_id: str,
        artifact_type: str,
        content_text: str,
        content_json: Optional[Dict[str, Any]] = None,
        page_no: int = 0,
        section_title: str = "",
        block_index: int = 0,
        parser_name: str = "",
        parser_version: str = "",
        quality_score: float = 0.0,
        review_required: bool = False,
    ) -> str:
        existing = conn.execute(
            """
            SELECT id
            FROM document_artifacts
            WHERE document_source_id = ? AND artifact_type = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (document_source_id, artifact_type),
        ).fetchone()
        now = _utc_now_iso()
        artifact_id = str(existing["id"]) if existing is not None else f"artifact_{uuid.uuid4().hex[:16]}"
        if existing is not None:
            conn.execute("DELETE FROM document_artifacts WHERE id = ?", (artifact_id,))
        conn.execute(
            """
            INSERT INTO document_artifacts (
                id, document_source_id, artifact_type, content_text, content_json,
                page_no, section_title, block_index, parser_name, parser_version,
                quality_score, review_required, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                document_source_id,
                artifact_type,
                str(content_text or ""),
                _dump_json_text(content_json or {}, "{}"),
                int(page_no or 0),
                str(section_title or ""),
                int(block_index or 0),
                str(parser_name or ""),
                str(parser_version or ""),
                float(quality_score or 0),
                1 if review_required else 0,
                now,
            ),
        )
        return artifact_id

    def _get_draft_row(
        self,
        conn: sqlite3.Connection,
        *,
        draft_id: str,
        enterprise_id: str = "",
    ) -> Optional[sqlite3.Row]:
        params: List[Any] = [draft_id]
        sql = "SELECT * FROM knowledge_drafts WHERE id = ?"
        if enterprise_id:
            sql += " AND enterprise_id = ?"
            params.append(enterprise_id)
        return conn.execute(sql + " LIMIT 1", params).fetchone()

    def _append_draft_version(
        self,
        conn: sqlite3.Connection,
        *,
        draft_row: sqlite3.Row,
        change_type: str,
        changed_by: str = "",
    ) -> None:
        draft_id = str(draft_row["id"] or "").strip()
        if not draft_id:
            return
        version_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_draft_versions WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()[0]
        snapshot = {
            "question": draft_row["question"],
            "answer": draft_row["answer"],
            "category": draft_row["category"],
            "domain": draft_row["domain"],
            "topic": draft_row["topic"],
            "tags": _load_json_text(draft_row["tags_json"], []),
            "keywords": _load_json_text(draft_row["keywords_json"], []),
            "aliases": _load_json_text(draft_row["aliases_json"], []),
            "review_status": draft_row["review_status"] or "",
            "review_comment": draft_row["review_comment"] or "",
            "published_knowledge_id": draft_row["published_knowledge_id"] or "",
            "updated_at": draft_row["updated_at"] or "",
        }
        conn.execute(
            """
            INSERT INTO knowledge_draft_versions (
                id, draft_id, version_no, snapshot_json, change_type, changed_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"draftver_{uuid.uuid4().hex[:16]}",
                draft_id,
                int(version_count or 0) + 1,
                _dump_json_text(snapshot, "{}"),
                str(change_type or "update"),
                str(changed_by or "").strip(),
                _utc_now_iso(),
            ),
        )

    def _refresh_document_publish_status(self, conn: sqlite3.Connection, *, document_source_id: str) -> None:
        counts = conn.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                SUM(CASE WHEN published_knowledge_id != '' THEN 1 ELSE 0 END) AS published_count
            FROM knowledge_drafts
            WHERE document_source_id = ?
            """,
            (document_source_id,),
        ).fetchone()
        total_count = int((counts["total_count"] if counts else 0) or 0)
        published_count = int((counts["published_count"] if counts else 0) or 0)
        if total_count == 0 or published_count == 0:
            publish_status = "draft_only"
        elif published_count >= total_count:
            publish_status = "published"
        else:
            publish_status = "pending_publish"
        conn.execute(
            "UPDATE document_sources SET publish_status = ?, updated_at = ? WHERE id = ?",
            (publish_status, _utc_now_iso(), document_source_id),
        )

    def _collect_document_knowledge_item_ids(
        self,
        conn: sqlite3.Connection,
        *,
        document_source_id: str,
    ) -> List[str]:
        normalized_document_source_id = str(document_source_id or "").strip()
        if not normalized_document_source_id:
            return []
        knowledge_item_ids: set[str] = set()
        published_rows = conn.execute(
            """
            SELECT published_knowledge_id
            FROM knowledge_drafts
            WHERE document_source_id = ? AND published_knowledge_id != ''
            """,
            (normalized_document_source_id,),
        ).fetchall()
        for row in published_rows:
            item_id = str(row["published_knowledge_id"] or "").strip()
            if item_id:
                knowledge_item_ids.add(item_id)
        linked_rows = conn.execute(
            """
            SELECT knowledge_item_id
            FROM published_knowledge_links
            WHERE document_source_id = ?
            """,
            (normalized_document_source_id,),
        ).fetchall()
        for row in linked_rows:
            item_id = str(row["knowledge_item_id"] or "").strip()
            if item_id:
                knowledge_item_ids.add(item_id)
        knowledge_service = get_unified_knowledge_service()
        for item in list(getattr(knowledge_service, "knowledge_items", []) or []):
            metadata = dict(getattr(item, "metadata", {}) or {})
            if str(metadata.get("document_source_id") or "").strip() != normalized_document_source_id:
                continue
            item_id = str(getattr(item, "id", "") or "").strip()
            if item_id:
                knowledge_item_ids.add(item_id)
        return sorted(knowledge_item_ids)

    def _upsert_published_knowledge_record(
        self,
        conn: sqlite3.Connection,
        *,
        knowledge_item_id: str,
        row: sqlite3.Row,
        metadata: Dict[str, Any],
        published_at: str,
    ) -> None:
        normalized_knowledge_item_id = str(knowledge_item_id or "").strip()
        if not normalized_knowledge_item_id:
            return
        conn.execute(
            """
            INSERT INTO published_knowledge_records (
                knowledge_item_id, enterprise_id, draft_id, document_source_id,
                question, answer, category, domain, topic,
                tags_json, keywords_json, aliases_json, metadata_json,
                source_excerpt, source_page_no, source_section,
                original_name, file_type, published_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(knowledge_item_id) DO UPDATE SET
                enterprise_id = excluded.enterprise_id,
                draft_id = excluded.draft_id,
                document_source_id = excluded.document_source_id,
                question = excluded.question,
                answer = excluded.answer,
                category = excluded.category,
                domain = excluded.domain,
                topic = excluded.topic,
                tags_json = excluded.tags_json,
                keywords_json = excluded.keywords_json,
                aliases_json = excluded.aliases_json,
                metadata_json = excluded.metadata_json,
                source_excerpt = excluded.source_excerpt,
                source_page_no = excluded.source_page_no,
                source_section = excluded.source_section,
                original_name = excluded.original_name,
                file_type = excluded.file_type,
                published_at = excluded.published_at,
                updated_at = excluded.updated_at
            """,
            (
                normalized_knowledge_item_id,
                row["enterprise_id"] or "",
                row["id"] or "",
                row["document_source_id"] or "",
                row["question"] or "",
                row["answer"] or "",
                row["category"] or "other",
                row["domain"] or "",
                row["topic"] or "",
                row["tags_json"] or "[]",
                row["keywords_json"] or "[]",
                row["aliases_json"] or "[]",
                _dump_json_text(metadata, "{}"),
                row["source_excerpt"] or "",
                int(row["source_page_no"] or 0),
                row["source_section"] or "",
                row["original_name"] or "",
                row["file_type"] or "",
                published_at,
                published_at,
            ),
        )

    def _cleanup_document_regeneration_state(
        self,
        conn: sqlite3.Connection,
        *,
        document_source_id: str,
    ) -> Dict[str, Any]:
        normalized_document_source_id = str(document_source_id or "").strip()
        if not normalized_document_source_id:
            return {
                "deleted_draft_count": 0,
                "deleted_version_count": 0,
                "deleted_link_count": 0,
                "deleted_published_record_count": 0,
                "deleted_knowledge_item_ids": [],
            }
        draft_rows = conn.execute(
            "SELECT id FROM knowledge_drafts WHERE document_source_id = ?",
            (normalized_document_source_id,),
        ).fetchall()
        draft_ids = [str(row["id"] or "").strip() for row in draft_rows if str(row["id"] or "").strip()]
        link_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM published_knowledge_links WHERE document_source_id = ?",
                (normalized_document_source_id,),
            ).fetchone()[0]
            or 0
        )
        published_record_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM published_knowledge_records WHERE document_source_id = ?",
                (normalized_document_source_id,),
            ).fetchone()[0]
            or 0
        )
        knowledge_item_ids = self._collect_document_knowledge_item_ids(
            conn,
            document_source_id=normalized_document_source_id,
        )
        deleted_knowledge_item_ids: List[str] = []
        if knowledge_item_ids:
            knowledge_service = get_unified_knowledge_service()
            delete_item = getattr(knowledge_service, "delete_item", None)
            if not callable(delete_item):
                raise ValueError("统一知识库未提供 delete_item，无法清理文档历史发布数据")
            for item_id in knowledge_item_ids:
                deleted = delete_item(item_id)
                if deleted is not False:
                    deleted_knowledge_item_ids.append(item_id)
        version_count = 0
        if draft_ids:
            placeholders = ",".join("?" for _ in draft_ids)
            version_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM knowledge_draft_versions WHERE draft_id IN ({placeholders})",
                    draft_ids,
                ).fetchone()[0]
                or 0
            )
            conn.execute(
                f"DELETE FROM knowledge_draft_versions WHERE draft_id IN ({placeholders})",
                draft_ids,
            )
        conn.execute(
            "DELETE FROM published_knowledge_links WHERE document_source_id = ?",
            (normalized_document_source_id,),
        )
        conn.execute(
            "DELETE FROM published_knowledge_records WHERE document_source_id = ?",
            (normalized_document_source_id,),
        )
        conn.execute(
            "DELETE FROM knowledge_drafts WHERE document_source_id = ?",
            (normalized_document_source_id,),
        )
        return {
            "deleted_draft_count": len(draft_ids),
            "deleted_version_count": version_count,
            "deleted_link_count": link_count,
            "deleted_published_record_count": published_record_count,
            "deleted_knowledge_item_ids": deleted_knowledge_item_ids,
        }

    def delete_document(
        self,
        document_source_id: str,
        *,
        enterprise_id: str = "",
    ) -> Dict[str, Any]:
        normalized_document_source_id = str(document_source_id or "").strip()
        normalized_enterprise_id = str(enterprise_id or "").strip()
        if not normalized_document_source_id:
            raise ValueError("document_source_id 不能为空")

        with self._lock:
            conn = self._get_conn()
            storage_dir = Path()
            try:
                with conn:
                    row = self._get_document_row(
                        conn,
                        document_source_id=normalized_document_source_id,
                        enterprise_id=normalized_enterprise_id,
                    )
                    if row is None:
                        raise ValueError("文档不存在")
                    stored_path = Path(str(row["stored_path"] or "").strip()) if str(row["stored_path"] or "").strip() else Path()
                    storage_dir = stored_path.parent if stored_path else (
                        self.storage_root / (str(row["enterprise_id"] or "").strip() or "default") / normalized_document_source_id
                    )
                    artifact_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM document_artifacts WHERE document_source_id = ?",
                            (normalized_document_source_id,),
                        ).fetchone()[0]
                        or 0
                    )
                    job_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM document_structuring_jobs WHERE document_source_id = ?",
                            (normalized_document_source_id,),
                        ).fetchone()[0]
                        or 0
                    )
                    cleanup_summary = self._cleanup_document_regeneration_state(
                        conn,
                        document_source_id=normalized_document_source_id,
                    )
                    conn.execute(
                        "DELETE FROM document_artifacts WHERE document_source_id = ?",
                        (normalized_document_source_id,),
                    )
                    conn.execute(
                        "DELETE FROM document_structuring_jobs WHERE document_source_id = ?",
                        (normalized_document_source_id,),
                    )
                    conn.execute(
                        "DELETE FROM document_sources WHERE id = ?",
                        (normalized_document_source_id,),
                    )

                if storage_dir:
                    shutil.rmtree(storage_dir, ignore_errors=True)

                return {
                    "document_source_id": normalized_document_source_id,
                    "deleted_artifact_count": artifact_count,
                    "deleted_job_count": job_count,
                    "cleanup_summary": cleanup_summary,
                    "deleted_storage_dir": str(storage_dir) if storage_dir else "",
                    "status": "deleted",
                }
            finally:
                conn.close()

    def _build_default_draft(self, *, document_name: str, content: str, category_hint: str) -> List[Dict[str, Any]]:
        text = str(content or "").strip()
        if len(text) < 20:
            return []
        summary = text[:500] + "..." if len(text) > 500 else text
        question = f"关于{Path(document_name).stem or '该文档'}的内容"
        if not self._is_valid_qa(question, summary):
            return []
        return [
            {
                "question": question,
                "answer": summary,
                "content": f"问：{question}\n答：{summary}",
                "mode": "default_summary",
                "category": category_hint,
            }
        ]

    def parse_document(
        self,
        document_source_id: str,
        *,
        enterprise_id: str = "",
        parser_name: str = "document_parser",
    ) -> Dict[str, Any]:
        normalized_document_source_id = str(document_source_id or "").strip()
        if not normalized_document_source_id:
            raise ValueError("document_source_id 不能为空")
        normalized_enterprise_id = str(enterprise_id or "").strip()
        with self._lock:
            conn = self._get_conn()
            try:
                with conn:
                    row = self._get_document_row(
                        conn,
                        document_source_id=normalized_document_source_id,
                        enterprise_id=normalized_enterprise_id,
                    )
                    if row is None:
                        raise ValueError("文档不存在")
                    self._update_job_status(
                        conn,
                        document_source_id=normalized_document_source_id,
                        job_type="parse",
                        job_status="processing",
                        current_step="正在解析文档",
                        progress=0.2,
                    )
                    conn.execute(
                        "UPDATE document_sources SET parse_status = ?, updated_at = ? WHERE id = ?",
                        ("processing", _utc_now_iso(), normalized_document_source_id),
                    )
                    file_path = str(row["stored_path"] or "").strip()
                    if not file_path or not Path(file_path).exists():
                        raise ValueError("原始文档文件不存在")
                    raw_text = self._parser.parse(file_path)
                    cleaned_text = self._preclean_parsed_text(
                        raw_text,
                        file_type=row["file_type"] or "",
                    )
                    artifact_id = self._replace_artifact(
                        conn,
                        document_source_id=normalized_document_source_id,
                        artifact_type="parsed_text",
                        content_text=cleaned_text,
                        content_json={
                            "original_name": row["original_name"],
                            "text_length": len(cleaned_text),
                        },
                        parser_name=parser_name,
                        parser_version="1.0",
                        quality_score=1.0 if cleaned_text else 0.0,
                    )
                    self._update_job_status(
                        conn,
                        document_source_id=normalized_document_source_id,
                        job_type="parse",
                        job_status="completed",
                        current_step="文档解析完成",
                        progress=1.0,
                        metrics={"artifact_id": artifact_id, "text_length": len(cleaned_text)},
                    )
                    conn.execute(
                        """
                        UPDATE document_sources
                        SET parse_status = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        ("completed", _utc_now_iso(), normalized_document_source_id),
                    )
                    return {
                        "document_source_id": normalized_document_source_id,
                        "artifact_id": artifact_id,
                        "text_length": len(cleaned_text),
                        "status": "completed",
                    }
            except OCRRequiredError as exc:
                with conn:
                    self._update_job_status(
                        conn,
                        document_source_id=normalized_document_source_id,
                        job_type="parse",
                        job_status="failed",
                        current_step="等待 OCR",
                        progress=0.0,
                        error_message=str(exc),
                    )
                    conn.execute(
                        "UPDATE document_sources SET parse_status = ?, updated_at = ? WHERE id = ?",
                        ("pending_ocr", _utc_now_iso(), normalized_document_source_id),
                    )
                raise ValueError(str(exc)) from exc
            except Exception as exc:
                with conn:
                    self._update_job_status(
                        conn,
                        document_source_id=normalized_document_source_id,
                        job_type="parse",
                        job_status="failed",
                        current_step="文档解析失败",
                        progress=0.0,
                        error_message=str(exc),
                    )
                    conn.execute(
                        "UPDATE document_sources SET parse_status = ?, updated_at = ? WHERE id = ?",
                        ("failed", _utc_now_iso(), normalized_document_source_id),
                    )
                raise
            finally:
                conn.close()

    def structure_document(
        self,
        document_source_id: str,
        *,
        enterprise_id: str = "",
        structurer_type: str = "auto",
        draft_only: bool = True,
        max_drafts: int = 500,
        regenerate: bool = False,
    ) -> Dict[str, Any]:
        normalized_document_source_id = str(document_source_id or "").strip()
        normalized_enterprise_id = str(enterprise_id or "").strip()
        normalized_structurer_type = str(structurer_type or "").strip().lower() or "auto"
        safe_max_drafts = max(1, min(int(max_drafts or 500), 1000))
        if not normalized_document_source_id:
            raise ValueError("document_source_id 不能为空")

        with self._lock:
            precheck_conn = self._get_conn()
            try:
                row = self._get_document_row(
                    precheck_conn,
                    document_source_id=normalized_document_source_id,
                    enterprise_id=normalized_enterprise_id,
                )
            finally:
                precheck_conn.close()

            if row is None:
                raise ValueError("文档不存在")
            if str(row["parse_status"] or "").strip().lower() != "completed":
                self.parse_document(
                    normalized_document_source_id,
                    enterprise_id=normalized_enterprise_id,
                )

            conn = self._get_conn()
            try:
                with conn:
                    row = self._get_document_row(
                        conn,
                        document_source_id=normalized_document_source_id,
                        enterprise_id=normalized_enterprise_id,
                    )
                    if row is None:
                        raise ValueError("文档不存在")
                    artifact_row = conn.execute(
                        """
                        SELECT *
                        FROM document_artifacts
                        WHERE document_source_id = ? AND artifact_type = 'parsed_text'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (normalized_document_source_id,),
                    ).fetchone()
                    if artifact_row is None:
                        raise ValueError("缺少解析产物，无法生成草稿")
                    parsed_text = str(artifact_row["content_text"] or "").strip()
                    self._update_job_status(
                        conn,
                        document_source_id=normalized_document_source_id,
                        job_type="structure",
                        job_status="processing",
                        current_step="正在生成知识草稿",
                        progress=0.2,
                    )
                    conn.execute(
                        "UPDATE document_sources SET structuring_status = ?, updated_at = ? WHERE id = ?",
                        ("processing", _utc_now_iso(), normalized_document_source_id),
                    )
                    cleanup_summary: Dict[str, Any] = {}
                    if regenerate:
                        cleanup_summary = self._cleanup_document_regeneration_state(
                            conn,
                            document_source_id=normalized_document_source_id,
                        )
                    category_hint = str(row["category_hint"] or "").strip() or "other"
                    draft_blocks = self._extract_qa_pairs(parsed_text, max_drafts=safe_max_drafts)
                    if not draft_blocks:
                        draft_blocks = self._build_default_draft(
                            document_name=str(row["original_name"] or "").strip(),
                            content=parsed_text,
                            category_hint=category_hint,
                        )
                    draft_blocks = draft_blocks[:safe_max_drafts]
                    now = _utc_now_iso()
                    created_ids: List[str] = []
                    for block in draft_blocks:
                        draft_id = f"draft_{uuid.uuid4().hex[:16]}"
                        question = str(block.get("question") or "").strip()
                        answer = str(block.get("answer") or "").strip()
                        mode = str(block.get("mode") or normalized_structurer_type or "auto")
                        confidence = 0.95 if mode == "explicit" else 0.75 if mode in {"title_section", "long_line"} else 0.6
                        keywords = self._extract_keywords(question, answer)
                        conn.execute(
                            """
                            INSERT INTO knowledge_drafts (
                                id, enterprise_id, document_source_id, artifact_id, draft_type,
                                question, answer, category, domain, topic, tags_json, keywords_json,
                                aliases_json, source_excerpt, source_page_no, source_section, confidence,
                                review_status, review_comment, edited_by, reviewed_by,
                                published_knowledge_id, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                draft_id,
                                row["enterprise_id"],
                                normalized_document_source_id,
                                artifact_row["id"],
                                mode if normalized_structurer_type == "auto" else normalized_structurer_type,
                                question,
                                answer,
                                category_hint,
                                "",
                                "",
                                row["tags_json"] or "[]",
                                _dump_json_text(keywords, "[]"),
                                "[]",
                                answer[:500],
                                0,
                                "",
                                confidence,
                                "pending",
                                "",
                                "",
                                "",
                                "",
                                now,
                                now,
                            ),
                        )
                        created_ids.append(draft_id)
                    publish_status = "draft_only" if draft_only else "pending_publish"
                    self._update_job_status(
                        conn,
                        document_source_id=normalized_document_source_id,
                        job_type="structure",
                        job_status="completed",
                        current_step="知识草稿生成完成",
                        progress=1.0,
                        metrics={
                            "draft_count": len(created_ids),
                            "structurer_type": normalized_structurer_type,
                            "cleanup_summary": cleanup_summary,
                        },
                    )
                    conn.execute(
                        """
                        UPDATE document_sources
                        SET structuring_status = ?, publish_status = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        ("completed", publish_status, _utc_now_iso(), normalized_document_source_id),
                    )
                    return {
                        "document_source_id": normalized_document_source_id,
                        "draft_ids": created_ids,
                        "draft_count": len(created_ids),
                        "status": "completed",
                    }
            except Exception as exc:
                with conn:
                    self._update_job_status(
                        conn,
                        document_source_id=normalized_document_source_id,
                        job_type="structure",
                        job_status="failed",
                        current_step="草稿生成失败",
                        progress=0.0,
                        error_message=str(exc),
                    )
                    conn.execute(
                        "UPDATE document_sources SET structuring_status = ?, updated_at = ? WHERE id = ?",
                        ("failed", _utc_now_iso(), normalized_document_source_id),
                    )
                raise
            finally:
                conn.close()

    def upload_document(
        self,
        *,
        enterprise_id: str,
        filename: str,
        content: bytes,
        category_hint: str = "other",
        tags: Optional[List[str]] = None,
        auto_parse: bool = True,
        auto_generate_drafts: bool = True,
        draft_only: bool = True,
        uploaded_by: str = "",
    ) -> Dict[str, Any]:
        normalized_enterprise_id = str(enterprise_id or "").strip() or "default"
        normalized_filename = str(filename or "").strip() or "document.bin"
        normalized_tags = [str(item).strip() for item in list(tags or []) if str(item).strip()]
        now = _utc_now_iso()
        document_source_id = f"docsrc_{uuid.uuid4().hex[:16]}"
        file_id = f"docfile_{uuid.uuid4().hex[:16]}"
        content_hash = hashlib.sha256(content or b"").hexdigest()
        file_path = self._build_document_storage_path(
            normalized_enterprise_id,
            document_source_id,
            normalized_filename,
        )
        with self._lock:
            file_path.write_bytes(content or b"")
            conn = self._get_conn()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO document_sources (
                            id, enterprise_id, file_id, content_hash, original_name, stored_path,
                            file_type, file_size, category_hint, tags_json, source_channel,
                            upload_status, parse_status, structuring_status, publish_status,
                            version_no, uploaded_by, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_source_id,
                            normalized_enterprise_id,
                            file_id,
                            content_hash,
                            normalized_filename,
                            str(file_path),
                            Path(normalized_filename).suffix.lower(),
                            len(content or b""),
                            str(category_hint or "").strip() or "other",
                            json.dumps(normalized_tags, ensure_ascii=False),
                            "document_upload",
                            "uploaded",
                            "queued" if auto_parse else "pending",
                            "queued" if auto_generate_drafts else "pending",
                            "draft_only" if draft_only else "pending_publish",
                            1,
                            str(uploaded_by or "").strip(),
                            now,
                            now,
                        ),
                    )
                    upload_job = self._create_job(
                        conn,
                        document_source_id=document_source_id,
                        job_type="upload",
                        job_status="completed",
                        current_step="原始文件已保存",
                        progress=1.0,
                        metrics={"file_size": len(content or b"")},
                    )
                    parse_job = None
                    if auto_parse:
                        parse_job = self._create_job(
                            conn,
                            document_source_id=document_source_id,
                            job_type="parse",
                            job_status="pending",
                            current_step="等待解析任务执行",
                            progress=0.0,
                        )
                    structure_job = None
                    if auto_generate_drafts:
                        structure_job = self._create_job(
                            conn,
                            document_source_id=document_source_id,
                            job_type="structure",
                            job_status="pending",
                            current_step="等待草稿整理任务执行",
                            progress=0.0,
                        )
            finally:
                conn.close()
        return {
            "document_source_id": document_source_id,
            "file_id": file_id,
            "upload_job_id": upload_job.id,
            "parse_job_id": getattr(parse_job, "id", ""),
            "structure_job_id": getattr(structure_job, "id", ""),
            "status": "uploaded",
        }

    def list_documents(
        self,
        *,
        enterprise_id: str = "default",
        status: str = "all",
        keyword: str = "",
        file_type: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        normalized_enterprise_id = str(enterprise_id or "").strip() or "default"
        normalized_status = str(status or "").strip().lower()
        normalized_keyword = str(keyword or "").strip()
        normalized_file_type = str(file_type or "").strip().lower()
        safe_page = max(int(page or 1), 1)
        safe_page_size = max(1, min(int(page_size or 20), 100))
        where_clauses = ["s.enterprise_id = ?"]
        params: List[Any] = [normalized_enterprise_id]
        if normalized_status and normalized_status != "all":
            where_clauses.append(
                "("
                "s.upload_status = ? OR s.parse_status = ? OR s.structuring_status = ? OR s.publish_status = ?"
                ")"
            )
            params.extend([normalized_status, normalized_status, normalized_status, normalized_status])
        if normalized_keyword:
            where_clauses.append("(s.original_name LIKE ? OR s.category_hint LIKE ?)")
            like = f"%{normalized_keyword}%"
            params.extend([like, like])
        if normalized_file_type:
            where_clauses.append("s.file_type = ?")
            params.append(normalized_file_type)
        where_sql = " AND ".join(where_clauses)
        offset = (safe_page - 1) * safe_page_size
        conn = self._get_conn()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM document_sources s WHERE {where_sql}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT
                    s.*,
                    (
                        SELECT COUNT(*)
                        FROM knowledge_drafts d
                        WHERE d.document_source_id = s.id
                    ) AS draft_count,
                    (
                        SELECT COUNT(*)
                        FROM knowledge_drafts d
                        WHERE d.document_source_id = s.id
                          AND d.published_knowledge_id != ''
                    ) AS published_count
                FROM document_sources s
                WHERE {where_sql}
                ORDER BY s.created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, safe_page_size, offset],
            ).fetchall()
            documents = [self._row_to_document(row).to_dict() for row in rows]
        finally:
            conn.close()
        return {
            "items": documents,
            "pagination": {
                "page": safe_page,
                "page_size": safe_page_size,
                "total": int(total or 0),
            },
        }

    def get_document_detail(self, document_source_id: str, *, enterprise_id: str = "") -> Optional[Dict[str, Any]]:
        normalized_document_source_id = str(document_source_id or "").strip()
        if not normalized_document_source_id:
            return None
        params: List[Any] = [normalized_document_source_id]
        where_sql = "s.id = ?"
        normalized_enterprise_id = str(enterprise_id or "").strip()
        if normalized_enterprise_id:
            where_sql += " AND s.enterprise_id = ?"
            params.append(normalized_enterprise_id)
        conn = self._get_conn()
        try:
            row = conn.execute(
                f"""
                SELECT
                    s.*,
                    (
                        SELECT COUNT(*)
                        FROM knowledge_drafts d
                        WHERE d.document_source_id = s.id
                    ) AS draft_count,
                    (
                        SELECT COUNT(*)
                        FROM knowledge_drafts d
                        WHERE d.document_source_id = s.id
                          AND d.published_knowledge_id != ''
                    ) AS published_count
                FROM document_sources s
                WHERE {where_sql}
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            jobs = conn.execute(
                """
                SELECT *
                FROM document_structuring_jobs
                WHERE document_source_id = ?
                ORDER BY created_at ASC
                """,
                (normalized_document_source_id,),
            ).fetchall()
            artifacts = conn.execute(
                """
                SELECT artifact_type, page_no, section_title, quality_score, created_at,
                       LENGTH(content_text) AS content_length
                FROM document_artifacts
                WHERE document_source_id = ?
                ORDER BY created_at DESC
                """,
                (normalized_document_source_id,),
            ).fetchall()
            pending_review_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM knowledge_drafts
                WHERE document_source_id = ? AND review_status = 'pending'
                """,
                (normalized_document_source_id,),
            ).fetchone()[0]
            document = self._row_to_document(row).to_dict()
            document["jobs"] = [self._row_to_job(job).to_dict() for job in jobs]
            document["artifacts"] = [
                {
                    "artifact_type": item["artifact_type"],
                    "page_no": int(item["page_no"] or 0),
                    "section_title": item["section_title"] or "",
                    "quality_score": float(item["quality_score"] or 0),
                    "content_length": int(item["content_length"] or 0),
                    "created_at": item["created_at"] or "",
                }
                for item in artifacts
            ]
            document["draft_stats"] = {
                "draft_count": document["draft_count"],
                "published_count": document["published_count"],
                "pending_review_count": int(pending_review_count or 0),
            }
            return document
        finally:
            conn.close()

    def get_document_history(
        self,
        document_source_id: str,
        *,
        enterprise_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        normalized_document_source_id = str(document_source_id or "").strip()
        if not normalized_document_source_id:
            return None
        params: List[Any] = [normalized_document_source_id]
        where_sql = "s.id = ?"
        normalized_enterprise_id = str(enterprise_id or "").strip()
        if normalized_enterprise_id:
            where_sql += " AND s.enterprise_id = ?"
            params.append(normalized_enterprise_id)
        conn = self._get_conn()
        try:
            document_row = conn.execute(
                f"""
                SELECT s.id, s.enterprise_id, s.original_name, s.parse_status, s.structuring_status, s.publish_status
                FROM document_sources s
                WHERE {where_sql}
                LIMIT 1
                """,
                params,
            ).fetchone()
            if document_row is None:
                return None
            summary_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_draft_count,
                    SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                    SUM(CASE WHEN review_status = 'edited' THEN 1 ELSE 0 END) AS edited_count,
                    SUM(CASE WHEN review_status = 'approved' THEN 1 ELSE 0 END) AS approved_count,
                    SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
                    SUM(CASE WHEN review_status = 'published' OR published_knowledge_id != '' THEN 1 ELSE 0 END) AS published_count
                FROM knowledge_drafts
                WHERE document_source_id = ?
                """,
                (normalized_document_source_id,),
            ).fetchone()
            draft_rows = conn.execute(
                """
                SELECT id, question, review_status, published_knowledge_id, confidence, updated_at, created_at
                FROM knowledge_drafts
                WHERE document_source_id = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 100
                """,
                (normalized_document_source_id,),
            ).fetchall()
            published_rows = conn.execute(
                """
                SELECT
                    l.*,
                    d.question,
                    d.review_status,
                    d.published_knowledge_id
                FROM published_knowledge_links l
                LEFT JOIN knowledge_drafts d ON d.id = l.draft_id
                WHERE l.document_source_id = ?
                ORDER BY l.published_at DESC
                LIMIT 100
                """,
                (normalized_document_source_id,),
            ).fetchall()
            version_rows = conn.execute(
                """
                SELECT
                    v.*,
                    d.question AS current_question,
                    d.review_status AS current_review_status
                FROM knowledge_draft_versions v
                JOIN knowledge_drafts d ON d.id = v.draft_id
                WHERE d.document_source_id = ?
                ORDER BY v.created_at DESC, v.version_no DESC
                LIMIT 100
                """,
                (normalized_document_source_id,),
            ).fetchall()
            timeline: List[Dict[str, Any]] = []
            for row in version_rows:
                snapshot = _load_json_text(row["snapshot_json"], {})
                change_type = row["change_type"] or ""
                review_status = snapshot.get("review_status") or row["current_review_status"] or ""
                if change_type == "approve":
                    review_status = "approved"
                elif change_type == "reject":
                    review_status = "rejected"
                elif change_type == "publish":
                    review_status = "published"
                elif change_type == "update" and str(row["changed_by"] or "").strip():
                    review_status = "edited"
                timeline.append(
                    {
                        "draft_id": row["draft_id"],
                        "version_no": int(row["version_no"] or 0),
                        "change_type": change_type,
                        "changed_by": row["changed_by"] or "",
                        "created_at": row["created_at"] or "",
                        "question": snapshot.get("question") or row["current_question"] or "",
                        "review_status": review_status,
                        "published_knowledge_id": snapshot.get("published_knowledge_id") or "",
                    }
                )
            return {
                "document": {
                    "id": document_row["id"],
                    "enterprise_id": document_row["enterprise_id"] or "",
                    "original_name": document_row["original_name"] or "",
                    "parse_status": document_row["parse_status"] or "pending",
                    "structuring_status": document_row["structuring_status"] or "pending",
                    "publish_status": document_row["publish_status"] or "draft_only",
                },
                "summary": {
                    "total_draft_count": int((summary_row["total_draft_count"] if summary_row else 0) or 0),
                    "pending_count": int((summary_row["pending_count"] if summary_row else 0) or 0),
                    "edited_count": int((summary_row["edited_count"] if summary_row else 0) or 0),
                    "approved_count": int((summary_row["approved_count"] if summary_row else 0) or 0),
                    "rejected_count": int((summary_row["rejected_count"] if summary_row else 0) or 0),
                    "published_count": int((summary_row["published_count"] if summary_row else 0) or 0),
                },
                "drafts": [
                    {
                        "id": row["id"],
                        "question": row["question"] or "",
                        "review_status": row["review_status"] or "pending",
                        "published_knowledge_id": row["published_knowledge_id"] or "",
                        "confidence": float(row["confidence"] or 0),
                        "updated_at": row["updated_at"] or row["created_at"] or "",
                    }
                    for row in draft_rows
                ],
                "published_links": [
                    {
                        "id": row["id"],
                        "knowledge_item_id": row["knowledge_item_id"],
                        "draft_id": row["draft_id"],
                        "question": row["question"] or "",
                        "review_status": row["review_status"] or "",
                        "published_knowledge_id": row["published_knowledge_id"] or row["knowledge_item_id"],
                        "page_no": int(row["page_no"] or 0),
                        "section_title": row["section_title"] or "",
                        "excerpt": row["excerpt"] or "",
                        "published_at": row["published_at"] or "",
                    }
                    for row in published_rows
                ],
                "timeline": timeline,
            }
        finally:
            conn.close()

    def list_drafts(
        self,
        *,
        enterprise_id: str = "default",
        document_id: str = "",
        review_status: str = "all",
        category: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        normalized_enterprise_id = str(enterprise_id or "").strip() or "default"
        normalized_document_id = str(document_id or "").strip()
        normalized_review_status = str(review_status or "").strip().lower()
        normalized_category = str(category or "").strip()
        safe_page = max(int(page or 1), 1)
        safe_page_size = max(1, min(int(page_size or 20), 100))
        where_clauses = ["enterprise_id = ?"]
        params: List[Any] = [normalized_enterprise_id]
        if normalized_document_id:
            where_clauses.append("document_source_id = ?")
            params.append(normalized_document_id)
        if normalized_review_status and normalized_review_status != "all":
            where_clauses.append("review_status = ?")
            params.append(normalized_review_status)
        if normalized_category:
            where_clauses.append("category = ?")
            params.append(normalized_category)
        where_sql = " AND ".join(where_clauses)
        offset = (safe_page - 1) * safe_page_size
        conn = self._get_conn()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM knowledge_drafts WHERE {where_sql}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT *
                FROM knowledge_drafts
                WHERE {where_sql}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, safe_page_size, offset],
            ).fetchall()
            items = [self._row_to_draft(row).to_dict() for row in rows]
        finally:
            conn.close()
        return {
            "items": items,
            "pagination": {
                "page": safe_page,
                "page_size": safe_page_size,
                "total": int(total or 0),
            },
        }

    def get_draft_detail(
        self,
        draft_id: str,
        *,
        enterprise_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        normalized_draft_id = str(draft_id or "").strip()
        if not normalized_draft_id:
            return None
        params: List[Any] = [normalized_draft_id]
        where_sql = "d.id = ?"
        normalized_enterprise_id = str(enterprise_id or "").strip()
        if normalized_enterprise_id:
            where_sql += " AND d.enterprise_id = ?"
            params.append(normalized_enterprise_id)
        conn = self._get_conn()
        try:
            row = conn.execute(
                f"""
                SELECT d.*, s.original_name, s.file_type
                FROM knowledge_drafts d
                JOIN document_sources s ON s.id = d.document_source_id
                WHERE {where_sql}
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            draft = self._row_to_draft(row).to_dict()
            draft["document"] = {
                "original_name": row["original_name"] or "",
                "file_type": row["file_type"] or "",
            }
            return draft
        finally:
            conn.close()

    def update_draft(
        self,
        draft_id: str,
        *,
        enterprise_id: str = "",
        question: str,
        answer: str,
        category: str = "other",
        domain: str = "",
        topic: str = "",
        tags: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        aliases: Optional[List[str]] = None,
        review_comment: str = "",
        edited_by: str = "",
    ) -> Dict[str, Any]:
        normalized_draft_id = str(draft_id or "").strip()
        if not normalized_draft_id:
            raise ValueError("draft_id 不能为空")
        normalized_enterprise_id = str(enterprise_id or "").strip()
        normalized_question = str(question or "").strip()
        normalized_answer = str(answer or "").strip()
        if not self._is_valid_qa(normalized_question, normalized_answer):
            raise ValueError("问题或答案内容不满足最小要求")
        normalized_tags = [str(item).strip() for item in list(tags or []) if str(item).strip()]
        normalized_aliases = [str(item).strip() for item in list(aliases or []) if str(item).strip()]
        normalized_keywords = build_trigger_keywords(
            normalized_question,
            normalized_answer,
            provided_keywords=[str(item).strip() for item in list(keywords or []) if str(item).strip()],
            aliases=normalized_aliases,
        )
        now = _utc_now_iso()
        conn = self._get_conn()
        try:
            with conn:
                params: List[Any] = [normalized_draft_id]
                sql = "SELECT * FROM knowledge_drafts WHERE id = ?"
                if normalized_enterprise_id:
                    sql += " AND enterprise_id = ?"
                    params.append(normalized_enterprise_id)
                row = conn.execute(sql + " LIMIT 1", params).fetchone()
                if row is None:
                    raise ValueError("草稿不存在")
                self._append_draft_version(
                    conn,
                    draft_row=row,
                    change_type="update",
                    changed_by=str(edited_by or "").strip(),
                )
                conn.execute(
                    """
                    UPDATE knowledge_drafts
                    SET question = ?, answer = ?, category = ?, domain = ?, topic = ?,
                        tags_json = ?, keywords_json = ?, aliases_json = ?,
                        review_comment = ?, edited_by = ?, review_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        normalized_question,
                        normalized_answer,
                        str(category or "").strip() or "other",
                        str(domain or "").strip(),
                        str(topic or "").strip(),
                        _dump_json_text(normalized_tags, "[]"),
                        _dump_json_text(normalized_keywords, "[]"),
                        _dump_json_text(normalized_aliases, "[]"),
                        str(review_comment or "").strip(),
                        str(edited_by or "").strip(),
                        "edited" if str(edited_by or "").strip() else (row["review_status"] or "pending"),
                        now,
                        normalized_draft_id,
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM knowledge_drafts WHERE id = ? LIMIT 1",
                    (normalized_draft_id,),
                ).fetchone()
            return self._row_to_draft(updated).to_dict()
        finally:
            conn.close()

    def normalize_existing_draft_prefixes(
        self,
        *,
        enterprise_id: str = "",
        operator: str = "system_cleanup",
        limit: int = 0,
    ) -> Dict[str, Any]:
        normalized_enterprise_id = str(enterprise_id or "").strip()
        safe_limit = max(int(limit or 0), 0)
        now = _utc_now_iso()
        conn = self._get_conn()
        try:
            params: List[Any] = []
            where_clauses = ["1=1"]
            if normalized_enterprise_id:
                where_clauses.append("enterprise_id = ?")
                params.append(normalized_enterprise_id)
            query = (
                "SELECT * FROM knowledge_drafts "
                f"WHERE {' AND '.join(where_clauses)} "
                "ORDER BY created_at ASC, id ASC"
            )
            if safe_limit:
                query += " LIMIT ?"
                params.append(safe_limit)
            rows = conn.execute(query, params).fetchall()

            cleaned_ids: List[str] = []
            skipped_ids: List[str] = []
            failed_items: List[Dict[str, str]] = []
            with conn:
                for row in rows:
                    draft_id = str(row["id"] or "").strip()
                    if not draft_id:
                        continue
                    cleaned_question = self._strip_qa_marker(row["question"], kind="question")
                    cleaned_answer = self._strip_qa_marker(row["answer"], kind="answer")
                    original_question = str(row["question"] or "").strip()
                    original_answer = str(row["answer"] or "").strip()
                    if cleaned_question == original_question and cleaned_answer == original_answer:
                        skipped_ids.append(draft_id)
                        continue
                    if not self._is_valid_qa(cleaned_question, cleaned_answer):
                        failed_items.append(
                            {
                                "draft_id": draft_id,
                                "reason": "清洗后问题或答案不满足最小要求",
                            }
                        )
                        continue
                    normalized_keywords = self._extract_keywords(cleaned_question, cleaned_answer)
                    self._append_draft_version(
                        conn,
                        draft_row=row,
                        change_type="normalize_prefix",
                        changed_by=str(operator or "").strip(),
                    )
                    conn.execute(
                        """
                        UPDATE knowledge_drafts
                        SET question = ?, answer = ?, keywords_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            cleaned_question,
                            cleaned_answer,
                            _dump_json_text(normalized_keywords, "[]"),
                            now,
                            draft_id,
                        ),
                    )
                    cleaned_ids.append(draft_id)
            return {
                "cleaned_ids": cleaned_ids,
                "skipped_ids": skipped_ids,
                "failed_items": failed_items,
                "count": len(cleaned_ids),
                "skipped_count": len(skipped_ids),
                "failed_count": len(failed_items),
            }
        finally:
            conn.close()

    def approve_drafts(
        self,
        draft_ids: List[str],
        *,
        enterprise_id: str = "",
        review_comment: str = "",
        reviewed_by: str = "",
    ) -> Dict[str, Any]:
        normalized_ids = [str(item).strip() for item in list(draft_ids or []) if str(item).strip()]
        if not normalized_ids:
            raise ValueError("draft_ids 不能为空")
        normalized_enterprise_id = str(enterprise_id or "").strip()
        now = _utc_now_iso()
        approved_ids: List[str] = []
        conn = self._get_conn()
        try:
            with conn:
                for draft_id in normalized_ids:
                    row = self._get_draft_row(
                        conn,
                        draft_id=draft_id,
                        enterprise_id=normalized_enterprise_id,
                    )
                    if row is None:
                        continue
                    self._append_draft_version(
                        conn,
                        draft_row=row,
                        change_type="approve",
                        changed_by=reviewed_by,
                    )
                    conn.execute(
                        """
                        UPDATE knowledge_drafts
                        SET review_status = ?, review_comment = ?, reviewed_by = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            "approved",
                            str(review_comment or "").strip(),
                            str(reviewed_by or "").strip(),
                            now,
                            draft_id,
                        ),
                    )
                    approved_ids.append(draft_id)
            return {
                "approved_ids": approved_ids,
                "count": len(approved_ids),
            }
        finally:
            conn.close()

    def reject_drafts(
        self,
        draft_ids: List[str],
        *,
        enterprise_id: str = "",
        review_comment: str = "",
        reviewed_by: str = "",
    ) -> Dict[str, Any]:
        normalized_ids = [str(item).strip() for item in list(draft_ids or []) if str(item).strip()]
        if not normalized_ids:
            raise ValueError("draft_ids 不能为空")
        normalized_enterprise_id = str(enterprise_id or "").strip()
        now = _utc_now_iso()
        rejected_ids: List[str] = []
        conn = self._get_conn()
        try:
            with conn:
                for draft_id in normalized_ids:
                    row = self._get_draft_row(
                        conn,
                        draft_id=draft_id,
                        enterprise_id=normalized_enterprise_id,
                    )
                    if row is None:
                        continue
                    self._append_draft_version(
                        conn,
                        draft_row=row,
                        change_type="reject",
                        changed_by=reviewed_by,
                    )
                    conn.execute(
                        """
                        UPDATE knowledge_drafts
                        SET review_status = ?, review_comment = ?, reviewed_by = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            "rejected",
                            str(review_comment or "").strip(),
                            str(reviewed_by or "").strip(),
                            now,
                            draft_id,
                        ),
                    )
                    rejected_ids.append(draft_id)
            return {
                "rejected_ids": rejected_ids,
                "count": len(rejected_ids),
            }
        finally:
            conn.close()

    def publish_draft(
        self,
        draft_id: str,
        *,
        enterprise_id: str = "",
        reviewed_by: str = "",
    ) -> Dict[str, Any]:
        normalized_draft_id = str(draft_id or "").strip()
        if not normalized_draft_id:
            raise ValueError("draft_id 不能为空")
        normalized_enterprise_id = str(enterprise_id or "").strip()
        knowledge_service = get_unified_knowledge_service()
        conn = self._get_conn()
        try:
            with conn:
                row = conn.execute(
                    """
                    SELECT d.*, s.original_name, s.file_type
                    FROM knowledge_drafts d
                    JOIN document_sources s ON s.id = d.document_source_id
                    WHERE d.id = ?
                    """ + (" AND d.enterprise_id = ?" if normalized_enterprise_id else "") + """
                    LIMIT 1
                    """,
                    [normalized_draft_id, normalized_enterprise_id] if normalized_enterprise_id else [normalized_draft_id],
                ).fetchone()
                if row is None:
                    raise ValueError("草稿不存在")
                if str(row["review_status"] or "").strip().lower() == "rejected":
                    raise ValueError("已拒绝的草稿不能直接发布")
                existing_knowledge_id = str(row["published_knowledge_id"] or "").strip()
                if existing_knowledge_id:
                    return {
                        "draft_id": normalized_draft_id,
                        "knowledge_item_id": existing_knowledge_id,
                        "status": "already_published",
                    }
                normalized_aliases = _load_json_text(row["aliases_json"], [])
                normalized_keywords = build_trigger_keywords(
                    row["question"] or "",
                    row["answer"] or "",
                    provided_keywords=_load_json_text(row["keywords_json"], []),
                    aliases=normalized_aliases,
                )
                if normalized_keywords != _load_json_text(row["keywords_json"], []):
                    conn.execute(
                        "UPDATE knowledge_drafts SET keywords_json = ?, updated_at = ? WHERE id = ?",
                        (_dump_json_text(normalized_keywords, "[]"), _utc_now_iso(), normalized_draft_id),
                    )
                metadata = {
                    "document_source_id": row["document_source_id"],
                    "artifact_id": row["artifact_id"] or "",
                    "draft_id": normalized_draft_id,
                    "draft_type": row["draft_type"] or "auto",
                    "source_excerpt": row["source_excerpt"] or "",
                    "source_page_no": int(row["source_page_no"] or 0),
                    "source_section": row["source_section"] or "",
                    "original_name": row["original_name"] or "",
                    "file_type": row["file_type"] or "",
                    "confidence": float(row["confidence"] or 0),
                    "source": "document_structuring",
                }
                item = knowledge_service.add_item(
                    {
                        "question": row["question"] or "",
                        "answer": row["answer"] or "",
                        "title": row["question"] or "",
                        "content": row["answer"] or "",
                        "category": row["category"] or "other",
                        "tags": _load_json_text(row["tags_json"], []),
                        "keywords": normalized_keywords,
                        "aliases": normalized_aliases,
                        "priority": 10,
                        "enabled": True,
                        "source": "document_structuring",
                        "domain": row["domain"] or "",
                        "topic": row["topic"] or "",
                        "enterprise_id": row["enterprise_id"] or "",
                        "metadata": metadata,
                    }
                )
                if item is None or not getattr(item, "id", ""):
                    raise ValueError("发布失败，统一知识库未返回有效条目")
                now = _utc_now_iso()
                self._append_draft_version(
                    conn,
                    draft_row=row,
                    change_type="publish",
                    changed_by=reviewed_by,
                )
                conn.execute(
                    """
                    UPDATE knowledge_drafts
                    SET review_status = ?, reviewed_by = ?, published_knowledge_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    ("published", str(reviewed_by or "").strip(), item.id, now, normalized_draft_id),
                )
                conn.execute(
                    """
                    INSERT INTO published_knowledge_links (
                        id, knowledge_item_id, draft_id, document_source_id, page_no, section_title, excerpt, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"publink_{uuid.uuid4().hex[:16]}",
                        item.id,
                        normalized_draft_id,
                        row["document_source_id"],
                        int(row["source_page_no"] or 0),
                        row["source_section"] or "",
                        row["source_excerpt"] or "",
                        now,
                    ),
                )
                self._upsert_published_knowledge_record(
                    conn,
                    knowledge_item_id=item.id,
                    row=row,
                    metadata=metadata,
                    published_at=now,
                )
                self._refresh_document_publish_status(
                    conn,
                    document_source_id=str(row["document_source_id"] or ""),
                )
                return {
                    "draft_id": normalized_draft_id,
                    "knowledge_item_id": item.id,
                    "status": "published",
                }
        finally:
            conn.close()

    def backfill_published_knowledge_records(
        self,
        *,
        enterprise_id: str = "",
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        normalized_enterprise_id = str(enterprise_id or "").strip()
        knowledge_service = get_unified_knowledge_service()
        get_all_items = getattr(knowledge_service, "get_all_items", None)
        if callable(get_all_items):
            knowledge_items = list(get_all_items() or [])
        else:
            knowledge_items = list(getattr(knowledge_service, "knowledge_items", []) or [])

        knowledge_item_map: Dict[str, Any] = {}
        for item in knowledge_items:
            item_id = str(getattr(item, "id", "") or "").strip()
            if item_id:
                knowledge_item_map[item_id] = item

        conn = self._get_conn()
        try:
            params: List[Any] = []
            sql = """
                SELECT
                    d.*,
                    s.original_name,
                    s.file_type,
                    COALESCE(MAX(l.published_at), d.updated_at, d.created_at, '') AS published_at
                FROM knowledge_drafts d
                JOIN document_sources s ON s.id = d.document_source_id
                LEFT JOIN published_knowledge_links l
                    ON l.knowledge_item_id = d.published_knowledge_id
                    AND l.draft_id = d.id
                WHERE d.published_knowledge_id != ''
            """
            if normalized_enterprise_id:
                sql += " AND d.enterprise_id = ?"
                params.append(normalized_enterprise_id)
            sql += """
                GROUP BY
                    d.id, d.enterprise_id, d.document_source_id, d.artifact_id, d.draft_type,
                    d.question, d.answer, d.category, d.domain, d.topic,
                    d.tags_json, d.keywords_json, d.aliases_json,
                    d.source_excerpt, d.source_page_no, d.source_section,
                    d.confidence, d.review_status, d.review_comment,
                    d.edited_by, d.reviewed_by, d.published_knowledge_id,
                    d.created_at, d.updated_at, s.original_name, s.file_type
                ORDER BY d.updated_at DESC, d.created_at DESC
            """
            rows = conn.execute(sql, params).fetchall()

            created_count = 0
            updated_count = 0
            skipped_count = 0
            missing_knowledge_item_ids: List[str] = []
            with conn:
                for row in rows:
                    knowledge_item_id = str(row["published_knowledge_id"] or "").strip()
                    if not knowledge_item_id:
                        continue
                    item = knowledge_item_map.get(knowledge_item_id)
                    if item is None:
                        missing_knowledge_item_ids.append(knowledge_item_id)
                        continue

                    exists = conn.execute(
                        """
                        SELECT 1
                        FROM published_knowledge_records
                        WHERE knowledge_item_id = ?
                        LIMIT 1
                        """,
                        (knowledge_item_id,),
                    ).fetchone()
                    if exists is not None and not overwrite:
                        skipped_count += 1
                        continue

                    item_metadata = dict(getattr(item, "metadata", {}) or {})
                    metadata = {
                        "document_source_id": row["document_source_id"] or "",
                        "artifact_id": row["artifact_id"] or "",
                        "draft_id": row["id"] or "",
                        "draft_type": row["draft_type"] or "auto",
                        "source_excerpt": row["source_excerpt"] or "",
                        "source_page_no": int(row["source_page_no"] or 0),
                        "source_section": row["source_section"] or "",
                        "original_name": row["original_name"] or "",
                        "file_type": row["file_type"] or "",
                        "confidence": float(row["confidence"] or 0),
                        "source": "document_structuring",
                    }
                    metadata.update(item_metadata)
                    self._upsert_published_knowledge_record(
                        conn,
                        knowledge_item_id=knowledge_item_id,
                        row=row,
                        metadata=metadata,
                        published_at=str(row["published_at"] or _utc_now_iso()),
                    )
                    if exists is None:
                        created_count += 1
                    else:
                        updated_count += 1

            return {
                "count": created_count + updated_count,
                "created_count": created_count,
                "updated_count": updated_count,
                "skipped_count": skipped_count,
                "missing_knowledge_item_count": len(missing_knowledge_item_ids),
                "missing_knowledge_item_ids": missing_knowledge_item_ids[:50],
                "total_count": len(rows),
                "overwrite": bool(overwrite),
            }
        finally:
            conn.close()

    def sync_published_keyword_mirrors(
        self,
        *,
        enterprise_id: str = "",
        overwrite_records: bool = True,
    ) -> Dict[str, Any]:
        """将已发布知识的关键词回写到草稿与已发布镜像表。"""
        normalized_enterprise_id = str(enterprise_id or "").strip()
        knowledge_service = get_unified_knowledge_service()
        get_all_items = getattr(knowledge_service, "get_all_items", None)
        if callable(get_all_items):
            knowledge_items = list(get_all_items() or [])
        else:
            knowledge_items = list(getattr(knowledge_service, "knowledge_items", []) or [])

        knowledge_item_map: Dict[str, Any] = {}
        for item in knowledge_items:
            item_id = str(getattr(item, "id", "") or "").strip()
            if item_id:
                knowledge_item_map[item_id] = item

        conn = self._get_conn()
        try:
            params: List[Any] = []
            sql = """
                SELECT *
                FROM knowledge_drafts
                WHERE published_knowledge_id != ''
            """
            if normalized_enterprise_id:
                sql += " AND enterprise_id = ?"
                params.append(normalized_enterprise_id)
            rows = conn.execute(sql + " ORDER BY updated_at DESC, created_at DESC", params).fetchall()

            updated_draft_count = 0
            missing_knowledge_item_ids: List[str] = []
            with conn:
                for row in rows:
                    knowledge_item_id = str(row["published_knowledge_id"] or "").strip()
                    if not knowledge_item_id:
                        continue
                    item = knowledge_item_map.get(knowledge_item_id)
                    if item is None:
                        missing_knowledge_item_ids.append(knowledge_item_id)
                        continue
                    normalized_keywords = list(getattr(item, "keywords", []) or [])
                    current_keywords = _load_json_text(row["keywords_json"], [])
                    if normalized_keywords == current_keywords:
                        continue
                    conn.execute(
                        "UPDATE knowledge_drafts SET keywords_json = ?, updated_at = ? WHERE id = ?",
                        (
                            _dump_json_text(normalized_keywords, "[]"),
                            _utc_now_iso(),
                            row["id"],
                        ),
                    )
                    updated_draft_count += 1

            mirror_result = self.backfill_published_knowledge_records(
                enterprise_id=normalized_enterprise_id,
                overwrite=overwrite_records,
            )
            return {
                "success": True,
                "updated_draft_count": updated_draft_count,
                "missing_knowledge_item_count": len(missing_knowledge_item_ids),
                "missing_knowledge_item_ids": missing_knowledge_item_ids[:50],
                "mirror_result": mirror_result,
            }
        finally:
            conn.close()

    def batch_publish_drafts(
        self,
        draft_ids: List[str],
        *,
        enterprise_id: str = "",
        reviewed_by: str = "",
    ) -> Dict[str, Any]:
        normalized_ids = [str(item).strip() for item in list(draft_ids or []) if str(item).strip()]
        if not normalized_ids:
            raise ValueError("draft_ids 不能为空")
        published_items: List[Dict[str, Any]] = []
        failed_items: List[Dict[str, Any]] = []
        for draft_id in normalized_ids:
            try:
                result = self.publish_draft(
                    draft_id,
                    enterprise_id=enterprise_id,
                    reviewed_by=reviewed_by,
                )
                published_items.append(result)
            except ValueError as exc:
                failed_items.append(
                    {
                        "draft_id": draft_id,
                        "reason": str(exc),
                        "status": "failed",
                    }
                )
        return {
            "items": published_items,
            "count": len(published_items),
            "failed_items": failed_items,
            "failed_count": len(failed_items),
            "total_count": len(normalized_ids),
        }

    def get_job_detail(self, job_id: str) -> Optional[Dict[str, Any]]:
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM document_structuring_jobs WHERE id = ? LIMIT 1",
                (normalized_job_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_job(row).to_dict()
        finally:
            conn.close()

    def get_stats(self, *, enterprise_id: str = "default") -> Dict[str, Any]:
        normalized_enterprise_id = str(enterprise_id or "").strip() or "default"
        conn = self._get_conn()
        try:
            base_row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN parse_status = 'pending' OR parse_status = 'queued' THEN 1 ELSE 0 END) AS pending_parse_count,
                    SUM(CASE WHEN structuring_status = 'pending' OR structuring_status = 'queued' THEN 1 ELSE 0 END) AS pending_structuring_count,
                    SUM(CASE WHEN publish_status = 'published' THEN 1 ELSE 0 END) AS published_document_count,
                    COUNT(*) AS total_document_count
                FROM document_sources
                WHERE enterprise_id = ?
                """,
                (normalized_enterprise_id,),
            ).fetchone()
            draft_row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending_review_count,
                    SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
                    AVG(confidence) AS avg_confidence
                FROM knowledge_drafts
                WHERE enterprise_id = ?
                """,
                (normalized_enterprise_id,),
            ).fetchone()
        finally:
            conn.close()
        return {
            "pending_parse_count": int((base_row["pending_parse_count"] if base_row else 0) or 0),
            "pending_structuring_count": int((base_row["pending_structuring_count"] if base_row else 0) or 0),
            "pending_review_count": int((draft_row["pending_review_count"] if draft_row else 0) or 0),
            "published_count": int((base_row["published_document_count"] if base_row else 0) or 0),
            "rejected_count": int((draft_row["rejected_count"] if draft_row else 0) or 0),
            "avg_confidence": round(float((draft_row["avg_confidence"] if draft_row else 0) or 0), 4),
            "total_document_count": int((base_row["total_document_count"] if base_row else 0) or 0),
        }


_service_lock = threading.Lock()
_service_instance: Optional[DocumentStructuringService] = None


def get_document_structuring_service() -> DocumentStructuringService:
    global _service_instance
    if _service_instance is None:
        with _service_lock:
            if _service_instance is None:
                _service_instance = DocumentStructuringService()
    return _service_instance
