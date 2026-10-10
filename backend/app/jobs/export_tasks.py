from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import select

from app.core.security import now
from app.db.models import Export, ExportFile
from app.integrations.storage import file_hash
from app.services import export_render


def locked(db, job):
    return db.scalar(
        select(Export)
        .where(Export.id == job.resource_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def mark_claim(db, job):
    row = locked(db, job)
    if not row.cancel_requested:
        row.status = "processing"
    db.flush()


def perform(db, job, settings, storage):
    row = db.get(Export, job.resource_id)
    if row.cancel_requested:
        return {"cancelled": True}
    extension = "xlsx" if row.format == "excel" else "pdf"
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if row.format == "excel"
        else "application/pdf"
    )
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="export-", dir=settings.storage_root) as folder:
        path = Path(folder) / ("report." + extension)
        if row.format == "excel":
            export_render.excel(db, row, path)
        else:
            export_render.pdf(db, row, path, settings)
        key = f"exports/{row.id}/{uuid4()}.{extension}"
        storage.publish(key, path, content_type)
        return {
            "object_key": key,
            "size_bytes": path.stat().st_size,
            "sha256": file_hash(path),
            "content_type": content_type,
            "filename": ("发票汇总" if row.source_type == "invoices" else "拆税汇总")
            + "."
            + extension,
            "storage_backend": storage.backend,
        }


def apply(db, job, result, error, retry, settings):
    row = locked(db, job)
    if row.cancel_requested or (result and result.get("cancelled")):
        row.status = "cancelled"
        row.error = None
    elif error:
        row.status = "queued" if retry else "failed"
        row.error = error
    else:
        row.status = "succeeded"
        row.error = None
        db.add(
            ExportFile(
                export_id=row.id,
                **result,
                expires_at=now() + timedelta(days=settings.export_retention_days),
            )
        )
    db.flush()
