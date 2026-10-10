from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.core.security import decode, now
from app.db.models import AuthSession, User

bearer = HTTPBearer(auto_error=False)


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


DB = Annotated[Session, Depends(get_db, scope="function")]


@dataclass
class Principal:
    user: User
    session: AuthSession


def current_user(
    request: Request,
    db: DB,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    if credentials is None:
        raise BusinessError("UNAUTHORIZED", "请先登录", 401)
    claims = decode(request.app.state.settings, credentials.credentials, "access")
    auth = db.get(AuthSession, claims.get("sid"))
    user = db.get(User, claims.get("sub"))
    if (
        auth is None
        or user is None
        or auth.user_id != user.id
        or auth.revoked_at is not None
        or auth.refresh_expires_at <= now()
        or user.status != "active"
    ):
        raise BusinessError("UNAUTHORIZED", "登录态已失效", 401)
    return Principal(user=user, session=auth)


Current = Annotated[Principal, Depends(current_user)]


def idempotency_key(
    value: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
):
    return value


Key = Annotated[str, Depends(idempotency_key)]


def ok(request: Request, data):
    return {
        "code": "OK",
        "message": "success",
        "request_id": request.state.request_id,
        "data": data,
    }
