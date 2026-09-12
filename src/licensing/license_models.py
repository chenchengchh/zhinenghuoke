from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class LicenseType(str, Enum):
    TRIAL_7D = "trial_7d"
    MONTH_1 = "month_1"
    MONTH_3 = "month_3"
    MONTH_6 = "month_6"
    YEAR_1 = "year_1"
    PERMANENT = "permanent"


class LicenseStatus(str, Enum):
    MISSING = "missing"
    ACTIVE = "active"
    EXPIRED = "expired"
    INVALID = "invalid"
    MACHINE_MISMATCH = "machine_mismatch"
    CLOCK_ROLLBACK = "clock_rollback"


class LicenseFeatures(BaseModel):
    crawler: bool = True
    monitor: bool = True
    auto_reply: bool = True


LICENSE_TYPE_LABELS: dict[LicenseType, str] = {
    LicenseType.TRIAL_7D: "7天免费试用",
    LicenseType.MONTH_1: "1个月",
    LicenseType.MONTH_3: "3个月",
    LicenseType.MONTH_6: "6个月",
    LicenseType.YEAR_1: "1年",
    LicenseType.PERMANENT: "永久授权",
}

LICENSE_TYPE_VALID_DAYS: dict[LicenseType, int] = {
    LicenseType.TRIAL_7D: 7,
    LicenseType.MONTH_1: 30,
    LicenseType.MONTH_3: 90,
    LicenseType.MONTH_6: 180,
    LicenseType.YEAR_1: 365,
}


def get_license_type_label(license_type: LicenseType | None) -> str:
    if license_type is None:
        return ""
    return LICENSE_TYPE_LABELS.get(license_type, str(license_type.value))


def get_license_valid_days(license_type: LicenseType) -> Optional[int]:
    return LICENSE_TYPE_VALID_DAYS.get(license_type)


class LicensePayload(BaseModel):
    schema_version: int = 1
    license_id: str
    license_type: LicenseType
    issued_at: datetime
    expire_at: Optional[datetime] = None
    machine_hash: str = ""
    fingerprint_version: str = ""
    customer_name: str = ""
    phone_number: str = ""
    features: LicenseFeatures = Field(default_factory=LicenseFeatures)
    nonce: str = ""
    signature_alg: str = "hmac-sha256"
    signature: str = ""


class LicenseValidationResult(BaseModel):
    status: LicenseStatus
    valid: bool
    message: str = ""
    license: Optional[LicensePayload] = None
    days_remaining: Optional[int] = None


class ActivationRequest(BaseModel):
    license_key: str
    phone_number: str = ""
    customer_name: str = ""


class ActivationResponse(BaseModel):
    success: bool
    message: str
    status: LicenseStatus
    license: Optional[LicensePayload] = None


class RequestCodePayload(BaseModel):
    request_version: str = "req_v1"
    machine_hash: str
    fingerprint_version: str = ""
    generated_at: datetime
    machine_profile: dict[str, Any] = Field(default_factory=dict)
    checksum: str = ""


class LicenseStateSnapshot(BaseModel):
    status: LicenseStatus
    valid: bool
    activated: bool
    license_type: Optional[LicenseType] = None
    license_type_label: str = ""
    license_id: str = ""
    customer_name: str = ""
    phone_number: str = ""
    expires_at: Optional[str] = None
    days_remaining: Optional[int] = None
    features: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 0
    fingerprint_version: str = ""
    machine_hash: str = ""
    licensed_machine_hash: str = ""
