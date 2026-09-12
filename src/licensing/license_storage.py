from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

APP_NAME = "HuokeSmartBot"


def _expand_path(path_value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path_value)))


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _get_base_dir() -> Path:
    env_value = os.getenv("HUOKE_BASE_DIR", "").strip()
    if env_value:
        return _expand_path(env_value)
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return _repo_root()


def get_persistent_root_dir() -> Path:
    env_value = os.getenv("HUOKE_PERSISTENT_ROOT", "").strip()
    if env_value:
        return _expand_path(env_value)
    if _is_frozen():
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            return _expand_path(local_app_data) / APP_NAME
    return _get_base_dir()


def get_data_dir() -> Path:
    env_value = os.getenv("HUOKE_DATA_DIR", "").strip()
    if env_value:
        return _expand_path(env_value)
    return get_persistent_root_dir() / "data"


def get_license_file_path() -> Path:
    return get_data_dir() / "license.dat"


def get_license_fallback_file_path() -> Path:
    # Keep the fallback inside the data directory so packaged builds do not
    # need to create new files at the persistent root level.
    return get_data_dir() / "license.fallback.dat"


def get_license_runtime_state_file_path() -> Path:
    return get_data_dir() / "license_runtime_state.dat"


def get_license_runtime_state_fallback_file_path() -> Path:
    return get_data_dir() / "license_runtime_state.fallback.dat"


class EncodedJsonStorage:
    def __init__(self, file_path: Path):
        self._primary_file_path = Path(file_path)
        self.file_path = self._primary_file_path

    def _fallback_paths(self) -> list[Path]:
        return []

    def _candidate_paths(self) -> list[Path]:
        candidates = [self._primary_file_path]
        for fallback in self._fallback_paths():
            if fallback not in candidates:
                candidates.append(fallback)
        return candidates

    def _encode_payload(self, payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return base64.b64encode(raw.encode("utf-8")).decode("utf-8")

    def _decode_payload(self, file_path: Path) -> Optional[dict]:
        try:
            payload = file_path.read_text(encoding="utf-8").strip()
            if not payload:
                return None
            decoded = base64.b64decode(payload.encode("utf-8")).decode("utf-8")
            return json.loads(decoded)
        except Exception:
            return None

    def load(self) -> Optional[dict]:
        primary_path = self._primary_file_path
        for candidate in self._candidate_paths():
            if not candidate.exists():
                continue
            decoded = self._decode_payload(candidate)
            if decoded is not None:
                if candidate != primary_path:
                    try:
                        self._write_atomic(primary_path, self._encode_payload(decoded))
                    except OSError:
                        pass
                self.file_path = primary_path if primary_path.exists() else candidate
                return decoded
        return None

    def _write_atomic(self, file_path: Path, encoded: str) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = file_path.with_name(f"{file_path.name}.{os.getpid()}.tmp")
        try:
            temp_path.write_text(encoded, encoding="utf-8")
            temp_path.replace(file_path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def _write_with_retries(self, file_path: Path, encoded: str) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                self._write_atomic(file_path, encoded)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.2 * (attempt + 1))
            except OSError as exc:
                last_error = exc
                break

        if last_error is not None:
            raise last_error
        raise PermissionError("无法写入授权文件")

    def save(self, payload: dict) -> None:
        encoded = self._encode_payload(payload)
        primary_path, *fallback_paths = self._candidate_paths()
        self._write_with_retries(primary_path, encoded)
        for fallback_path in fallback_paths:
            try:
                self._write_with_retries(fallback_path, encoded)
            except OSError:
                continue
        self.file_path = primary_path

    def clear(self) -> None:
        for candidate in self._candidate_paths():
            if candidate.exists():
                candidate.unlink()


class LicenseStorage(EncodedJsonStorage):
    def __init__(self, file_path: Optional[Path] = None):
        super().__init__(file_path or get_license_file_path())

    def _fallback_paths(self) -> list[Path]:
        return [get_license_fallback_file_path()]


class LicenseRuntimeStateStorage(EncodedJsonStorage):
    def __init__(self, file_path: Optional[Path] = None):
        super().__init__(file_path or get_license_runtime_state_file_path())

    def _fallback_paths(self) -> list[Path]:
        return [get_license_runtime_state_fallback_file_path()]
