import os
import sys
from pathlib import Path


APP_NAME = "HuokeSmartBot"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _expand_path(path_value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path_value)))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_base_dir() -> Path:
    env_value = os.getenv("HUOKE_BASE_DIR", "").strip()
    if env_value:
        return _expand_path(env_value)
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _repo_root()


def get_persistent_root_dir() -> Path:
    env_value = os.getenv("HUOKE_PERSISTENT_ROOT", "").strip()
    if env_value:
        return _expand_path(env_value)
    if is_frozen():
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            return _expand_path(local_app_data) / APP_NAME
    return get_base_dir()


def get_data_dir() -> Path:
    env_value = os.getenv("HUOKE_DATA_DIR", "").strip()
    if env_value:
        return _expand_path(env_value)
    return get_persistent_root_dir() / "data"


def get_log_dir() -> Path:
    env_value = os.getenv("HUOKE_LOG_DIR", "").strip()
    if env_value:
        return _expand_path(env_value)
    return get_persistent_root_dir() / "logs"


def get_config_dir() -> Path:
    env_value = os.getenv("HUOKE_CONFIG_DIR", "").strip()
    if env_value:
        return _expand_path(env_value)
    return get_persistent_root_dir() / "config"


def get_models_dir() -> Path:
    env_value = os.getenv("HUOKE_MODELS_DIR", "").strip()
    if env_value:
        return _expand_path(env_value)
    candidates = [
        get_base_dir() / "models",
        get_base_dir() / "_internal" / "models",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def get_kb_data_dir() -> Path:
    env_value = os.getenv("HUOKE_KB_DATA_DIR", "").strip()
    if env_value:
        return _expand_path(env_value)
    candidates = [
        get_base_dir() / "kb_data",
        get_base_dir() / "_internal" / "kb_data",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def get_static_dir() -> Path:
    env_value = os.getenv("HUOKE_STATIC_DIR", "").strip()
    if env_value:
        return _expand_path(env_value)
    candidates = [
        get_base_dir() / "_internal" / "src" / "web" / "static",
        get_base_dir() / "src" / "web" / "static",
        get_base_dir() / "static",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if is_frozen():
        return candidates[0]
    return candidates[1]


def get_templates_dir() -> Path:
    env_value = os.getenv("HUOKE_TEMPLATES_DIR", "").strip()
    if env_value:
        return _expand_path(env_value)
    candidates = [
        get_base_dir() / "_internal" / "src" / "web" / "templates",
        get_base_dir() / "src" / "web" / "templates",
        get_base_dir() / "templates",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if is_frozen():
        return candidates[0]
    return candidates[1]


def get_browser_user_data_dir() -> Path:
    return get_data_dir() / "browser_user_data"


def get_crawler_browser_user_data_dir() -> Path:
    return get_data_dir() / "crawler_browser_user_data"


def get_accounts_dir() -> Path:
    return get_data_dir() / "accounts"


def get_account_registry_path() -> Path:
    return get_accounts_dir() / "registry.json"


def get_account_dir(account_id: str) -> Path:
    normalized = str(account_id or "").strip() or "default"
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in normalized)
    safe_name = safe_name or "default"
    return get_accounts_dir() / safe_name


def get_account_browser_user_data_dir(account_id: str) -> Path:
    return get_account_dir(account_id) / "browser_user_data"


def get_account_crawler_browser_user_data_dir(account_id: str) -> Path:
    return get_account_dir(account_id) / "crawler_browser_user_data"


def get_app_state_dir() -> Path:
    return get_data_dir() / "app_state"


def get_feedback_dir() -> Path:
    return get_data_dir() / "feedback"


def get_learning_dir() -> Path:
    return get_data_dir() / "learning"


def get_document_structuring_dir() -> Path:
    return get_data_dir() / "document_structuring"


def get_industry_schemas_dir() -> Path:
    return get_data_dir() / "industry_schemas"


def get_knowledge_files_dir() -> Path:
    return get_data_dir() / "knowledge_files"


def get_archives_dir() -> Path:
    return get_data_dir() / "archives"


def get_chroma_dir() -> Path:
    return get_data_dir() / "chroma_db"


def get_memory_db_path() -> Path:
    return get_data_dir() / "memory.db"


def get_customers_db_path() -> Path:
    return get_data_dir() / "customers.json"


def get_messages_sqlite_path() -> Path:
    return get_data_dir() / "messages.sqlite3"


def get_knowledge_base_path() -> Path:
    return get_data_dir() / "knowledge_base.json"


def get_models_embedding_dir() -> Path:
    return get_models_dir() / "embedding"


def get_models_reranker_dir() -> Path:
    return get_models_embedding_dir() / "reranker"


def ensure_runtime_directories() -> None:
    required_dirs = [
        get_data_dir(),
        get_log_dir(),
        get_config_dir(),
        get_app_state_dir(),
        get_feedback_dir(),
        get_learning_dir(),
        get_document_structuring_dir(),
        get_industry_schemas_dir(),
        get_knowledge_files_dir(),
        get_archives_dir(),
        get_chroma_dir(),
        get_accounts_dir(),
        get_browser_user_data_dir(),
        get_crawler_browser_user_data_dir(),
        get_models_embedding_dir(),
        get_models_reranker_dir(),
    ]
    for directory in required_dirs:
        directory.mkdir(parents=True, exist_ok=True)
