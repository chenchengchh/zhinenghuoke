from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Mapping, Optional

from src.licensing.license_models import RequestCodePayload
from src.licensing.machine_fingerprint import (
    FINGERPRINT_VERSION,
    build_machine_fingerprint,
    collect_machine_fingerprint_components,
    get_machine_fingerprint_profile,
)


def _canonical_payload(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_request_code_checksum(payload: Mapping[str, object]) -> str:
    checksum_payload = dict(payload)
    checksum_payload.pop("checksum", None)
    return hashlib.sha256(_canonical_payload(checksum_payload).encode("utf-8")).hexdigest()


def build_request_code_payload(
    *,
    machine_hash: Optional[str] = None,
    components: Optional[Mapping[str, str]] = None,
    generated_at: Optional[datetime] = None,
) -> RequestCodePayload:
    current_components = dict(components or collect_machine_fingerprint_components())
    current_machine_hash = str(machine_hash or "").strip() or build_machine_fingerprint()
    timestamp = generated_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    payload = RequestCodePayload(
        machine_hash=current_machine_hash,
        fingerprint_version=FINGERPRINT_VERSION,
        generated_at=timestamp,
        machine_profile=get_machine_fingerprint_profile(current_components),
        checksum="",
    )
    payload.checksum = build_request_code_checksum(payload.model_dump(mode="json"))
    return payload


def encode_request_code(payload: RequestCodePayload | Mapping[str, object]) -> str:
    request_payload = payload if isinstance(payload, RequestCodePayload) else RequestCodePayload.model_validate(payload)
    payload_dict = request_payload.model_dump(mode="json")
    payload_dict["checksum"] = build_request_code_checksum(payload_dict)
    raw = _canonical_payload(payload_dict)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8").rstrip("=")


def decode_request_code(request_code: str) -> RequestCodePayload:
    normalized = str(request_code or "").strip()
    decoded = base64.urlsafe_b64decode(normalized.encode("utf-8") + b"===")
    payload = json.loads(decoded.decode("utf-8"))
    request_payload = RequestCodePayload.model_validate(payload)
    validate_request_code(request_payload)
    return request_payload


def validate_request_code(payload: RequestCodePayload | Mapping[str, object]) -> RequestCodePayload:
    request_payload = payload if isinstance(payload, RequestCodePayload) else RequestCodePayload.model_validate(payload)
    expected_checksum = build_request_code_checksum(request_payload.model_dump(mode="json"))
    if request_payload.checksum != expected_checksum:
        raise ValueError("request_code_checksum_invalid")
    if not str(request_payload.machine_hash or "").strip():
        raise ValueError("request_code_machine_hash_missing")
    return request_payload
