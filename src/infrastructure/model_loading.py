from __future__ import annotations

import os
from typing import Dict


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def is_local_model_only_enabled() -> bool:
    return (
        _env_flag("LOCAL_MODEL_ONLY", False)
        or _env_flag("HF_HUB_OFFLINE", False)
        or _env_flag("TRANSFORMERS_OFFLINE", False)
    )


def apply_local_model_only_env() -> None:
    if not is_local_model_only_enabled():
        return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")


def build_transformers_local_kwargs() -> Dict[str, bool]:
    apply_local_model_only_env()
    if is_local_model_only_enabled():
        return {"local_files_only": True}
    return {}

