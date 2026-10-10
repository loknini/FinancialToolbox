import base64
import hashlib
import secrets
from datetime import timedelta

from Crypto.Cipher import AES
from sqlalchemy import select, update

from app.core.errors import BusinessError
from app.core.security import now, sign, timestamp, token_hash
from app.db.models import (
    Export,
    ExportFile,
    ExportShare,
    ExportSnapshot,
    Invoice,
    InvoiceBatch,
    Job,
    TaxCalculation,
)
from app.services import idempotency, invoices, tax
from app.services.files import owned
from app.services.pagination import date_range

TERMINAL = {"succeeded", "failed", "cancelled"}


def file_for(db, row):
    return db.scalar(select(ExportFile).where(ExportFile.export_id == row.id))


def export_out(db, row):
    file = file_for(db, row)
    return {
        "id": row.id,
        "format": row.format,
        "source_type": row.source_type,
        "status": row.status,
        "record_count": row.record_count,
        "pending_review_count": row.pending_review_count,
        "snapshot_at": timestamp(row.snapshot_at),
        "progress": {"percent": 100 if row.status == "succeeded" else None},
        "file": {
            "id": file.id,
            "status": "expired" if file.expires_at <= now() else "available",
            "filename": file.filename,
            "size_bytes": str(int(file.size_bytes)),
            "expires_at": timestamp(file.expires_at),
        }
        if file
        else None,
        "error": row.error,
        "can_cancel": row.status not in TERMINAL,
        "created_at": timestamp(row.created_at),
    }


def create_export(db, user_id, body):
    model = Invoice if body.source_type == "invoices" else TaxCalculation
    query = select(model).where(model.user_id == user_id)
    choice = body.selection
    if choice.mode == "ids":
        # Validate every requested ID before review filtering. Foreign IDs are never silently dropped.
        found = set(
            db.scalars(select(model.id).where(model.user_id == user_id, model.id.in_(choice.ids)))
        )
        if found != set(choice.ids):
            raise BusinessError("RESOURCE_NOT_FOUND", "资源不存在", 404)
        query = query.where(model.id.in_(choice.ids))
    elif choice.mode == "batch":
        owned(db, InvoiceBatch, choice.batch_id, user_id)
        query = query.where(Invoice.batch_id == choice.batch_id)
    else:
        start, end = date_range(choice.date_from, choice.date_to)
        if start:
            query = query.where(model.created_at >= start)
        if end:
            query = query.where(model.created_at < end)
        if model is Invoice:
            if choice.type_code:
                query = query.where(Invoice.type_code == choice.type_code)
            if choice.invoice_date_from:
                query = query.where(Invoice.invoice_date >= choice.invoice_date_from)
            if choice.invoice_date_to:
                query = query.where(Invoice.invoice_date <= choice.invoice_date_to)
    if model is Invoice and body.review_scope == "confirmed":
        query = query.where(Invoice.review_status == "confirmed")
    export = Export(
        user_id=user_id,
        format=body.format,
        source_type=body.source_type,
        request_data=body.model_dump(mode="json"),
        record_count=0,
        pending_review_count=0,
    )
    db.add(export)
    db.flush()
    rows = db.scalars(query.order_by(model.created_at, model.id).execution_options(yield_per=500))
    for position, row in enumerate(rows):
        data = invoices.invoice_out(row, True) if model is Invoice else tax.serialize(row)
        db.add(ExportSnapshot(export_id=export.id, position=position, data=data))
        export.record_count += 1
        export.pending_review_count += int(data.get("review_status") == "pending_review")
        if position % 500 == 499:
            db.flush()
    if not export.record_count:
        raise BusinessError("EMPTY_EXPORT", "没有符合条件的导出记录", 422)
    db.add(Job(type="generate_export", resource_id=export.id, dedupe_key="export:" + export.id))
    db.flush()
    return export_out(db, export)


def cancel_export(db, user_id, resource_id):
    row = owned(db, Export, resource_id, user_id)
    db.refresh(row, with_for_update=True)
    if row.status not in TERMINAL:
        row.cancel_requested = True
        affected = db.execute(
            update(Job)
            .where(Job.type == "generate_export", Job.resource_id == row.id, Job.status == "queued")
            .values(status="cancelled")
        )
        row.status = "cancelled" if affected.rowcount else "cancelling"
        db.flush()
    return export_out(db, row)


def available_file(db, row, storage=None):
    file = file_for(db, row)
    if row.status != "succeeded" or file is None:
        raise BusinessError("FILE_NOT_READY", "导出文件尚不可用", 409)
    if file.expires_at <= now():
        raise BusinessError("FILE_EXPIRED", "导出文件已过期", 410)
    if storage and file.storage_backend != storage.backend:
        raise BusinessError("STORAGE_BACKEND_MISMATCH", "文件存储配置不一致", 503)
    return file


