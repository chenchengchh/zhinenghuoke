from __future__ import annotations

from typing import Any, List, Optional

from src.common.knowledge_base_adapter import get_learning_knowledge_base_adapter


class LearningRepository:
    """学习运营域的知识读写仓储。"""

    def __init__(self, knowledge_base=None):
        self.knowledge_base = knowledge_base or get_learning_knowledge_base_adapter()

    def list_knowledge_items(self) -> List[Any]:
        if not self.knowledge_base:
            return []
        if hasattr(self.knowledge_base, "list_knowledge_items"):
            try:
                return list(self.knowledge_base.list_knowledge_items())
            except Exception:
                pass
        if hasattr(self.knowledge_base, "get_knowledge_list"):
            try:
                return list(self.knowledge_base.get_knowledge_list())
            except Exception:
                pass
        if hasattr(self.knowledge_base, "get_all_items"):
            try:
                return list(self.knowledge_base.get_all_items())
            except Exception:
                pass
        return list(getattr(self.knowledge_base, "knowledge_items", []) or [])

    def list_conflict_scan_items(self) -> List[Any]:
        return self.list_knowledge_items()

    def get_conflict_signature(self, items: Optional[List[Any]] = None) -> tuple[int, str, int]:
        candidates = items if items is not None else self.list_conflict_scan_items()
        latest_updated_at = ""
        enabled_count = 0
        for item in candidates:
            latest_updated_at = max(latest_updated_at, str(getattr(item, "updated_at", "") or ""))
            if getattr(item, "enabled", False):
                enabled_count += 1
        return (len(candidates), latest_updated_at, enabled_count)

    def list_pending_review_items(self) -> List[Any]:
        if not self.knowledge_base:
            return []
        if hasattr(self.knowledge_base, "list_pending_review_items"):
            try:
                return list(self.knowledge_base.list_pending_review_items())
            except Exception:
                pass
        return [
            item
            for item in self.list_knowledge_items()
            if not getattr(item, "enabled", True) and "待人工审核" in list(getattr(item, "tags", []) or [])
        ]

    def get_pending_review_item(self, item_id: str) -> Optional[Any]:
        if not self.knowledge_base:
            return None
        if hasattr(self.knowledge_base, "get_knowledge"):
            item = self.knowledge_base.get_knowledge(item_id)
            if (
                item is not None
                and not getattr(item, "enabled", True)
                and "待人工审核" in list(getattr(item, "tags", []) or [])
            ):
                return item
        for item in self.list_pending_review_items():
            if getattr(item, "id", None) == item_id:
                return item
        return None

    def approve_pending_review_item(self, item_id: str, answer: str = "") -> bool:
        if not self.knowledge_base:
            return False
        if hasattr(self.knowledge_base, "approve_pending_review_item"):
            try:
                return bool(self.knowledge_base.approve_pending_review_item(item_id, answer=answer))
            except Exception:
                pass

        item = self.get_pending_review_item(item_id)
        if item is None or not hasattr(self.knowledge_base, "update_knowledge"):
            return False

        tags = [tag for tag in list(getattr(item, "tags", []) or []) if tag != "待人工审核"]
        if "已审核" not in tags:
            tags.append("已审核")
        updates = {"enabled": True, "tags": tags}
        if answer:
            updates["answer"] = answer
        return bool(self.knowledge_base.update_knowledge(item_id, updates))

    def reject_pending_review_item(self, item_id: str) -> bool:
        if not self.knowledge_base:
            return False
        if hasattr(self.knowledge_base, "reject_pending_review_item"):
            try:
                return bool(self.knowledge_base.reject_pending_review_item(item_id))
            except Exception:
                pass
        if not hasattr(self.knowledge_base, "delete_knowledge"):
            return False
        return bool(self.knowledge_base.delete_knowledge(item_id))


def get_learning_repository(knowledge_base=None) -> LearningRepository:
    return LearningRepository(knowledge_base=knowledge_base)


__all__ = ["LearningRepository", "get_learning_repository"]
