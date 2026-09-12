from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class DocumentSourceRecord:
    id: str
    enterprise_id: str
    file_id: str
    content_hash: str
    original_name: str
    stored_path: str
    file_type: str
    file_size: int = 0
    category_hint: str = "other"
    tags: List[str] = field(default_factory=list)
    source_channel: str = "document_upload"
    upload_status: str = "uploaded"
    parse_status: str = "pending"
    structuring_status: str = "pending"
    publish_status: str = "draft_only"
    version_no: int = 1
    uploaded_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    draft_count: int = 0
    published_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DocumentStructuringJobRecord:
    id: str
    document_source_id: str
    job_type: str
    job_status: str = "pending"
    current_step: str = ""
    progress: float = 0.0
    error_message: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class KnowledgeDraftRecord:
    id: str
    enterprise_id: str
    document_source_id: str
    artifact_id: str = ""
    draft_type: str = "auto"
    question: str = ""
    answer: str = ""
    category: str = "other"
    domain: str = ""
    topic: str = ""
    tags: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    source_excerpt: str = ""
    source_page_no: int = 0
    source_section: str = ""
    confidence: float = 0.0
    review_status: str = "pending"
    review_comment: str = ""
    edited_by: str = ""
    reviewed_by: str = ""
    published_knowledge_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
