import json
import sqlite3
import shutil
from pathlib import Path
from typing import Callable, Iterable, Sequence


CRITICAL_DATA_FILES = (
    "customers.json",
    "customers.journal.jsonl",
    "customers.sqlite3",
    "messages.sqlite3",
    "license.dat",
    "license.fallback.dat",
    "license_runtime_state.dat",
    "license_runtime_state.fallback.dat",
)

CRITICAL_DATA_DIRS = (
    "browser_user_data",
    "crawler_browser_user_data",
)


def _safe_log(logger: Callable[[str], None] | None, message: str) -> None:
    if logger is None:
        return
    try:
        logger(message)
    except Exception:
        return


def _is_empty_customer_payload(file_path: Path) -> bool:
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    if not isinstance(payload, dict):
        return False

    customers = payload.get("customers")
    platforms = payload.get("platforms")
    send_modes = payload.get("send_modes")
    return (
        isinstance(customers, list)
        and len(customers) == 0
        and isinstance(platforms, dict)
        and len(platforms) == 0
        and isinstance(send_modes, dict)
        and len(send_modes) == 0
    )


def is_placeholder_runtime_file(file_path: Path) -> bool:
    if not file_path.exists() or not file_path.is_file():
        return True

    try:
        size = file_path.stat().st_size
    except OSError:
        return False

    name = file_path.name.lower()
    if name == "customers.json":
        if size <= 256 and _is_empty_customer_payload(file_path):
            return True
        return False

    if name.startswith("license") and name.endswith(".dat"):
        return size <= 8

    if name.endswith(".sqlite3"):
        return _is_empty_sqlite_runtime_db(file_path)

    if name.endswith(".jsonl"):
        return size == 0

    return False


def _sqlite_table_count(file_path: Path, table_name: str) -> int | None:
    try:
        conn = sqlite3.connect(str(file_path))
    except Exception:
        return None

    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        if cursor.fetchone() is None:
            return None
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return None
    finally:
        conn.close()


def _is_empty_sqlite_runtime_db(file_path: Path) -> bool:
    name = file_path.name.lower()
    if name == "customers.sqlite3":
        count = _sqlite_table_count(file_path, "customers")
        return count == 0 if count is not None else False

    if name == "messages.sqlite3":
        messages_count = _sqlite_table_count(file_path, "messages")
        conversations_count = _sqlite_table_count(file_path, "conversations")
        observed_counts = [count for count in (messages_count, conversations_count) if count is not None]
        return bool(observed_counts) and all(count == 0 for count in observed_counts)

    return False


def _directory_has_content(directory: Path) -> bool:
    try:
        next(directory.iterdir())
        return True
    except StopIteration:
        return False
    except OSError:
        return False


def discover_legacy_data_dirs(
    *,
    application_path: Path,
    persistent_data_dir: Path,
    working_dir: Path | None = None,
) -> list[Path]:
    candidates = [
        application_path / "data",
        application_path.parent / "data",
    ]
    if working_dir is not None:
        candidates.append(working_dir / "data")

    resolved_current = persistent_data_dir.resolve()
    results: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        key = str(resolved).lower()
        if key == str(resolved_current).lower() or key in seen:
            continue
        seen.add(key)
        if resolved.exists() and resolved.is_dir():
            results.append(resolved)
    return results


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_dir_contents(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        target_path = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target_path, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target_path)


def migrate_legacy_runtime_data(
    *,
    application_path: Path,
    persistent_data_dir: Path,
    working_dir: Path | None = None,
    legacy_data_dirs: Sequence[Path] | None = None,
    logger: Callable[[str], None] | None = None,
) -> list[str]:
    persistent_data_dir.mkdir(parents=True, exist_ok=True)
    data_dirs = list(
        legacy_data_dirs
        if legacy_data_dirs is not None
        else discover_legacy_data_dirs(
            application_path=application_path,
            persistent_data_dir=persistent_data_dir,
            working_dir=working_dir,
        )
    )

    if not data_dirs:
        _safe_log(logger, "[迁移] 未发现旧数据目录，跳过运行时数据迁移")
        return []

    migrated: list[str] = []
    for legacy_dir in data_dirs:
        _safe_log(logger, f"[迁移] 检查旧数据目录: {legacy_dir}")

        for file_name in CRITICAL_DATA_FILES:
            source = legacy_dir / file_name
            target = persistent_data_dir / file_name
            if not source.exists() or not source.is_file():
                continue
            if is_placeholder_runtime_file(source):
                continue
            if not is_placeholder_runtime_file(target):
                continue

            _copy_file(source, target)
            migrated.append(file_name)
            _safe_log(logger, f"[迁移] 已迁移文件: {source} -> {target}")

        for dir_name in CRITICAL_DATA_DIRS:
            source_dir = legacy_dir / dir_name
            target_dir = persistent_data_dir / dir_name
            if not source_dir.exists() or not source_dir.is_dir():
                continue
            if not _directory_has_content(source_dir):
                continue
            if target_dir.exists() and _directory_has_content(target_dir):
                continue

            _copy_dir_contents(source_dir, target_dir)
            migrated.append(dir_name)
            _safe_log(logger, f"[迁移] 已迁移目录: {source_dir} -> {target_dir}")

    if migrated:
        unique_items = list(dict.fromkeys(migrated))
        _safe_log(logger, f"[迁移] 完成旧运行时数据迁移: {', '.join(unique_items)}")
        return unique_items

    _safe_log(logger, "[迁移] 未检测到需要迁移的旧运行时数据")
    return []
