from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def _normalize_http_url_env(var_name: str) -> None:
    """确保 URL 型环境变量在缺少协议头时自动补全 http://。"""
    raw_value = str(os.getenv(var_name, "") or "").strip()
    if not raw_value or "://" in raw_value:
        return
    os.environ[var_name] = f"http://{raw_value.lstrip('/')}"


def _expand_path(path_value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path_value))).resolve()


def _default_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resolve_env_file(base_dir: str | Path | None = None) -> Optional[Path]:
    explicit_path = str(os.getenv("HUOKE_ENV_FILE", "") or "").strip()
    if explicit_path:
        env_path = _expand_path(explicit_path)
        return env_path if env_path.exists() else None

    if base_dir is None:
        base_path = _expand_path(os.getenv("HUOKE_BASE_DIR", "").strip()) if os.getenv("HUOKE_BASE_DIR", "").strip() else _default_base_dir()
    else:
        base_path = _expand_path(str(base_dir))

    env_path = base_path / ".env"
    return env_path if env_path.exists() else None


def load_project_env(
    base_dir: str | Path | None = None,
    *,
    override: bool = False,
) -> Optional[Path]:
    try:
        from dotenv import load_dotenv
    except Exception:
        return None

    env_path = resolve_env_file(base_dir=base_dir)
    if not env_path:
        return None

    load_dotenv(env_path, override=override)
    _normalize_http_url_env("OLLAMA_HOST")
    _normalize_http_url_env("LLM_BASE_URL")
    return env_path
