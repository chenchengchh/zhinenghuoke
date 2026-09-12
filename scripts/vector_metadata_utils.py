#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""向量库 metadata 构建与同步辅助方法。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


def load_raw_kb_items(path: Path) -> List[Dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _csv_text(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(part) for part in value if str(part).strip())
    return str(value or "")


def build_vector_metadata(item: Dict[str, Any], enterprise_id: str = "default") -> Dict[str, Any]:
    metadata = dict(item.get("metadata") or {})
    vector_metadata: Dict[str, Any] = {
        "enterprise_id": enterprise_id,
        "question": str(item.get("question") or "")[:200],
        "category": item.get("category") or "general",
        "keywords": _csv_text(item.get("keywords") or []),
        "tags": _csv_text(item.get("tags") or []),
        "enabled": bool(item.get("enabled", True)),
        "priority": int(item.get("priority", 10) or 10),
        "source": str(item.get("source") or "manual"),
        "domain": str(item.get("domain") or ""),
        "topic": str(item.get("topic") or ""),
    }

    passthrough_fields: Iterable[str] = (
        "schema_id",
        "entity_type",
        "entity_name",
        "schema_topic",
        "knowledge_type",
        "conversion_stage",
        "reservation_supported",
        "handoff_trigger",
    )
    for field in passthrough_fields:
        if field in metadata:
            vector_metadata[field] = metadata.get(field)

    return vector_metadata


def build_vector_document(item: Dict[str, Any], enterprise_id: str = "default") -> Dict[str, Any]:
    question = str(item.get("question") or "")
    answer = str(item.get("answer") or "")
    content = f"{question} {answer}".strip()[:2000]
    return {
        "id": str(item.get("id") or ""),
        "content": content,
        "metadata": build_vector_metadata(item, enterprise_id=enterprise_id),
    }
