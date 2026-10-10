from fastapi import APIRouter, Request

from app.api.dependencies import DB, Current, ok
from app.core.errors import BusinessError
from app.core.security import now
from app.schemas.auth import AuthResult, DevLogin, RefreshInput, WechatLogin
from app.schemas.common import EmptyInput, Envelope
from app.services.auth import issue, rotate

router = APIRouter(prefix="/auth", tags=["登录"])


@router.post("/wechat-login", response_model=Envelope[AuthResult])
def wechat_login(body: WechatLogin, request: Request, db: DB):
    settings = request.app.state.settings
    openid = request.app.state.wechat.exchange(body.code)
    return ok(request, issue(db, settings, settings.wechat_app_id, openid))


@router.post("/dev-login", response_model=Envelope[AuthResult])
def dev_login(body: DevLogin, request: Request, db: DB):
    settings = request.app.state.settings
    if not settings.enable_dev_login or settings.environment == "production":
        raise BusinessError("CAPABILITY_DISABLED", "开发登录未启用", 403)
    return ok(request, issue(db, settings, "development", "dev:" + body.account))


@router.post("/refresh", response_model=Envelope[AuthResult])
def refresh(body: RefreshInput, request: Request, db: DB):
    return ok(request, rotate(db, request.app.state.settings, body.refresh_token))


@router.post("/logout", response_model=Envelope[dict[str, bool]])
def logout(body: EmptyInput, request: Request, principal: Current):
    principal.session.revoked_at = now()
    return ok(request, {"logged_out": True})
