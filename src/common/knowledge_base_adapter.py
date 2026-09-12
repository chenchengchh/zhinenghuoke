from __future__ import annotations

"""学习域知识库兼容适配层。

为学习 workflow/repository 等旧接口提供统一知识库访问面，
避免上层直接依赖 `unified_knowledge_service` 的内部结构。
"""

from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, List, Tuple, Optional

from loguru import logger

from src.common.types.knowledge import KnowledgeItem
from src.common.unified_knowledge_service import get_unified_knowledge_service


class UnifiedKnowledgeBaseAdapter:
    """为旧学习/审核组件提供统一知识库兼容接口。"""

    def __init__(self, unified_service=None):
        self._unified_service = unified_service

    @property
    def unified_service(self):
        if self._unified_service is None:
            self._unified_service = get_unified_knowledge_service()
        return self._unified_service

    @unified_service.setter
    def unified_service(self, value):
        self._unified_service = value

    @property
    def knowledge_items(self):
        # Compatibility shim for older components that still enumerate raw items directly.
        return self.unified_service.knowledge_items

    def _list_unified_items(self) -> List[Any]:
        if self.unified_service is None:
            return []
        if hasattr(self.unified_service, "get_all_items"):
            return list(self.unified_service.get_all_items() or [])
        return list(getattr(self.unified_service, "knowledge_items", []) or [])

    def search(self, query: str, top_k: int = 5, **kwargs) -> List[Tuple[Any, float]]:
        enterprise_id = str(kwargs.get("enterprise_id") or "").strip()
        if not enterprise_id:
            logger.warning("统一知识库适配器搜索缺少 enterprise_id，已按 fail-closed 返回空结果")
            return []
        search_kwargs = {
            "category": kwargs.get("category"),
            "source": kwargs.get("source", "all"),
            "use_vector": kwargs.get("use_vector", True),
            "min_score": kwargs.get("min_score", 18.0),
            "business_stage": kwargs.get("business_stage", ""),
            "intent": kwargs.get("intent", ""),
            "enterprise_id": enterprise_id,
            "allow_vector_init": kwargs.get("allow_vector_init", True),
            "retrieval_options": kwargs.get("retrieval_options"),
        }
        return self.unified_service.search(
            query=query,
            top_k=top_k,
            **search_kwargs,
        )

    def add_knowledge(self, item: Any) -> bool:
        try:
            if isinstance(item, dict):
                item_data = dict(item)
            elif hasattr(item, "to_dict"):
                item_data = item.to_dict()
            elif is_dataclass(item):
                item_data = asdict(item)
            else:
                item_data = {
                    "question": getattr(item, "question", ""),
                    "answer": getattr(item, "answer", ""),
                    "category": getattr(item, "category", "other"),
                    "keywords": list(getattr(item, "keywords", []) or []),
                    "tags": list(getattr(item, "tags", []) or []),
                    "source": getattr(item, "source", "main"),
                    "enabled": getattr(item, "enabled", True),
                    "priority": getattr(item, "priority", 0),
                }

            item_data.setdefault("question", "")
            item_data.setdefault("answer", "")
            item_data.setdefault("category", "other")
            item_data.setdefault("source", "main")
            item_data.setdefault("enabled", True)
            item_data.setdefault("keywords", [])
            item_data.setdefault("tags", [])
            item_data.setdefault("aliases", [])
            item_data.setdefault("reply_templates", [])
            item_data.setdefault("priority", 0)
            item_data.setdefault("status", "published")
            item_data.setdefault("updated_at", datetime.now().isoformat())
            item_data.setdefault("metadata", {})

            metadata = dict(item_data.get("metadata") or {})
            enterprise_id = str(
                item_data.get("enterprise_id")
                or metadata.get("enterprise_id")
                or ""
            ).strip()
            if not enterprise_id:
                raise ValueError("enterprise_id is required for learning knowledge adapter")
            schema_id = str(
                item_data.get("schema_id")
                or metadata.get("schema_id")
                or metadata.get("preferred_schema_id")
                or ""
            ).strip()
            item_data["enterprise_id"] = enterprise_id
            metadata.setdefault("enterprise_id", enterprise_id)
            if schema_id:
                item_data["schema_id"] = schema_id
                metadata.setdefault("schema_id", schema_id)
            item_data["metadata"] = metadata

            item_id = item_data.get("id")
            if item_id and self.unified_service.get_item_by_id(item_id):
                self.unified_service.update_item(item_id, item_data)
            else:
                self.unified_service.add_item(item_data)
            return True
        except Exception as exc:
            logger.error(f"统一知识库适配器添加知识失败: {exc}")
            return False

    def record_usage(self, item_id: str) -> bool:
        return self.unified_service.increment_use_count(item_id)

    def record_usages(self, item_ids: List[str]) -> int:
        return self.unified_service.increment_use_counts(item_ids)

    def _save_knowledge(self) -> None:
        self.unified_service.persist_runtime_mutations(sync_vector=True)

    def get_knowledge_list(self) -> List[KnowledgeItem]:
        items = []
        for item in self._list_unified_items():
            items.append(
                KnowledgeItem(
                    id=item.id,
                    question=item.question,
                    answer=item.answer,
                    category=item.category,
                    tags=list(item.tags),
                    keywords=list(item.keywords),
                    aliases=list(item.aliases),
                    reply_templates=list(item.reply_templates),
                    priority=item.priority,
                    enabled=item.enabled,
                    use_count=item.use_count,
                    last_used_at=item.last_used_at,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    source=item.source or "main",
                    enterprise_id=getattr(item, "enterprise_id", ""),
                    schema_id=getattr(item, "schema_id", ""),
                    metadata=dict(getattr(item, "metadata", {}) or {}),
                )
            )
        return items

    def list_knowledge_items(self) -> List[KnowledgeItem]:
        return self.get_knowledge_list()

    def get_all_items(self) -> List[KnowledgeItem]:
        return self.get_knowledge_list()

    def get_by_category(self, category: str) -> List[KnowledgeItem]:
        return [item for item in self.get_knowledge_list() if item.category == category and item.enabled]

    def list_pending_review_items(self) -> List[KnowledgeItem]:
        return [
            item
            for item in self.get_knowledge_list()
            if not item.enabled and "待人工审核" in list(item.tags or [])
        ]

    def approve_pending_review_item(self, item_id: str, answer: str = "") -> bool:
        item = self.get_knowledge(item_id)
        if item is None:
            return False

        tags = [tag for tag in list(item.tags or []) if tag != "待人工审核"]
        if "已审核" not in tags:
            tags.append("已审核")

        updates = {
            "enabled": True,
            "tags": tags,
            "updated_at": datetime.now().isoformat(),
        }
        if answer:
            updates["answer"] = answer
        return self.update_knowledge(item_id, updates)

    def reject_pending_review_item(self, item_id: str) -> bool:
        item = self.get_knowledge(item_id)
        if item is None:
            return False
        return self.delete_knowledge(item_id)

    def update_knowledge(self, item_id: str, updates: dict) -> bool:
        try:
            sanitized_updates = dict(updates)
            sanitized_updates.setdefault("updated_at", datetime.now().isoformat())
            return self.unified_service.update_item(item_id, sanitized_updates) is not None
        except Exception as exc:
            logger.error(f"统一知识库适配器更新知识失败: {exc}")
            return False

    def delete_knowledge(self, item_id: str) -> bool:
        try:
            return self.unified_service.delete_item(item_id)
        except Exception as exc:
            logger.error(f"统一知识库适配器删除知识失败: {exc}")
            return False

    def get_knowledge(self, item_id: str) -> Optional[KnowledgeItem]:
        item = self.unified_service.get_item_by_id(item_id)
        if not item:
            return None
        return KnowledgeItem(
            id=item.id,
            question=item.question,
            answer=item.answer,
            category=item.category,
            tags=list(item.tags),
            keywords=list(item.keywords),
            aliases=list(item.aliases),
            reply_templates=list(item.reply_templates),
            priority=item.priority,
            enabled=item.enabled,
            use_count=item.use_count,
            last_used_at=item.last_used_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
            source=item.source or "main",
            enterprise_id=getattr(item, "enterprise_id", ""),
            schema_id=getattr(item, "schema_id", ""),
            metadata=dict(getattr(item, "metadata", {}) or {}),
        )

    def get_all_categories(self) -> List[str]:
        return sorted({item.category for item in self._list_unified_items() if item.enabled})


def get_learning_knowledge_base_adapter() -> UnifiedKnowledgeBaseAdapter:
    return UnifiedKnowledgeBaseAdapter()


__all__ = ["UnifiedKnowledgeBaseAdapter", "get_learning_knowledge_base_adapter"]
