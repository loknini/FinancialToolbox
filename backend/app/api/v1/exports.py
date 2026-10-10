from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.dependencies import DB, Current, Key, ok
from app.core.errors import BusinessError
from app.core.security import decode, now
from app.db.models import Export, ExportFile, ExportShare
from app.schemas.common import EmptyInput, Envelope, Page
from app.schemas.exports import (
    ExportOut,
    ExportRequest,
    ShareCreated,
    ShareOut,
    ShareRequest,
    ShareView,
)
from app.schemas.files import DownloadOut
from app.services import exports, idempotency
from app.services.files import owned
from app.services.pagination import date_range, paginate

router = APIRouter(tags=["导出与分享"])


@router.post("/exports", status_code=202, response_model=Envelope[ExportOut])
def create(body: ExportRequest, request: Request, db: DB, principal: Current, key: Key):
    return ok(
        request,
        idempotency.execute(
            db,
            principal.user.id,
            "export.create",
            key,
            body.model_dump(mode="json"),
            lambda: exports.create_export(db, principal.user.id, body),
        ),
    )


@router.get("/exports", response_model=Envelope[Page[ExportOut]])
def listing(
    request: Request,
    db: DB,
    principal: Current,
    format: Literal["excel", "pdf"] | None = None,
    status: Literal["queued", "processing", "succeeded", "failed", "cancelling", "cancelled"]
    | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    start, end = date_range(date_from, date_to)
    query = select(Export).where(Export.user_id == principal.user.id)
    if format:
        query = query.where(Export.format == format)
    if status:
        query = query.where(Export.status == status)
    if start:
        query = query.where(Export.created_at >= start)
    if end:
        query = query.where(Export.created_at < end)
    return ok(
        request,
        paginate(
            db,
            request.app.state.settings,
            query,
            Export,
            {
                "operation": "exports",
                "user_id": principal.user.id,
                "format": format,
                "status": status,
                "date_from": date_from,
                "date_to": date_to,
            },
            cursor,
            limit,
            lambda row: exports.export_out(db, row),
        ),
    )


@router.get("/exports/{resource_id}", response_model=Envelope[ExportOut])
def detail(resource_id: str, request: Request, db: DB, principal: Current):
    return ok(request, exports.export_out(db, owned(db, Export, resource_id, principal.user.id)))


@router.post("/exports/{resource_id}/cancel", response_model=Envelope[ExportOut])
def cancel(resource_id: str, body: EmptyInput, request: Request, db: DB, principal: Current):
    return ok(request, exports.cancel_export(db, principal.user.id, resource_id))


@router.get("/exports/{resource_id}/download-url", response_model=Envelope[DownloadOut])
def download(resource_id: str, request: Request, db: DB, principal: Current):
    return ok(
        request,
        exports.download(
            db,
            request.app.state.settings,
            request.app.state.storage,
            owned(db, Export, resource_id, principal.user.id),
        ),
    )


@router.post("/exports/{resource_id}/share", status_code=201, response_model=Envelope[ShareCreated])
def share(
    resource_id: str, body: ShareRequest, request: Request, db: DB, principal: Current, key: Key
):
    return ok(
        request,
        exports.create_share(
            db, request.app.state.settings, principal.user.id, resource_id, body, key
        ),
    )


@router.get("/exports/{resource_id}/share", response_model=Envelope[ShareOut | None])
def share_detail(resource_id: str, request: Request, db: DB, principal: Current):
    return ok(request, exports.get_share(db, principal.user.id, resource_id))


@router.delete("/exports/{resource_id}/share", response_model=Envelope[dict[str, bool]])
def revoke(resource_id: str, request: Request, db: DB, principal: Current):
    return ok(request, exports.revoke_share(db, principal.user.id, resource_id))


@router.get("/shares/{token}", response_model=Envelope[ShareView])
def public(token: str, request: Request, db: DB):
    share, row, file = exports.public_share(db, token)
    return ok(
        request,
        {
            "filename": file.filename,
            "format": row.format,
            "size_bytes": str(int(file.size_bytes)),
            "expires_at": exports.timestamp(share.expires_at),
            "description": "此链接仅授权下载本次导出文件",
        },
    )


@router.get("/shares/{token}/download-url", response_model=Envelope[DownloadOut])
def public_download(token: str, request: Request, db: DB):
    share, row, file = exports.public_share(db, token)
    return ok(
        request,
        exports.download(
            db, request.app.state.settings, request.app.state.storage, row, share=share
        ),
    )


@router.get("/storage/export-download", include_in_schema=False)
def transfer(request: Request, db: DB, token: Annotated[str, Query(max_length=4096)]):
    storage = request.app.state.storage
    if storage.remote:
        raise BusinessError("CAPABILITY_DISABLED", "请使用云授权下载地址", 403)
    claims = decode(request.app.state.settings, token, "export_download")
    file = db.get(ExportFile, claims.get("file_id"))
    if file is None:
        raise BusinessError("RESOURCE_NOT_FOUND", "文件不存在", 404)
    if claims.get("share_id"):
        share = db.get(ExportShare, claims["share_id"])
        if share is None or share.export_id != file.export_id or share.revoked_at:
            raise BusinessError("SHARE_UNAVAILABLE", "分享不可用", 404)
        if share.expires_at <= now():
            raise BusinessError("SHARE_EXPIRED", "分享已过期", 410)
    row = db.get(Export, file.export_id)
    exports.available_file(db, row, storage)
    path = storage.object_path(file.object_key)
    if not path.is_file():
        raise BusinessError("STORAGE_UNAVAILABLE", "导出文件不可读取", 503, retryable=True)
    return FileResponse(
        path,
        media_type=file.content_type,
        filename=file.filename,
        headers={"Cache-Control": "private, no-store"},
    )
