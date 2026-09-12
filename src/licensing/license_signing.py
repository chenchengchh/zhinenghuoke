from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from src.infrastructure.env_loader import load_project_env
from src.infrastructure.runtime_paths import get_base_dir

load_project_env(base_dir=get_base_dir(), override=False)

LICENSE_SECRET_ENV = "HUOKE_LICENSE_SECRET"
DEFAULT_LICENSE_SECRET = "huoke-local-license-secret"
LICENSE_SIGNATURE_ALG_ENV = "HUOKE_LICENSE_SIGNATURE_ALG"
LICENSE_PRIVATE_KEY_ENV = "HUOKE_LICENSE_PRIVATE_KEY"
LICENSE_PUBLIC_KEY_ENV = "HUOKE_LICENSE_PUBLIC_KEY"

HMAC_SHA256_ALG = "hmac-sha256"
ED25519_ALG = "ed25519"
SUPPORTED_LICENSE_SIGNATURE_ALGS = {HMAC_SHA256_ALG, ED25519_ALG}


def _ensure_project_env_loaded() -> None:
    load_project_env(base_dir=get_base_dir(), override=False)


def _canonical_payload(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _secret_key() -> str:
    _ensure_project_env_loaded()
    return os.getenv(LICENSE_SECRET_ENV, DEFAULT_LICENSE_SECRET).strip() or DEFAULT_LICENSE_SECRET


def _urlsafe_b64decode(value: str) -> bytes:
    normalized = str(value or "").strip()
    if not normalized:
        return b""
    padding = "=" * ((4 - len(normalized) % 4) % 4)
    return base64.urlsafe_b64decode((normalized + padding).encode("utf-8"))


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def get_runtime_hmac_secret() -> str:
    return _secret_key()


def get_preferred_license_signature_alg() -> str:
    _ensure_project_env_loaded()
    configured = str(os.getenv(LICENSE_SIGNATURE_ALG_ENV, "") or "").strip().lower()
    if configured in SUPPORTED_LICENSE_SIGNATURE_ALGS:
        return configured
    if str(os.getenv(LICENSE_PRIVATE_KEY_ENV, "") or "").strip():
        return ED25519_ALG
    return HMAC_SHA256_ALG


def _build_signing_payload(payload: Mapping[str, object], *, signature_alg: str | None = None) -> dict:
    signing_payload = dict(payload)
    signing_payload.pop("signature", None)
    resolved_alg = str(signature_alg or signing_payload.get("signature_alg") or get_preferred_license_signature_alg()).strip().lower()
    if resolved_alg not in SUPPORTED_LICENSE_SIGNATURE_ALGS:
        raise ValueError(f"unsupported_license_signature_alg:{resolved_alg}")
    signing_payload["signature_alg"] = resolved_alg
    return signing_payload


def sign_runtime_state_payload(payload: Mapping[str, object]) -> str:
    signing_payload = dict(payload)
    signing_payload.pop("signature", None)
    raw = _canonical_payload(signing_payload)
    digest = hmac.new(_secret_key().encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).digest()
    return _urlsafe_b64encode(digest)


def verify_runtime_state_signature(payload: Mapping[str, object], signature: str) -> bool:
    expected_signature = sign_runtime_state_payload(payload)
    return hmac.compare_digest(str(signature or "").strip(), expected_signature)


def sign_signed_payload(payload: Mapping[str, object]) -> str:
    return sign_runtime_state_payload(payload)


def _get_private_key() -> Ed25519PrivateKey:
    _ensure_project_env_loaded()
    raw_value = str(os.getenv(LICENSE_PRIVATE_KEY_ENV, "") or "").strip()
    if not raw_value:
        raise ValueError("license_private_key_missing")
    key_bytes = _urlsafe_b64decode(raw_value)
    if len(key_bytes) != 32:
        raise ValueError("license_private_key_invalid")
    return Ed25519PrivateKey.from_private_bytes(key_bytes)


def _get_public_key() -> Ed25519PublicKey:
    _ensure_project_env_loaded()
    raw_value = str(os.getenv(LICENSE_PUBLIC_KEY_ENV, "") or "").strip()
    if raw_value:
        key_bytes = _urlsafe_b64decode(raw_value)
        if len(key_bytes) != 32:
            raise ValueError("license_public_key_invalid")
        return Ed25519PublicKey.from_public_bytes(key_bytes)
    return _get_private_key().public_key()


def sign_license_payload(payload: Mapping[str, object], *, signature_alg: str | None = None) -> str:
    signing_payload = _build_signing_payload(payload, signature_alg=signature_alg)
    raw = _canonical_payload(signing_payload).encode("utf-8")
    resolved_alg = signing_payload["signature_alg"]
    if resolved_alg == HMAC_SHA256_ALG:
        digest = hmac.new(_secret_key().encode("utf-8"), raw, hashlib.sha256).digest()
        return _urlsafe_b64encode(digest)
    if resolved_alg == ED25519_ALG:
        return _urlsafe_b64encode(_get_private_key().sign(raw))
    raise ValueError(f"unsupported_license_signature_alg:{resolved_alg}")


def verify_license_payload_signature(payload: Mapping[str, object]) -> bool:
    signing_payload = _build_signing_payload(payload, signature_alg=str(payload.get("signature_alg") or "").strip().lower() or None)
    raw = _canonical_payload(signing_payload).encode("utf-8")
    signature = str(payload.get("signature", "") or "").strip()
    resolved_alg = signing_payload["signature_alg"]
    if resolved_alg == HMAC_SHA256_ALG:
        expected_signature = _urlsafe_b64encode(hmac.new(_secret_key().encode("utf-8"), raw, hashlib.sha256).digest())
        return hmac.compare_digest(signature, expected_signature)
    if resolved_alg == ED25519_ALG:
        try:
            _get_public_key().verify(_urlsafe_b64decode(signature), raw)
            return True
        except Exception:
            return False
    return False


def generate_ed25519_keypair() -> dict[str, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "signature_alg": ED25519_ALG,
        "private_key": _urlsafe_b64encode(private_bytes),
        "public_key": _urlsafe_b64encode(public_bytes),
    }
