from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from src.remote_control.policy_models import RemoteControlPolicy


REMOTE_CONTROL_SECRET_ENV = "HUOKE_REMOTE_CONTROL_SECRET"
DEFAULT_REMOTE_CONTROL_SECRET = "huoke-remote-control-secret"


def _secret_key() -> str:
    return os.getenv(REMOTE_CONTROL_SECRET_ENV, DEFAULT_REMOTE_CONTROL_SECRET).strip() or DEFAULT_REMOTE_CONTROL_SECRET


def sign_remote_policy(payload: dict) -> str:
    signing_payload = dict(payload)
    signing_payload.pop("signature", None)
    raw = json.dumps(signing_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hmac.new(_secret_key().encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def validate_remote_policy(payload: dict, expected_instance_id: str) -> tuple[bool, str, RemoteControlPolicy | None]:
    try:
        parsed = RemoteControlPolicy.model_validate(payload)
    except Exception as exc:
        return False, f"policy_parse_failed:{exc}", None

    expected_signature = sign_remote_policy(parsed.model_dump(mode="json"))
    if not hmac.compare_digest(parsed.signature, expected_signature):
        return False, "policy_signature_invalid", parsed

    if parsed.instance_id and parsed.instance_id != expected_instance_id:
        return False, "policy_instance_mismatch", parsed

    now = datetime.now(timezone.utc)
    if parsed.expire_at is not None:
        expire_at = parsed.expire_at
        if expire_at.tzinfo is None:
            expire_at = expire_at.replace(tzinfo=timezone.utc)
        if expire_at < now:
            return False, "policy_expired", parsed

    return True, "ok", parsed
