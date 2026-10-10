from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.dependencies import DB, Current, ok
from app.core.security import timestamp
from app.db.models import UserSettings
from app.schemas.auth import (
    NotificationOut,
    NotificationPatch,
    PrivacyOut,
    PrivacyPatch,
    SecurityOut,
    UserOut,
    UserPatch,
)
from app.schemas.common import Envelope
from app.services.auth import check_version, user_out

router = APIRouter(prefix="/users/me", tags=["用户"])


def settings_row(db, user_id):
    return db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))


def notification_out(row):
    return {
        "version": row.version,
        "in_app_task_updates": row.in_app_task_updates,
        "wechat_subscription_available": False,
    }


def privacy_out(row):
    return {
        "version": row.version,
        "retain_chat_history": row.retain_chat_history,
        "applies_to": "future_sessions",
        "retention_policy": {
            "chat_days": None if row.retain_chat_history else 1,
            "files_days": None,
            "export_days": None,
        },
    }


@router.get("", response_model=Envelope[UserOut])
def me(request: Request, principal: Current):
    return ok(request, user_out(principal.user))


@router.patch("", response_model=Envelope[UserOut])
def patch_me(body: UserPatch, request: Request, db: DB, principal: Current):
    check_version(principal.user, body.expected_version)
    principal.user.display_name = body.display_name
    db.flush()
    return ok(request, user_out(principal.user))


@router.get("/security", response_model=Envelope[SecurityOut])
def security(request: Request, principal: Current):
    return ok(
        request,
        {
            "account_status": principal.user.status,
            "wechat_bound": principal.user.appid != "development",
            "last_login_at": timestamp(principal.session.last_login_at),
            "account_deletion_available": False,
            "support_contact": "微信客服",
        },
    )


@router.get("/notification-settings", response_model=Envelope[NotificationOut])
def notifications(request: Request, db: DB, principal: Current):
    return ok(request, notification_out(settings_row(db, principal.user.id)))


@router.patch("/notification-settings", response_model=Envelope[NotificationOut])
def patch_notifications(body: NotificationPatch, request: Request, db: DB, principal: Current):
    row = settings_row(db, principal.user.id)
    check_version(row, body.expected_version)
    row.in_app_task_updates = body.in_app_task_updates
    db.flush()
    return ok(request, notification_out(row))


@router.get("/privacy-settings", response_model=Envelope[PrivacyOut])
def privacy(request: Request, db: DB, principal: Current):
    return ok(request, privacy_out(settings_row(db, principal.user.id)))


@router.patch("/privacy-settings", response_model=Envelope[PrivacyOut])
def patch_privacy(body: PrivacyPatch, request: Request, db: DB, principal: Current):
    row = settings_row(db, principal.user.id)
    check_version(row, body.expected_version)
    row.retain_chat_history = body.retain_chat_history
    db.flush()
    return ok(request, privacy_out(row))