def download(db, settings, storage, row, *, share=None):
    file = available_file(db, row, storage)
    seconds = min(
        settings.signed_url_seconds, max(1, int((file.expires_at - now()).total_seconds()))
    )
    if share:
        seconds = min(seconds, max(1, int((share.expires_at - now()).total_seconds())))
    if storage.remote:
        url = storage.download_url(file, seconds)
    else:
        claims = {"file_id": file.id}
        if share:
            claims["share_id"] = share.id
        url = (
            settings.public_base_url.rstrip("/")
            + "/api/v1/storage/export-download?token="
            + sign(settings, "export_download", claims, seconds)
        )
    return {
        "url": url,
        "filename": file.filename,
        "content_type": file.content_type,
        "size_bytes": str(int(file.size_bytes)),
        "expires_at": timestamp(now() + timedelta(seconds=seconds)),
    }


def share_out(row):
    return {
        "id": row.id,
        "export_id": row.export_id,
        "status": "revoked"
        if row.revoked_at
        else "expired"
        if row.expires_at <= now()
        else "active",
        "expires_at": timestamp(row.expires_at),
        "created_at": timestamp(row.created_at),
    }


def get_share(db, user_id, export_id):
    export = owned(db, Export, export_id, user_id)
    row = db.get(ExportShare, export.active_share_id) if export.active_share_id else None
    return share_out(row) if row else None


def replay_cipher(settings):
    key = hashlib.sha256(
        b"financial-toolbox-share-replay-v1" + settings.auth_signing_key.get_secret_value().encode()
    ).digest()
    return key


def create_share(db, settings, user_id, export_id, body, key):
    # Public token is hashed in shares and authenticated-encrypted in the 24h replay cache.
    def create():
        export = owned(db, Export, export_id, user_id)
        db.refresh(export, with_for_update=True)
        file = available_file(db, export)
        active = db.get(ExportShare, export.active_share_id) if export.active_share_id else None
        if active and not active.revoked_at and active.expires_at > now():
            raise BusinessError(
                "SHARE_ALREADY_EXISTS", "已有活动分享，请使用已保存结果或先撤销", 409
            )
        token = secrets.token_urlsafe(32)
        row = ExportShare(
            export_id=export.id,
            token_hash=token_hash(token),
            expires_at=min(file.expires_at, now() + timedelta(days=body.expires_in_days)),
        )
        db.add(row)
        db.flush()
        export.active_share_id = row.id
        db.flush()
        cipher = AES.new(replay_cipher(settings), AES.MODE_GCM, nonce=secrets.token_bytes(12))
        encrypted, tag = cipher.encrypt_and_digest(token.encode())
        return {
            **share_out(row),
            "sealed_token": base64.b64encode(cipher.nonce + tag + encrypted).decode(),
        }

    value = idempotency.execute(
        db, user_id, "share.create:" + export_id, key, body.model_dump(), create
    )
    try:
        raw = base64.b64decode(value["sealed_token"])
        cipher = AES.new(replay_cipher(settings), AES.MODE_GCM, nonce=raw[:12])
        token = cipher.decrypt_and_verify(raw[28:], raw[12:28]).decode()
    except (ValueError, KeyError, UnicodeDecodeError) as exc:
        raise BusinessError(
            "SHARE_REPLAY_UNAVAILABLE", "分享创建结果无法恢复，请查询分享状态", 409
        ) from exc
    return {
        **{k: v for k, v in value.items() if k != "sealed_token"},
        "token": token,
        "share_path": "/pages/share/index?token=" + token,
    }


def revoke_share(db, user_id, export_id):
    export = owned(db, Export, export_id, user_id)
    db.refresh(export, with_for_update=True)
    row = db.get(ExportShare, export.active_share_id) if export.active_share_id else None
    if row and row.revoked_at is None:
        row.revoked_at = now()
        db.flush()
    return {"revoked": True}


def public_share(db, token):
    if not 20 <= len(token) <= 128:
        raise BusinessError("SHARE_UNAVAILABLE", "分享不可用", 404)
    share = db.scalar(select(ExportShare).where(ExportShare.token_hash == token_hash(token)))
    if share is None or share.revoked_at:
        raise BusinessError("SHARE_UNAVAILABLE", "分享不可用", 404)
    if share.expires_at <= now():
        raise BusinessError("SHARE_EXPIRED", "分享已过期", 410)
    export = db.get(Export, share.export_id)
    try:
        file = available_file(db, export)
    except BusinessError as exc:
        raise BusinessError("SHARE_UNAVAILABLE", "分享不可用", 404) from exc
    return share, export, file
