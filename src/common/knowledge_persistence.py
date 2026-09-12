import json
import os
import threading
import tempfile
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from loguru import logger


class KnowledgePersistence:

    def __init__(self, data_dir: str = ""):
        self._data_dir = Path(data_dir) if data_dir else Path("")
        self.unified_data_path = self._data_dir / "knowledge_base.json"
        self.legacy_data_path = self._data_dir / "knowledge_unified.json"
        self.enterprise_data_path = self._data_dir / "knowledge" / "knowledge_items.json"
        self._file_signature: Optional[Tuple[int, int]] = None
        self._cache: Dict[str, Any] = {}
        self._cache_time: float = 0
        self._cache_ttl: int = 60
        self._data_lock = threading.Lock()

    def load_knowledge_base(self, source_path: str = "") -> List[Dict]:
        from .unified_knowledge_service import normalize_knowledge_payloads, _normalize_runtime_flags

        items: List[Dict] = []
        # 修复 R2：多源合并而非替换，按 id 去重
        seen_ids: set = set()

        knowledge_paths = [
            self.unified_data_path,
            self.legacy_data_path,
            self.enterprise_data_path,
        ]

        if source_path:
            source = Path(source_path)
            if source.exists():
                knowledge_paths = [source] + knowledge_paths

        for path in knowledge_paths:
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    source_hint = "main" if path == self.unified_data_path else ""
                    normalized_data = normalize_knowledge_payloads(
                        data,
                        source_hint=source_hint,
                        strict_question_validation=False,
                        skip_invalid_items=True,
                    )
                    loaded_items = [_normalize_runtime_flags(item) for item in normalized_data]

                    # 修复 R2：多源合并，按 id 去重，而非只保留记录数最多的源
                    added = 0
                    for item in loaded_items:
                        item_id = item.get("id", "")
                        if item_id:
                            if item_id in seen_ids:
                                continue
                            seen_ids.add(item_id)
                        items.append(item)
                        added += 1

                    if added > 0:
                        logger.info(f"从 {path.name} 加载 {added} 条知识（累计 {len(items)} 条）")
                except Exception as e:
                    logger.error(f"加载 {path} 失败: {e}")

        self.invalidate_cache()
        return items

    def save_knowledge_base(self, items: List, target_path: str = "") -> bool:
        target = Path(target_path) if target_path else self.unified_data_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_dir = str(target.parent)
            fd, temp_path = tempfile.mkstemp(suffix=".json", dir=temp_dir)
            try:
                serialized = [
                    item.to_dict() if hasattr(item, "to_dict") else item
                    for item in items
                ]
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(serialized, f, ensure_ascii=False, indent=2)
                os.replace(temp_path, str(target))
            except Exception:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise
            self.invalidate_cache()
            self.update_file_signature()
            logger.info(f"保存 {len(items)} 条知识到统一数据文件")
            return True
        except Exception as e:
            logger.error(f"保存数据失败: {e}")
            return False

    def hot_reload(self, source_path: str = "", current_items: List = None) -> Tuple[bool, List[Dict]]:
        current_signature = self.get_file_signature()
        if current_signature == self._file_signature:
            return (False, [])

        with self._data_lock:
            current_signature = self.get_file_signature()
            if current_signature == self._file_signature:
                return (False, [])

            previous_count = len(current_items) if current_items else 0
            new_items = self.load_knowledge_base(source_path=source_path)
            self.update_file_signature()
            logger.info(
                "检测到知识库文件外部变更，已重新加载数据: "
                f"{previous_count} -> {len(new_items)} 条"
            )
            return (True, new_items)

    def warmup(self, items: List = None) -> List[Dict]:
        from .unified_knowledge_service import normalize_knowledge_payloads, _normalize_runtime_flags

        if items:
            return [
                item.to_dict() if hasattr(item, "to_dict") else item
                for item in items
            ]

        loaded: List[Dict] = []

        if self.unified_data_path.exists():
            try:
                with open(self.unified_data_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data = normalize_knowledge_payloads(
                    data,
                    source_hint="main",
                    strict_question_validation=False,
                    skip_invalid_items=True,
                )
                loaded = [_normalize_runtime_flags(item) for item in data]
            except Exception as e:
                logger.error(f"预加载数据失败: {e}")

        if not loaded and self.legacy_data_path.exists():
            try:
                with open(self.legacy_data_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data = normalize_knowledge_payloads(
                    data,
                    source_hint="main",
                    strict_question_validation=False,
                    skip_invalid_items=True,
                )
                for item in data:
                    item["source"] = "main"
                    loaded.append(_normalize_runtime_flags(item))
            except Exception as e:
                logger.error(f"预加载主知识库失败: {e}")

        return loaded

    def get_file_signature(self) -> Optional[Tuple[int, int]]:
        try:
            stat = self.unified_data_path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def update_file_signature(self) -> None:
        self._file_signature = self.get_file_signature()

    def invalidate_cache(self) -> None:
        self._cache = {}
        self._cache_time = 0
