from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.config import Settings


@dataclass(frozen=True, slots=True)
class SessionClaims:
    user_id: str
    role: str
    telegram_user_id: int | None
    telegram_linked: bool
    marzban_username: str
    telegram_handle: str
    display_name: str
    email: str | None
    device_id: str
    device_label: str
    platform: str
    is_premium: bool


class SessionTokenError(RuntimeError):
    pass


def issue_app_token(settings: Settings, claims: SessionClaims) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": claims.user_id,
        "role": claims.role,
        "telegram_user_id": claims.telegram_user_id,
        "telegram_linked": claims.telegram_linked,
        "marzban_username": claims.marzban_username,
        "telegram_handle": claims.telegram_handle,
        "display_name": claims.display_name,
        "email": claims.email,
        "device_id": claims.device_id,
        "device_label": claims.device_label,
        "platform": claims.platform,
        "is_premium": claims.is_premium,
        "iat": now,
        "exp": now + timedelta(days=settings.session_ttl_days),
    }
    return jwt.encode(payload, settings.app_auth_secret, algorithm="HS256")


def decode_app_token(settings: Settings, token: str) -> SessionClaims:
    try:
        payload = jwt.decode(
            token,
            settings.app_auth_secret,
            algorithms=["HS256"],
        )
    except jwt.PyJWTError as exc:
        raise SessionTokenError("Invalid session token.") from exc

    required_fields = (
        "sub",
        "role",
        "marzban_username",
        "telegram_handle",
        "display_name",
        "device_id",
        "device_label",
        "platform",
    )
    for field_name in required_fields:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise SessionTokenError("Session token is missing required fields.")

    telegram_user_id = payload.get("telegram_user_id")
    if telegram_user_id is not None and not isinstance(telegram_user_id, int):
        raise SessionTokenError("Session token is malformed (telegram_user_id).")

    telegram_linked = payload.get("telegram_linked")
    if telegram_linked is None:
        telegram_linked = telegram_user_id is not None
    if not isinstance(telegram_linked, bool):
        raise SessionTokenError("Session token is malformed (telegram_linked).")

    is_premium = payload.get("is_premium")
    if is_premium is None:
        is_premium = False
    if not isinstance(is_premium, bool):
        raise SessionTokenError("Session token is malformed.")

    email = payload.get("email")
    if email is not None and not isinstance(email, str):
        raise SessionTokenError("Session token is malformed.")

    return SessionClaims(
        user_id=payload["sub"],
        role=payload["role"],
        telegram_user_id=telegram_user_id,
        telegram_linked=telegram_linked,
        marzban_username=payload["marzban_username"],
        telegram_handle=payload["telegram_handle"],
        display_name=payload["display_name"],
        email=email,
        device_id=payload["device_id"],
        device_label=payload["device_label"],
        platform=payload["platform"],
        is_premium=is_premium,
    )
