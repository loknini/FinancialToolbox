from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BusinessError, not_found
from app.core.security import now, sign, timestamp
from app.db.models import File, Job, UploadPart, UploadSession
from app.schemas.files import UploadComplete, UploadCreate


def owned(db: Session, model, resource_id: str, user_id: str):
    value = db.scalar(select(model).where(model.id == resource_id, model.user_id == user_id))
    if value is None:
        raise not_found()
    return value


def active_upload(row: UploadSession):
    if row.expires_at <= now():
        raise BusinessError("UPLOAD_EXPIRED", "上传会话已过期", 410)
    if row.status != "uploading":
        raise BusinessError("STATE_CONFLICT", "上传会话已结束或正在处理", 409)


def part_count(row: UploadSession):
    return (int(row.size_bytes) + row.part_size_bytes - 1) // row.part_size_bytes


def upload_out(db: Session, row: UploadSession):
    parts = db.scalars(
        select(UploadPart).where(UploadPart.session_id == row.id).order_by(UploadPart.part_number)
    ).all()
    return {
        "id": row.id,
        "status": "expired"
        if row.expires_at <= now() and row.status == "uploading"
        else row.status,
        "filename": row.filename,
        "size_bytes": str(int(row.size_bytes)),
        "purpose": row.purpose,
        "part_size_bytes": row.part_size_bytes,
        "max_part_count": part_count(row),
        "suggested_concurrency": 1,
        "expires_at": timestamp(row.expires_at),
        "parts": [{"part_number": p.part_number, "etag": p.etag} for p in parts],
        "file_id": row.file_id,
    }


def create_upload(db: Session, settings: Settings, user_id: str, data: UploadCreate, storage=None):
    part_size = settings.upload_part_bytes
    if storage and storage.remote:
        part_size = max(
            storage.min_part_bytes,
            part_size,
            (int(data.size_bytes) + storage.max_parts - 1) // storage.max_parts,
        )
        if part_size > storage.max_part_bytes:
            raise BusinessError("INFRASTRUCTURE_FILE_LIMIT", "文件超过当前云存储的技术容量", 413)
    row = UploadSession(
        user_id=user_id,
        filename=data.filename,
        content_type=data.content_type,
        size_bytes=int(data.size_bytes),
        purpose=data.purpose,
        part_size_bytes=part_size,
        storage_backend=settings.storage_backend,
        expires_at=now() + timedelta(seconds=settings.upload_session_seconds),
    )
    db.add(row)
    db.flush()
    if storage and storage.remote:
        row.remote_upload_id = storage.begin(row.id, row.content_type)
        db.flush()
    return upload_out(db, row)


def sync_parts(db, row, storage):
    if not storage.remote or row.status != "uploading":
        return
    active_upload(row)
    reported = storage.list_parts(row)
    db.execute(delete(UploadPart).where(UploadPart.session_id == row.id))
    for part in reported:
        number = part["part_number"]
        if number < 1 or number > part_count(row):
            raise BusinessError("VALIDATION_ERROR", "云端分片编号与上传计划不一致")
        expected = min(
            row.part_size_bytes, int(row.size_bytes) - (number - 1) * row.part_size_bytes
        )
        if part["size_bytes"] != expected:
            raise BusinessError("PART_SIZE_MISMATCH", "云端分片长度与上传计划不一致")
        db.add(UploadPart(session_id=row.id, **part))
    db.flush()


def part_urls(settings: Settings, row: UploadSession, numbers: list[int], storage=None):
    active_upload(row)
    if len(set(numbers)) != len(numbers) or any(n < 1 or n > part_count(row) for n in numbers):
        raise BusinessError("VALIDATION_ERROR", "分片编号不在当前文件范围内")
    expiry = now() + timedelta(seconds=settings.signed_url_seconds)
    if storage and storage.remote:
        seconds = max(
            1, min(settings.signed_url_seconds, int((row.expires_at - now()).total_seconds()))
        )
        return {
            "parts": [
                {
                    "part_number": n,
                    "method": "PUT",
                    "headers": {"Content-Type": "application/octet-stream"},
                    "url": storage.part_url(row, n, seconds),
                    "expires_at": timestamp(now() + timedelta(seconds=seconds)),
                }
                for n in numbers
            ]
        }

    return {
        "parts": [
            {
                "part_number": n,
                "method": "PUT",
                "headers": {"Content-Type": "application/octet-stream"},
                "url": settings.public_base_url.rstrip("/")
                + "/api/v1/storage/parts?token="
                + sign(
                    settings,
                    "upload",
                    {"upload_id": row.id, "part": n},
                    settings.signed_url_seconds,
                ),
                "expires_at": timestamp(expiry),
            }
            for n in numbers
        ]
    }


def complete_upload(db: Session, row: UploadSession, data: UploadComplete, storage=None):
    active_upload(row)
    if storage and storage.remote:
        sync_parts(db, row, storage)
    stored = db.scalars(select(UploadPart).where(UploadPart.session_id == row.id)).all()
    # Avoid constructing a range set for extremely large declared files.
    count = part_count(row)
    provided = {p.part_number: p.etag for p in data.parts}
    if len(provided) != len(data.parts) or len(provided) != count:
        raise BusinessError("VALIDATION_ERROR", "上传分片不完整或重复")
    if len(stored) != count or any(provided.get(p.part_number) != p.etag for p in stored):
        raise BusinessError("VALIDATION_ERROR", "分片标识与已上传内容不一致")
    if sum(p.size_bytes for p in stored) != int(row.size_bytes):
        raise BusinessError("VALIDATION_ERROR", "文件大小与声明不一致")
    file = File(
        user_id=row.user_id,
        upload_session_id=row.id,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        purpose=row.purpose,
        object_key=f"objects/{row.id}",
    )
    db.add(file)
    db.flush()
    row.file_id, row.status = file.id, "completing"
    db.add(Job(type="validate_file", resource_id=file.id, dedupe_key=f"validate:{file.id}"))
    db.flush()
    return {"upload_session_id": row.id, "file_id": file.id, "status": "validating"}


def file_out(row: File):
    return {
        "id": row.id,
        "filename": row.filename,
        "content_type": row.content_type,
        "size_bytes": str(int(row.size_bytes)),
        "purpose": row.purpose,
        "status": row.status,
        "sha256": row.sha256,
        "error": row.error,
        "created_at": timestamp(row.created_at),
        "expires_at": timestamp(row.expires_at),
    }


def download(settings: Settings, row: File, storage=None):
    if row.expires_at and row.expires_at <= now():
        raise BusinessError("FILE_EXPIRED", "文件已过期", 410)
    if row.status != "ready":
        raise BusinessError("FILE_NOT_READY", "文件尚不可下载", 409)
    if storage and storage.remote:
        return {
            "url": storage.download_url(row, settings.signed_url_seconds),
            "expires_at": timestamp(now() + timedelta(seconds=settings.signed_url_seconds)),
            "filename": row.filename,
            "content_type": row.content_type,
            "size_bytes": str(int(row.size_bytes)),
        }
    return {
        "url": settings.public_base_url.rstrip("/")
        + "/api/v1/storage/download?token="
        + sign(settings, "download", {"file_id": row.id}, settings.signed_url_seconds),
        "expires_at": timestamp(now() + timedelta(seconds=settings.signed_url_seconds)),
        "filename": row.filename,
        "content_type": row.content_type,
        "size_bytes": str(int(row.size_bytes)),
    }
