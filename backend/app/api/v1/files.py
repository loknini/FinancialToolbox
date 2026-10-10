import hashlib
from datetime import timedelta
from typing import Annotated
from uuid import uuid4

import anyio
from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from app.api.dependencies import DB, Current, Key, ok
from app.core.errors import BusinessError, not_found
from app.core.security import decode, now
from app.db.models import File, UploadPart, UploadSession
from app.schemas.common import EmptyInput, Envelope
from app.schemas.files import (
    CompleteOut,
    DownloadOut,
    FileOut,
    PartNumbers,
    PartUrlsOut,
    UploadComplete,
    UploadCreate,
    UploadOut,
)
from app.services import files, idempotency

router = APIRouter(tags=["文件"])


@router.post("/upload-sessions", status_code=201, response_model=Envelope[UploadOut])
def create(body: UploadCreate, request: Request, db: DB, principal: Current, key: Key):
    value = idempotency.execute(
        db,
        principal.user.id,
        "upload.create",
        key,
        body.model_dump(),
        lambda: files.create_upload(
            db, request.app.state.settings, principal.user.id, body, request.app.state.storage
        ),
    )
    return ok(request, value)


@router.get("/upload-sessions/{resource_id}", response_model=Envelope[UploadOut])
def detail(resource_id: str, request: Request, db: DB, principal: Current):
    row = files.owned(db, UploadSession, resource_id, principal.user.id)
    if request.app.state.storage.remote:
        db.refresh(row, with_for_update=True)
    if row.expires_at > now():
        files.sync_parts(db, row, request.app.state.storage)
    return ok(request, files.upload_out(db, row))


@router.post("/upload-sessions/{resource_id}/part-urls", response_model=Envelope[PartUrlsOut])
def urls(resource_id: str, body: PartNumbers, request: Request, db: DB, principal: Current):
    row = files.owned(db, UploadSession, resource_id, principal.user.id)
    return ok(
        request,
        files.part_urls(
            request.app.state.settings, row, body.part_numbers, request.app.state.storage
        ),
    )


@router.post(
    "/upload-sessions/{resource_id}/complete", status_code=202, response_model=Envelope[CompleteOut]
)
def complete(
    resource_id: str, body: UploadComplete, request: Request, db: DB, principal: Current, key: Key
):
    with request.app.state.upload_lock(resource_id):
        row = files.owned(db, UploadSession, resource_id, principal.user.id)
        db.refresh(row, with_for_update=True)
        value = idempotency.execute(
            db,
            principal.user.id,
            "upload.complete:" + row.id,
            key,
            body.model_dump(),
            lambda: files.complete_upload(db, row, body, request.app.state.storage),
        )
        db.commit()
    return ok(request, value)


@router.post("/upload-sessions/{resource_id}/renew", response_model=Envelope[UploadOut])
def renew(resource_id: str, body: EmptyInput, request: Request, db: DB, principal: Current):
    with request.app.state.upload_lock(resource_id):
        row = files.owned(db, UploadSession, resource_id, principal.user.id)
        db.refresh(row, with_for_update=True)
        files.active_upload(row)
        row.expires_at = now() + timedelta(
            seconds=request.app.state.settings.upload_session_seconds
        )
        db.flush()
        value = files.upload_out(db, row)
        db.commit()
    return ok(request, value)


@router.delete("/upload-sessions/{resource_id}", response_model=Envelope[dict[str, str]])
def cancel(resource_id: str, request: Request, db: DB, principal: Current):
    with request.app.state.upload_lock(resource_id):
        row = files.owned(db, UploadSession, resource_id, principal.user.id)
        db.refresh(row, with_for_update=True)
        if row.status not in ("uploading", "cancelled"):
            raise BusinessError("STATE_CONFLICT", "上传已提交，不能取消上传会话", 409)
        if request.app.state.storage.remote:
            request.app.state.storage.abort(row)
        row.status = "cancelled"
        db.commit()
    return ok(request, {"id": row.id, "status": row.status})


@router.get("/files/{resource_id}", response_model=Envelope[FileOut])
def file_detail(resource_id: str, request: Request, db: DB, principal: Current):
    return ok(request, files.file_out(files.owned(db, File, resource_id, principal.user.id)))


@router.get("/files/{resource_id}/download-url", response_model=Envelope[DownloadOut])
def download_url(resource_id: str, request: Request, db: DB, principal: Current):
    return ok(
        request,
        files.download(
            request.app.state.settings,
            files.owned(db, File, resource_id, principal.user.id),
            request.app.state.storage,
        ),
    )


@router.put("/storage/parts", include_in_schema=False)
async def upload_part(request: Request, db: DB, token: Annotated[str, Query(max_length=4096)]):
    if request.app.state.storage.remote:
        raise BusinessError("CAPABILITY_DISABLED", "云存储使用直传地址", 403)
    claims = decode(request.app.state.settings, token, "upload")
    upload_id, number = claims.get("upload_id"), claims.get("part")
    lock = request.app.state.upload_lock(upload_id)
    await anyio.to_thread.run_sync(lock.acquire)
    temporary = None
    try:
        row = db.get(UploadSession, upload_id)
        if row is None or not isinstance(number, int):
            raise not_found()
        files.active_upload(row)
        if number < 1 or number > files.part_count(row):
            raise BusinessError("VALIDATION_ERROR", "分片编号无效")
        expected = min(
            row.part_size_bytes, int(row.size_bytes) - (number - 1) * row.part_size_bytes
        )
        target = request.app.state.storage.part_path(row.id, number)
        temporary = target.with_name(target.name + "." + str(uuid4()) + ".tmp")
        digest = hashlib.sha256()
        size = 0
        with temporary.open("wb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > expected:
                    raise BusinessError("PART_SIZE_MISMATCH", "分片大小超出预期")
                digest.update(chunk)
                stream.write(chunk)
        if size != expected:
            raise BusinessError("PART_SIZE_MISMATCH", "分片大小与预期不一致")
        etag = '"' + digest.hexdigest() + '"'
        part = db.scalar(
            select(UploadPart).where(
                UploadPart.session_id == row.id, UploadPart.part_number == number
            )
        )
        if part is None:
            part = UploadPart(session_id=row.id, part_number=number, etag=etag, size_bytes=size)
            db.add(part)
        else:
            part.etag, part.size_bytes = etag, size
        # Persist metadata before releasing the local per-session lock.
        temporary.replace(target)
        db.commit()
        return JSONResponse({"etag": etag}, headers={"ETag": etag})
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
        lock.release()


@router.get("/storage/download", include_in_schema=False)
def download_file(request: Request, db: DB, token: Annotated[str, Query(max_length=4096)]):
    if request.app.state.storage.remote:
        raise BusinessError("CAPABILITY_DISABLED", "云存储使用授权下载地址", 403)
    claims = decode(request.app.state.settings, token, "download")
    row = db.get(File, claims.get("file_id"))
    if row is None:
        raise not_found()
    # Re-check state on actual download, not only when signing the URL.
    files.download(request.app.state.settings, row)
    path = request.app.state.storage.object_path(row.object_key)
    if not path.is_file():
        raise not_found()
    return FileResponse(
        path,
        filename=row.filename,
        media_type=row.content_type,
        headers={"Cache-Control": "private, no-store"},
    )
