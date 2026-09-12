from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _deep_get(payload: Any, path: str) -> Any:
    current = payload
    for part in [segment.strip() for segment in str(path or "").split(".") if segment.strip()]:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _extract_ocr_text(payload: Any, preferred_path: str = "") -> str:
    candidate_paths = [preferred_path] if preferred_path else []
    candidate_paths.extend(
        [
            "text",
            "content",
            "ocr_text",
            "extracted_text",
            "data.text",
            "data.content",
            "data.ocr_text",
            "result.text",
            "result.content",
            "result.ocr_text",
        ]
    )
    for path in candidate_paths:
        value = _deep_get(payload, path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return ""


def build_document_ocr_extractor_from_env() -> Optional[Callable[[str], str]]:
    endpoint = str(os.getenv("DOCUMENT_OCR_ENDPOINT", "") or "").strip()
    enabled = _env_flag("DOCUMENT_OCR_ENABLED", bool(endpoint))
    if not enabled or not endpoint:
        return None

    timeout = max(float(os.getenv("DOCUMENT_OCR_TIMEOUT_SECONDS", "30") or 30.0), 3.0)
    file_field = str(os.getenv("DOCUMENT_OCR_FILE_FIELD", "file") or "file").strip() or "file"
    response_field = str(os.getenv("DOCUMENT_OCR_RESPONSE_FIELD", "") or "").strip()
    api_key = str(os.getenv("DOCUMENT_OCR_API_KEY", "") or "").strip()
    auth_header = str(os.getenv("DOCUMENT_OCR_AUTH_HEADER", "Authorization") or "Authorization").strip()
    auth_scheme = str(os.getenv("DOCUMENT_OCR_AUTH_SCHEME", "Bearer") or "Bearer").strip()

    def _extract(file_path: str) -> str:
        import requests

        request_headers: dict[str, str] = {}
        if api_key:
            request_headers[auth_header] = f"{auth_scheme} {api_key}".strip()

        upload_path = Path(file_path)
        with upload_path.open("rb") as fh:
            response = requests.post(
                endpoint,
                files={file_field: (upload_path.name, fh, "application/octet-stream")},
                headers=request_headers or None,
                timeout=timeout,
            )
        response.raise_for_status()

        content_type = str(response.headers.get("content-type", "")).lower()
        if "application/json" in content_type:
            text = _extract_ocr_text(response.json(), preferred_path=response_field)
        else:
            text = str(response.text or "").strip()

        if not text:
            logger.warning(f"OCR 服务返回空文本: endpoint={endpoint}, file={upload_path.name}")
        return text

    logger.info(f"文档 OCR 提取器已启用: {endpoint}")
    return _extract
