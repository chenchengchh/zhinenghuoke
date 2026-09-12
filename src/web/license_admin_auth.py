import hashlib
import hmac
import os
from typing import Optional

from fastapi import HTTPException, Request


LICENSE_ADMIN_COOKIE_NAME = "huoke_license_admin"


def get_license_admin_credentials() -> tuple[str, str]:
    username = str(os.getenv("HUOKE_LICENSE_ADMIN_USER", "") or "").strip()
    password = str(os.getenv("HUOKE_LICENSE_ADMIN_PASSWORD", "") or "")
    return username, password


def _get_license_admin_secret() -> str:
    configured_secret = str(os.getenv("HUOKE_LICENSE_ADMIN_SECRET", "") or "").strip()
    if configured_secret:
        return configured_secret
    username, password = get_license_admin_credentials()
    return f"{username}:{password}:license-admin"


def is_license_admin_configured() -> bool:
    username, password = get_license_admin_credentials()
    return bool(username and password)


def build_license_admin_cookie_value(username: str) -> str:
    normalized_username = str(username or "").strip()
    secret = _get_license_admin_secret()
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{normalized_username}|license-admin".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{normalized_username}:{digest}"


def verify_license_admin_cookie(cookie_value: Optional[str]) -> bool:
    if not is_license_admin_configured():
        return False
    raw_value = str(cookie_value or "").strip()
    if ":" not in raw_value:
        return False
    username, provided_digest = raw_value.split(":", 1)
    configured_username, _ = get_license_admin_credentials()
    if not hmac.compare_digest(str(username or "").strip(), configured_username):
        return False
    expected_value = build_license_admin_cookie_value(configured_username)
    return hmac.compare_digest(raw_value, expected_value)


def require_license_admin(request: Request) -> None:
    if not is_license_admin_configured():
        raise HTTPException(status_code=503, detail="授权管理未配置登录凭据，请先设置服务端环境变量")
    cookie_value = request.cookies.get(LICENSE_ADMIN_COOKIE_NAME, "")
    if not verify_license_admin_cookie(cookie_value):
        raise HTTPException(status_code=401, detail="请先登录授权管理")
