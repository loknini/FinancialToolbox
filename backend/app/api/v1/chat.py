import time
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select

from app.api.dependencies import DB, Current, Key, ok
from app.core.errors import BusinessError
from app.core.security import decode, now, timestamp
from app.db.models import AuthSession, ChatEvent, ChatMessage, ChatSession, User
from app.schemas.chat import (
    ActionSubmission,
    CancelledRunOut,
    CancelRequest,
    EventPage,
    MessageAccepted,
    MessageOut,
    MessageRequest,
    RunOut,
    SessionCreate,
    SessionOut,
    SessionPatch,
)
from app.schemas.common import Envelope, ErrorResponse, Page
from app.schemas.exports import ShareCreated
from app.services import chat, idempotency
from app.services.auth import check_version
from app.services.pagination import paginate

router = APIRouter(prefix="/chat", tags=["聊天助手"])


@router.post("/sessions", status_code=201, response_model=Envelope[SessionOut])
def create_session(body: SessionCreate, request: Request, db: DB, principal: Current, key: Key):
    return ok(
        request,
        idempotency.execute(
            db,
            principal.user.id,
            "chat.session.create",
            key,
            body.model_dump(),
            lambda: chat.create_session(db, principal.user.id, body),
        ),
    )


@router.get("/sessions", response_model=Envelope[Page[SessionOut]])
def sessions(
    request: Request,
    db: DB,
    principal: Current,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = select(ChatSession).where(
        ChatSession.user_id == principal.user.id,
        ChatSession.deleted_at.is_(None),
        or_(ChatSession.expires_at.is_(None), ChatSession.expires_at > now()),
    )
    return ok(
        request,
        paginate(
            db,
            request.app.state.settings,
            query,
            ChatSession,
            {"operation": "chat.sessions", "user_id": principal.user.id},
            cursor,
            limit,
            lambda row: chat.session_out(db, row),
        ),
    )


@router.get("/sessions/{session_id}", response_model=Envelope[SessionOut])
def session_detail(session_id: str, request: Request, db: DB, principal: Current):
    return ok(
        request,
        chat.session_out(db, chat.session_owned(db, session_id, principal.user.id, lock=True)),
    )


@router.patch("/sessions/{session_id}", response_model=Envelope[SessionOut])
def rename(session_id: str, body: SessionPatch, request: Request, db: DB, principal: Current):
    row = chat.session_owned(db, session_id, principal.user.id, lock=True)
    check_version(row, body.expected_version)
    row.title = body.title
    db.flush()
    return ok(request, chat.session_out(db, row))


@router.delete("/sessions/{session_id}", response_model=Envelope[dict])
def delete_session(session_id: str, request: Request, db: DB, principal: Current):
    return ok(request, chat.delete_session(db, principal.user.id, session_id))


@router.get("/sessions/{session_id}/messages", response_model=Envelope[Page[MessageOut]])
def messages(
    session_id: str,
    request: Request,
    db: DB,
    principal: Current,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    chat.session_owned(db, session_id, principal.user.id)
    return ok(
        request,
        paginate(
            db,
            request.app.state.settings,
            select(ChatMessage).where(ChatMessage.session_id == session_id),
            ChatMessage,
            {"operation": "chat.messages", "session_id": session_id, "user_id": principal.user.id},
            cursor,
            limit,
            chat.message_out,
        ),
    )


@router.post(
    "/sessions/{session_id}/messages", status_code=202, response_model=Envelope[MessageAccepted]
)
def send(
    session_id: str, body: MessageRequest, request: Request, db: DB, principal: Current, key: Key
):
    chat.session_owned(db, session_id, principal.user.id)
    return ok(
        request,
        idempotency.execute(
            db,
            principal.user.id,
            "chat.message:" + session_id,
            key,
            body.model_dump(mode="json"),
            lambda: chat.send_message(
                db,
                request.app.state.settings,
                request.app.state.model,
                principal.user.id,
                session_id,
                body,
            ),
        ),
    )


@router.get("/sessions/{session_id}/events", response_model=Envelope[EventPage])
def events(
    session_id: str,
    request: Request,
    db: DB,
    principal: Current,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    wait_seconds: Annotated[int, Query(ge=0, le=25)] = 20,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    user_id, auth_id = principal.user.id, principal.session.id
    deadline = time.monotonic() + wait_seconds
    while True:
        # Each poll takes a fresh transaction; disconnect does not hold DB locks.
        decode(request.app.state.settings, request.headers["Authorization"].split()[1], "access")
        session = chat.session_owned(db, session_id, user_id)
        auth = db.get(AuthSession, auth_id)
        user = db.get(User, user_id)
        if (
            not auth
            or auth.revoked_at
            or auth.refresh_expires_at <= now()
            or not user
            or user.status != "active"
        ):
            raise BusinessError("UNAUTHORIZED", "登录态已失效", 401)
        if after_sequence > session.last_sequence:
            raise BusinessError("INVALID_CURSOR", "事件游标超出当前会话", 400)
        rows = list(
            db.scalars(
                select(ChatEvent)
                .where(ChatEvent.session_id == session_id, ChatEvent.sequence > after_sequence)
                .order_by(ChatEvent.sequence)
                .limit(limit + 1)
            )
        )
        if rows or time.monotonic() >= deadline:
            items = [
                {
                    "sequence": row.sequence,
                    "run_id": row.run_id,
                    "type": row.type,
                    "created_at": timestamp(row.created_at),
                    "payload": row.payload,
                }
                for row in rows[:limit]
            ]
            return ok(
                request,
                {
                    "items": items,
                    "next_sequence": items[-1]["sequence"] if items else after_sequence,
                    "has_more": len(rows) > limit,
                },
            )
        db.rollback()
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


@router.get("/runs/{run_id}", response_model=Envelope[RunOut])
def run_detail(run_id: str, request: Request, db: DB, principal: Current):
    run, _ = chat.run_owned(db, run_id, principal.user.id, lock=True)
    return ok(request, chat.run_out(db, run))


@router.post("/runs/{run_id}/cancel", response_model=Envelope[CancelledRunOut])
def cancel(run_id: str, body: CancelRequest, request: Request, db: DB, principal: Current):
    return ok(request, chat.cancel_run(db, principal.user.id, run_id, body.cancel_linked_tasks))


@router.post(
    "/actions/{action_id}/submit",
    status_code=202,
    response_model=Envelope[RunOut],
    responses={
        409: {"model": Envelope[RunOut] | ErrorResponse},
        410: {"model": Envelope[RunOut] | ErrorResponse},
    },
)
def submit(action_id: str, body: ActionSubmission, request: Request, db: DB, principal: Current):
    status, data, code = chat.submit_action(
        db, request.app.state.settings, principal.user.id, action_id, body
    )
    if code:
        # Preserve replacement/expiry events instead of rolling back via exception handling.
        return JSONResponse(
            {
                **ok(request, data),
                "code": code,
                "message": "操作已过期" if status == 410 else "范围已变化，请确认新操作",
                "details": [],
                "retryable": False,
            },
            status_code=status,
        )
    return ok(request, data)


@router.get("/actions/{action_id}/result", response_model=Envelope[ShareCreated])
def action_result(action_id: str, request: Request, db: DB, principal: Current):
    return ok(
        request, chat.action_result(db, request.app.state.settings, principal.user.id, action_id)
    )
