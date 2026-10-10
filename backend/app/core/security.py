import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import Settings
from app.core.errors import BusinessError


def now() -> datetime:
    # SQLAlchemy's portable SQLite datetime is UTC-naive; only convert at the API edge.
    return datetime.now(UTC).replace(tzinfo=None)


def timestamp(value: datetime | None) -> str | None:
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z") if value else None


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def refresh_token() -> str:
    return secrets.token_urlsafe(48)


def sign(settings: Settings, purpose: str, claims: dict[str, Any], seconds: int) -> str:
    instant = datetime.now(UTC)
    payload = {
        **claims,
        "purpose": purpose,
        "iss": "financial-toolbox",
        "iat": instant,
        "exp": instant + timedelta(seconds=seconds),
    }
    return jwt.encode(payload, settings.auth_signing_key.get_secret_value(), algorithm="HS256")


def decode(settings: Settings, token: str, purpose: str) -> dict[str, Any]:
    try:
        data = jwt.decode(
            token,
            settings.auth_signing_key.get_secret_value(),
            algorithms=["HS256"],
            issuer="financial-toolbox",
            options={"require": ["exp", "iat", "iss", "purpose"]},
        )
        if data["purpose"] != purpose:
            raise jwt.InvalidTokenError("wrong purpose")
        return data
    except jwt.ExpiredSignatureError as exc:
        raise BusinessError("TOKEN_EXPIRED", "凭证已过期", 401) from exc
    except jwt.InvalidTokenError as exc:
        raise BusinessError("UNAUTHORIZED", "无效凭证", 401) from exc
