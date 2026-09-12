from __future__ import annotations

import sqlite3


def ensure_document_structuring_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_sources (
            id TEXT PRIMARY KEY,
            enterprise_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            original_name TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_size INTEGER NOT NULL DEFAULT 0,
            category_hint TEXT NOT NULL DEFAULT 'other',
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_channel TEXT NOT NULL DEFAULT 'document_upload',
            upload_status TEXT NOT NULL DEFAULT 'uploaded',
            parse_status TEXT NOT NULL DEFAULT 'pending',
            structuring_status TEXT NOT NULL DEFAULT 'pending',
            publish_status TEXT NOT NULL DEFAULT 'draft_only',
            version_no INTEGER NOT NULL DEFAULT 1,
            uploaded_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_sources_enterprise_id "
        "ON document_sources(enterprise_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_sources_content_hash "
        "ON document_sources(content_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_sources_parse_status "
        "ON document_sources(parse_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_sources_structuring_status "
        "ON document_sources(structuring_status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_artifacts (
            id TEXT PRIMARY KEY,
            document_source_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            content_text TEXT NOT NULL DEFAULT '',
            content_json TEXT NOT NULL DEFAULT '{}',
            page_no INTEGER NOT NULL DEFAULT 0,
            section_title TEXT NOT NULL DEFAULT '',
            block_index INTEGER NOT NULL DEFAULT 0,
            parser_name TEXT NOT NULL DEFAULT '',
            parser_version TEXT NOT NULL DEFAULT '',
            quality_score REAL NOT NULL DEFAULT 0,
            review_required INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_artifacts_document_source_id "
        "ON document_artifacts(document_source_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_artifacts_artifact_type "
        "ON document_artifacts(artifact_type)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_drafts (
            id TEXT PRIMARY KEY,
            enterprise_id TEXT NOT NULL,
            document_source_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL DEFAULT '',
            draft_type TEXT NOT NULL DEFAULT 'auto',
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other',
            domain TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            keywords_json TEXT NOT NULL DEFAULT '[]',
            aliases_json TEXT NOT NULL DEFAULT '[]',
            source_excerpt TEXT NOT NULL DEFAULT '',
            source_page_no INTEGER NOT NULL DEFAULT 0,
            source_section TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'pending',
            review_comment TEXT NOT NULL DEFAULT '',
            edited_by TEXT NOT NULL DEFAULT '',
            reviewed_by TEXT NOT NULL DEFAULT '',
            published_knowledge_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_drafts_enterprise_id "
        "ON knowledge_drafts(enterprise_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_drafts_document_source_id "
        "ON knowledge_drafts(document_source_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_drafts_review_status "
        "ON knowledge_drafts(review_status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_draft_versions (
            id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL,
            version_no INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            change_type TEXT NOT NULL DEFAULT 'update',
            changed_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_draft_versions_draft_id "
        "ON knowledge_draft_versions(draft_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_structuring_jobs (
            id TEXT PRIMARY KEY,
            document_source_id TEXT NOT NULL,
            job_type TEXT NOT NULL,
            job_status TEXT NOT NULL DEFAULT 'pending',
            current_step TEXT NOT NULL DEFAULT '',
            progress REAL NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_structuring_jobs_document_source_id "
        "ON document_structuring_jobs(document_source_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_structuring_jobs_job_status "
        "ON document_structuring_jobs(job_status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS published_knowledge_links (
            id TEXT PRIMARY KEY,
            knowledge_item_id TEXT NOT NULL,
            draft_id TEXT NOT NULL,
            document_source_id TEXT NOT NULL,
            page_no INTEGER NOT NULL DEFAULT 0,
            section_title TEXT NOT NULL DEFAULT '',
            excerpt TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_published_knowledge_links_knowledge_item_id "
        "ON published_knowledge_links(knowledge_item_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_published_knowledge_links_document_source_id "
        "ON published_knowledge_links(document_source_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS published_knowledge_records (
            knowledge_item_id TEXT PRIMARY KEY,
            enterprise_id TEXT NOT NULL DEFAULT '',
            draft_id TEXT NOT NULL DEFAULT '',
            document_source_id TEXT NOT NULL DEFAULT '',
            question TEXT NOT NULL DEFAULT '',
            answer TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'other',
            domain TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            keywords_json TEXT NOT NULL DEFAULT '[]',
            aliases_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            source_excerpt TEXT NOT NULL DEFAULT '',
            source_page_no INTEGER NOT NULL DEFAULT 0,
            source_section TEXT NOT NULL DEFAULT '',
            original_name TEXT NOT NULL DEFAULT '',
            file_type TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_published_knowledge_records_document_source_id "
        "ON published_knowledge_records(document_source_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_published_knowledge_records_enterprise_id "
        "ON published_knowledge_records(enterprise_id)"
    )
