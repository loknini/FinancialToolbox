from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BusinessError
from app.core.security import now, refresh_token, sign, timestamp, token_hash
from app.db.models import AuthSession, User, UserSettings


def user_out(user: User) -> dict:
    return {
        "id": user.id,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "version": user.version,
        "created_at": timestamp(user.created_at),
    }


def issue(db: Session, settings: Settings, appid: str, openid: str) -> dict:
    user = db.scalar(select(User).where(User.appid == appid, User.openid == openid))
    if user is None:
        user = User(appid=appid, openid=openid)
        db.add(user)
        db.flush()
        db.add(UserSettings(user_id=user.id))
    if user.status != "active":
        raise BusinessError("UNAUTHORIZED", "账号不可用", 401)
    refresh = refresh_token()
    auth = AuthSession(
        user_id=user.id,
        refresh_hash=token_hash(refresh),
        refresh_expires_at=now() + timedelta(seconds=settings.refresh_token_seconds),
    )
    db.add(auth)
    db.flush()
    return auth_result(settings, auth, user, refresh)


def auth_result(settings: Settings, auth: AuthSession, user: User, refresh: str) -> dict:
    return {
        "access_token": sign(
            settings, "access", {"sub": user.id, "sid": auth.id}, settings.access_token_seconds
        ),
        "token_type": "Bearer",
        "expires_in": settings.access_token_seconds,
        "refresh_token": refresh,
        "refresh_expires_in": settings.refresh_token_seconds,
        "user": user_out(user),
    }


def rotate(db: Session, settings: Settings, old_refresh: str) -> dict:
    old_hash = token_hash(old_refresh)
    auth = db.scalar(
        select(AuthSession).where(
            AuthSession.refresh_hash == old_hash,
            AuthSession.revoked_at.is_(None),
            AuthSession.refresh_expires_at > now(),
        )
    )
    if auth is None:
        raise BusinessError("UNAUTHORIZED", "刷新凭证无效或已过期", 401)
    user = db.get(User, auth.user_id)
    if user is None or user.status != "active":
        raise BusinessError("UNAUTHORIZED", "账号不可用", 401)
    new_refresh = refresh_token()
    expiry = now() + timedelta(seconds=settings.refresh_token_seconds)
    result = db.execute(
        update(AuthSession)
        .where(
            AuthSession.id == auth.id,
            AuthSession.refresh_hash == old_hash,
            AuthSession.revoked_at.is_(None),
        )
        .values(refresh_hash=token_hash(new_refresh), refresh_expires_at=expiry),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        raise BusinessError("UNAUTHORIZED", "刷新凭证已使用", 401)
    return auth_result(settings, auth, user, new_refresh)


def check_version(entity, expected: int):
    if entity.version != expected:
        raise BusinessError("VERSION_CONFLICT", "数据已更新，请刷新后重试", 409)
