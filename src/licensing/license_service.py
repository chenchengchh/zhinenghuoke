from __future__ import annotations

import base64
import hmac
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.licensing.license_models import (
    ActivationResponse,
    LicenseFeatures,
    LicensePayload,
    LicenseStateSnapshot,
    LicenseStatus,
    LicenseType,
    get_license_type_label,
    get_license_valid_days,
)
from src.licensing.license_signing import get_preferred_license_signature_alg
from src.licensing.license_storage import LicenseRuntimeStateStorage, LicenseStorage
from src.licensing.license_validator import (
    normalize_phone_number,
    sign_license_payload,
    sign_signed_payload,
    validate_license_payload,
)
from src.licensing.machine_fingerprint import build_machine_fingerprint
from src.licensing.machine_fingerprint import FINGERPRINT_VERSION


class LicenseService:
    _clock_rollback_tolerance_seconds = 300

    def __init__(
        self,
        storage: Optional[LicenseStorage] = None,
        runtime_state_storage: Optional[LicenseRuntimeStateStorage] = None,
        machine_hash_provider: Optional[Callable[[], str]] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ):
        self._storage = storage or LicenseStorage()
        self._runtime_state_storage = runtime_state_storage or LicenseRuntimeStateStorage()
        self._machine_hash_provider = machine_hash_provider or build_machine_fingerprint
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def get_machine_hash(self) -> str:
        return self._machine_hash_provider()

    def _get_now(self) -> datetime:
        current_time = self._now_provider()
        if current_time.tzinfo is None:
            return current_time.replace(tzinfo=timezone.utc)
        return current_time.astimezone(timezone.utc)

    def _build_runtime_state_payload(self, license_payload: LicensePayload, current_time: datetime) -> dict:
        payload = {
            "license_id": license_payload.license_id,
            "phone_number": license_payload.phone_number,
            "machine_hash": license_payload.machine_hash,
            "last_verified_at": current_time.isoformat(),
            "signature": "",
        }
        payload["signature"] = sign_signed_payload(payload)
        return payload

    def _persist_runtime_state(self, license_payload: LicensePayload, current_time: datetime) -> None:
        self._runtime_state_storage.save(self._build_runtime_state_payload(license_payload, current_time))

    def _check_clock_rollback(self, license_payload: LicensePayload, current_time: datetime):
        runtime_state = self._runtime_state_storage.load()
        if not runtime_state:
            return None

        signature = str(runtime_state.get("signature", "") or "").strip()
        if not signature:
            from src.licensing.license_models import LicenseValidationResult

            return LicenseValidationResult(
                status=LicenseStatus.CLOCK_ROLLBACK,
                valid=False,
                message="license_runtime_state_invalid",
                license=license_payload,
            )

        expected_signature = sign_signed_payload(runtime_state)
        if not hmac.compare_digest(signature, expected_signature):
            from src.licensing.license_models import LicenseValidationResult

            return LicenseValidationResult(
                status=LicenseStatus.CLOCK_ROLLBACK,
                valid=False,
                message="license_runtime_state_invalid",
                license=license_payload,
            )

        state_license_id = str(runtime_state.get("license_id", "") or "").strip()
        state_phone_number = normalize_phone_number(runtime_state.get("phone_number", ""))
        state_machine_hash = str(runtime_state.get("machine_hash", "") or "").strip()
        if (
            state_license_id != license_payload.license_id
            or state_phone_number != normalize_phone_number(license_payload.phone_number)
            or state_machine_hash != str(license_payload.machine_hash or "").strip()
        ):
            return None

        last_verified_raw = str(runtime_state.get("last_verified_at", "") or "").strip()
        try:
            last_verified_at = datetime.fromisoformat(last_verified_raw)
        except Exception:
            from src.licensing.license_models import LicenseValidationResult

            return LicenseValidationResult(
                status=LicenseStatus.CLOCK_ROLLBACK,
                valid=False,
                message="license_runtime_state_invalid",
                license=license_payload,
            )
        if last_verified_at.tzinfo is None:
            last_verified_at = last_verified_at.replace(tzinfo=timezone.utc)
        else:
            last_verified_at = last_verified_at.astimezone(timezone.utc)

        if current_time.timestamp() + self._clock_rollback_tolerance_seconds < last_verified_at.timestamp():
            from src.licensing.license_models import LicenseValidationResult

            return LicenseValidationResult(
                status=LicenseStatus.CLOCK_ROLLBACK,
                valid=False,
                message="license_clock_rollback_detected",
                license=license_payload,
            )
        return None

    def get_validation_result(self):
        payload = self._storage.load()
        if not payload:
            from src.licensing.license_models import LicenseValidationResult

            return LicenseValidationResult(status=LicenseStatus.MISSING, valid=False, message="license_missing")
        current_time = self._get_now()
        result = validate_license_payload(payload, machine_hash=self.get_machine_hash(), current_time=current_time)
        if not result.license:
            return result

        rollback_result = self._check_clock_rollback(result.license, current_time)
        if rollback_result is not None:
            return rollback_result

        if result.status in {LicenseStatus.ACTIVE, LicenseStatus.EXPIRED}:
            with self._lock:
                self._persist_runtime_state(result.license, current_time)
        return result

    def get_state_snapshot(self) -> LicenseStateSnapshot:
        result = self.get_validation_result()
        license_payload = result.license
        return LicenseStateSnapshot(
            status=result.status,
            valid=result.valid,
            activated=bool(result.valid and license_payload),
            license_type=license_payload.license_type if license_payload else None,
            license_type_label=get_license_type_label(license_payload.license_type if license_payload else None),
            license_id=license_payload.license_id if license_payload else "",
            customer_name=license_payload.customer_name if license_payload else "",
            phone_number=license_payload.phone_number if license_payload else "",
            expires_at=license_payload.expire_at.isoformat() if license_payload and license_payload.expire_at else None,
            days_remaining=result.days_remaining,
            features=license_payload.features.model_dump() if license_payload else {},
            schema_version=license_payload.schema_version if license_payload else 0,
            fingerprint_version=license_payload.fingerprint_version if license_payload else "",
            machine_hash=self.get_machine_hash(),
            licensed_machine_hash=license_payload.machine_hash if license_payload else "",
        )

    def is_feature_enabled(self, feature_name: str) -> bool:
        snapshot = self.get_state_snapshot()
        if not snapshot.valid:
            return False
        if not snapshot.features:
            return False
        return bool(snapshot.features.get(feature_name, False))

    def _decode_license_key(self, license_key: str) -> dict:
        normalized = str(license_key or "").strip()
        decoded = base64.urlsafe_b64decode(normalized.encode("utf-8") + b"===")
        return json.loads(decoded.decode("utf-8"))

    def activate(self, license_key: str, phone_number: str, customer_name: str = "") -> ActivationResponse:
        normalized_phone_number = normalize_phone_number(phone_number)
        if not normalized_phone_number:
            return ActivationResponse(success=False, message="请输入注册手机号", status=LicenseStatus.INVALID)

        try:
            payload = self._decode_license_key(license_key)
        except Exception:
            return ActivationResponse(success=False, message="激活码格式无效", status=LicenseStatus.INVALID)

        payload_phone_number = normalize_phone_number(payload.get("phone_number", ""))
        payload_customer_name = str(payload.get("customer_name", "") or "").strip()

        if not payload_phone_number:
            return ActivationResponse(success=False, message="该激活码未绑定手机号，请联系供应商重新获取使用权限", status=LicenseStatus.INVALID)

        if payload_phone_number != normalized_phone_number:
            return ActivationResponse(success=False, message="手机号与激活码不匹配，请输入生成该激活码时绑定的手机号", status=LicenseStatus.INVALID)

        if customer_name and payload_customer_name and customer_name.strip() != payload_customer_name:
            return ActivationResponse(success=False, message="客户名称与激活码登记信息不匹配", status=LicenseStatus.INVALID)

        current_time = self._get_now()
        current_machine_hash = self.get_machine_hash()
        result = validate_license_payload(payload, machine_hash=current_machine_hash, current_time=current_time)
        if not result.valid:
            message = result.message or "激活失败"
            if result.status == LicenseStatus.EXPIRED:
                message = "授权已到期，请和供应商联系获取使用权限"
            elif result.status == LicenseStatus.MACHINE_MISMATCH:
                message = "验证码已绑定其他设备"
            elif result.status == LicenseStatus.CLOCK_ROLLBACK:
                message = "检测到系统时间被回拨，请校准电脑时间后重试，或联系供应商获取使用权限"
            elif result.message == "license_signature_invalid":
                message = "激活码签名校验失败，请联系供应商重新获取使用权限"
            return ActivationResponse(success=False, message=message, status=result.status)

        with self._lock:
            self._storage.save(payload)
            self._persist_runtime_state(result.license or LicensePayload.model_validate(payload), current_time)

        return ActivationResponse(success=True, message="激活成功", status=result.status, license=result.license)

    def deactivate(self) -> ActivationResponse:
        with self._lock:
            self._storage.clear()
            self._runtime_state_storage.clear()
        return ActivationResponse(success=True, message="授权已清除", status=LicenseStatus.MISSING)

    def generate_license_key(
        self,
        license_type: LicenseType,
        customer_name: str = "",
        phone_number: str = "",
        machine_hash: str = "",
        features: Optional[LicenseFeatures] = None,
        valid_days: Optional[int] = None,
        license_id: str = "",
    ) -> str:
        issued_at = datetime.now(timezone.utc)
        normalized_phone_number = normalize_phone_number(phone_number)
        if not normalized_phone_number:
            raise ValueError("手机号不能为空")

        expire_at = None
        days_to_expire = valid_days or get_license_valid_days(license_type)
        if days_to_expire is not None:
            expire_at = issued_at + timedelta(days=int(days_to_expire))
        payload = LicensePayload(
            schema_version=2,
            license_id=license_id or f"LIC-{issued_at.strftime('%Y%m%d%H%M%S')}",
            license_type=license_type,
            issued_at=issued_at,
            expire_at=expire_at,
            machine_hash=str(machine_hash or "").strip(),
            fingerprint_version=FINGERPRINT_VERSION if machine_hash else "",
            customer_name=customer_name,
            phone_number=normalized_phone_number,
            features=features or LicenseFeatures(),
            nonce=secrets.token_hex(8),
            signature_alg=get_preferred_license_signature_alg(),
            signature="",
        )
        payload_dict = payload.model_dump(mode="json")
        payload_dict["signature"] = sign_license_payload(payload_dict)
        raw = json.dumps(payload_dict, ensure_ascii=False, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8").rstrip("=")

    def generate_demo_license_key(self, license_type: LicenseType, customer_name: str = "") -> str:
        return self.generate_license_key(
            license_type=license_type,
            customer_name=customer_name,
            phone_number="13800138000",
            license_id=f"DEMO-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        )


_license_service: Optional[LicenseService] = None
_license_lock = threading.Lock()


def get_license_service() -> LicenseService:
    global _license_service
    if _license_service is None:
        with _license_lock:
            if _license_service is None:
                _license_service = LicenseService()
    return _license_service
