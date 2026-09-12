from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from src.infrastructure.runtime_paths import get_data_dir


def get_remote_policy_path() -> Path:
    return get_data_dir() / "remote_control_policy.json"


class RemotePolicyStorage:
    def __init__(self, file_path: Optional[Path] = None):
        self.file_path = file_path or get_remote_policy_path()

    def load(self) -> Optional[dict]:
        if not self.file_path.exists():
            return None
        try:
            return json.loads(self.file_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save(self, payload: dict) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        if self.file_path.exists():
            self.file_path.unlink()
