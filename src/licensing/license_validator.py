from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Optional

from src.licensing.license_models import (
    LicensePayload,
    LicenseStatus,
    LicenseValidationResult,
)
from src.licensing.license_signing import (
    DEFAULT_LICENSE_SECRET,
    LICENSE_SECRET_ENV,
    sign_license_payload,
    sign_runtime_state_payload,
    verify_license_payload_signature,
    _secret_key,
)


def normalize_phone_number(phone_number: str) -> str:
    return re.sub(r"\D+", "", str(phone_number or "").strip())

def sign_signed_payload(payload: dict) -> str:
    return sign_runtime_state_payload(payload)


def validate_license_payload(
    payload: dict,
    machine_hash: str = "",
    current_time: Optional[datetime] = None,
) -> LicenseValidationResult:
    try:
        parsed = LicensePayload.model_validate(payload)
    except Exception as exc:
        return LicenseValidationResult(status=LicenseStatus.INVALID, valid=False, message=f"license_parse_failed:{exc}")

    if not verify_license_payload_signature(parsed.model_dump(mode="json")):
        return LicenseValidationResult(status=LicenseStatus.INVALID, valid=False, message="license_signature_invalid")

    if not normalize_phone_number(parsed.phone_number):
        return LicenseValidationResult(status=LicenseStatus.INVALID, valid=False, message="license_phone_missing", license=parsed)

    normalized_machine_hash = str(machine_hash or "").strip()
    licensed_machine_hash = str(parsed.machine_hash or "").strip()
    if licensed_machine_hash and normalized_machine_hash and licensed_machine_hash != normalized_machine_hash:
        return LicenseValidationResult(
            status=LicenseStatus.MACHINE_MISMATCH,
            valid=False,
            message="license_machine_mismatch",
            license=parsed,
        )

    now = current_time or datetime.now(timezone.utc)
    if parsed.expire_at is not None:
        expire_at = parsed.expire_at
        if expire_at.tzinfo is None:
            expire_at = expire_at.replace(tzinfo=timezone.utc)
        if expire_at < now:
            return LicenseValidationResult(status=LicenseStatus.EXPIRED, valid=False, message="license_expired", license=parsed, days_remaining=0)
        days_remaining = max(0, (expire_at - now).days)
    else:
        days_remaining = None

    return LicenseValidationResult(
        status=LicenseStatus.ACTIVE,
        valid=True,
        message="ok",
        license=parsed,
        days_remaining=days_remaining,
    )
