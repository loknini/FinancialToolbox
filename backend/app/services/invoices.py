from copy import deepcopy
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, update

from app.core.errors import BusinessError
from app.core.security import now, timestamp
from app.db.models import BatchFile, File, Invoice, InvoiceBatch, InvoiceRevision, InvoiceUnit, Job
from app.services.auth import check_version
from app.services.files import owned

TERMINAL = {"succeeded", "partial_failed", "failed", "cancelled"}


def metrics(db, batch):
    files = list(db.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)))
    grouped = dict(
        db.execute(
            select(InvoiceUnit.status, func.count())
            .where(InvoiceUnit.batch_id == batch.id)
            .group_by(InvoiceUnit.status)
        ).all()
    )
    total = sum(grouped.values())
    completed = sum(grouped.get(s, 0) for s in ("succeeded", "failed", "cancelled"))
    discovered = all(f.discovery_complete for f in files)
    invoice_count = db.scalar(
        select(func.count()).select_from(Invoice).where(Invoice.batch_id == batch.id)
    )
    pending = db.scalar(
        select(func.count())
        .select_from(Invoice)
        .where(Invoice.batch_id == batch.id, Invoice.review_status == "pending_review")
    )
    error = next((f.discovery_error for f in files if f.discovery_error), None)
    if error is None:
        error = db.scalar(
            select(InvoiceUnit.error)
            .where(InvoiceUnit.batch_id == batch.id, InvoiceUnit.status == "failed")
            .order_by(InvoiceUnit.id)
            .limit(1)
        )
    return files, grouped, total, completed, discovered, invoice_count, pending, error


def batch_out(db, batch):
    fs, counts, total, completed, discovered, invoice_count, pending, error = metrics(db, batch)
    return {
        "id": batch.id,
        "status": batch.status,
        "source": batch.source,
        "file_count": len(fs),
        "discovery_complete": discovered,
        "progress": {
            "total_units": total if discovered else None,
            "completed_units": completed,
            "succeeded_units": counts.get("succeeded", 0),
            "failed_units": counts.get("failed", 0),
            "cancelled_units": counts.get("cancelled", 0),
            "percent": completed * 100 // total if discovered and total else None,
        },
        "invoice_count": invoice_count,
        "pending_review_count": pending,
        "can_retry": batch.status in {"failed", "partial_failed"},
        "can_cancel": batch.status not in TERMINAL,
        "error": error,
        "created_at": timestamp(batch.created_at),
        "updated_at": timestamp(batch.updated_at),
    }


def related_jobs(batch_id):
    return Job.resource_id.in_(
        select(BatchFile.id)
        .where(BatchFile.batch_id == batch_id)
        .union(select(InvoiceUnit.id).where(InvoiceUnit.batch_id == batch_id))
    )


def refresh_batch(db, batch_id):
    db.flush()
    batch = db.scalar(select(InvoiceBatch).where(InvoiceBatch.id == batch_id).with_for_update())
    fs, counts, total, completed, discovered, invoice_count, pending, error = metrics(db, batch)
    if batch.cancel_requested:
        active = db.scalar(
            select(func.count())
            .select_from(Job)
            .where(related_jobs(batch_id), Job.status == "processing")
        )
        batch.status = "cancelling" if active else "cancelled"
    elif not discovered or completed < total:
        batch.status = "processing"
    elif error or counts.get("failed", 0):
        batch.status = "partial_failed" if invoice_count else "failed"
    else:
        batch.status = "succeeded"
    batch.updated_at = now()
    db.flush()
    return batch


def create_batch(db, user_id, body, ocr):
    if not ocr.enabled:
        raise BusinessError("CAPABILITY_DISABLED", "票据识别尚未配置", 403)
    batch = InvoiceBatch(user_id=user_id, source=body.source)
    db.add(batch)
    db.flush()
    for position, file_id in enumerate(body.file_ids):
        file = owned(db, File, file_id, user_id)
        if file.status != "ready" or (file.expires_at and file.expires_at <= now()):
            raise BusinessError("FILE_NOT_READY", "文件不可识别", 409)
        if file.purpose not in ("invoice", "chat_attachment"):
            raise BusinessError("VALIDATION_ERROR", "文件用途不支持发票识别")
        link = BatchFile(batch_id=batch.id, file_id=file.id, position=position)
        db.add(link)
        db.flush()
        db.add(
            Job(type="discover_file", resource_id=link.id, dedupe_key="discover:" + link.id + ":0")
        )
    db.flush()
    return batch_out(db, batch)


def retry_batch(db, user_id, batch_id, ocr):
    if not ocr.enabled:
        raise BusinessError("CAPABILITY_DISABLED", "票据识别尚未配置", 403)
    batch = owned(db, InvoiceBatch, batch_id, user_id)
    db.refresh(batch, with_for_update=True)
    if batch.status not in ("failed", "partial_failed"):
        raise BusinessError("STATE_CONFLICT", "批次尚未结束或不可重试", 409)
    units = list(
        db.scalars(
            select(InvoiceUnit).where(
                InvoiceUnit.batch_id == batch.id, InvoiceUnit.status == "failed"
            )
        )
    )
    links = [
        link
        for link in db.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id))
        if link.discovery_error is not None
    ]
    if not units and not links:
        raise BusinessError("NO_FAILED_ITEMS", "没有可重试的失败项目")
    for unit in units:
        unit.generation += 1
        unit.status, unit.error = "queued", None
        db.add(
            Job(
                type="recognize_unit",
                resource_id=unit.id,
                dedupe_key=f"ocr:{unit.id}:{unit.generation}",
            )
        )
    for link in links:
        link.generation += 1
        link.discovery_complete = False
        link.discovery_error = None
        db.add(
            Job(
                type="discover_file",
                resource_id=link.id,
                dedupe_key=f"discover:{link.id}:{link.generation}",
            )
        )
    batch.status = "queued"
    db.flush()
    return batch_out(db, batch)


def cancel_batch(db, user_id, batch_id):
    batch = owned(db, InvoiceBatch, batch_id, user_id)
    db.refresh(batch, with_for_update=True)
    if batch.status in TERMINAL:
        return batch_out(db, batch)
    batch.cancel_requested = True
    db.execute(
        update(Job).where(related_jobs(batch.id), Job.status == "queued").values(status="cancelled")
    )
    db.execute(
        update(InvoiceUnit)
        .where(InvoiceUnit.batch_id == batch.id, InvoiceUnit.status == "queued")
        .values(status="cancelled")
    )
    db.execute(
        update(BatchFile).where(BatchFile.batch_id == batch.id).values(discovery_complete=True)
    )
    batch = refresh_batch(db, batch.id)
    return batch_out(db, batch)


def unit_out(db, row):
    return {
        "id": row.id,
        "file_id": row.file_id,
        "page_from": row.page_from,
        "page_to": row.page_to,
        "status": row.status,
        "attempt": row.attempt,
        "invoice_count": db.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.unit_id == row.id)
        ),
        "error": row.error,
    }


def validate_data(data):
    issues = []
    if data.get("gross_amount") is None:
        issues.append(
            {
                "field": "gross_amount",
                "code": "MISSING_AMOUNT",
                "message": "请核对票面总金额",
                "severity": "error",
            }
        )
    if all(data.get(k) is not None for k in ("gross_amount", "net_amount", "tax_amount")):
        if Decimal(data["net_amount"]) + Decimal(data["tax_amount"]) != Decimal(
            data["gross_amount"]
        ):
            issues.append(
                {
                    "field": "gross_amount",
                    "code": "AMOUNT_MISMATCH",
                    "message": "不含税金额与税额之和不等于含税金额",
                    "severity": "error",
                }
            )
    if data.get("currency") == "UNKNOWN":
        issues.append(
            {
                "field": "currency",
                "code": "UNKNOWN_CURRENCY",
                "message": "票面币种需要核对，不能直接与人民币合计",
                "severity": "warning",
            }
        )
    if data.get("type_code") == "unknown":
        issues.append(
            {
                "field": "type_code",
                "code": "UNKNOWN_TYPE",
                "message": "票据类型需要核对",
                "severity": "warning",
            }
        )
    if not data.get("invoice_number"):
        issues.append(
            {
                "field": "invoice_number",
                "code": "MISSING_NUMBER",
                "message": "票据号码未识别",
                "severity": "warning",
            }
        )
    if not data.get("invoice_date"):
        issues.append(
            {
                "field": "invoice_date",
                "code": "MISSING_DATE",
                "message": "票面日期未识别",
                "severity": "warning",
            }
        )
    return issues


def assign_data(row, data):
    row.data = deepcopy(data)
    row.data["issues"] = validate_data(data)
    row.type_code = data["type_code"]
    row.invoice_number = data.get("invoice_number")
    row.invoice_date = (
        date.fromisoformat(data["invoice_date"]) if data.get("invoice_date") else None
    )
    row.seller_name = data.get("seller", {}).get("name")
    for field in ("gross_amount", "net_amount", "tax_amount"):
        setattr(row, field, Decimal(data[field]) if data.get(field) is not None else None)


def invoice_out(row, detail=False):
    data = row.data
    summary = {
        "id": row.id,
        "batch_id": row.batch_id,
        "type_code": row.type_code,
        "type_label": data.get("type_label", "其他票据"),
        "invoice_number": row.invoice_number,
        "invoice_date": row.invoice_date.isoformat() if row.invoice_date else None,
        "seller_name": row.seller_name,
        "gross_amount": format(row.gross_amount, ".2f") if row.gross_amount is not None else None,
        "net_amount": format(row.net_amount, ".2f") if row.net_amount is not None else None,
        "tax_amount": format(row.tax_amount, ".2f") if row.tax_amount is not None else None,
        "currency": data.get("currency", "CNY"),
        "review_status": row.review_status,
        "version": row.version,
        "issues": data.get("issues", []),
        "created_at": timestamp(row.created_at),
    }
    if detail:
        summary.update(
            {
                "invoice_code": data.get("invoice_code"),
                "buyer": data.get("buyer", {}),
                "seller": data.get("seller", {}),
                "lines": data.get("lines", []),
                "source": data["source"],
                "extended_fields": data.get("extended_fields", {}),
            }
        )
    return summary


def patch_invoice(db, user_id, invoice_id, body):
    row = owned(db, Invoice, invoice_id, user_id)
    check_version(row, body.expected_version)
    before = invoice_out(row, True)
    data = deepcopy(row.data)
    changes = body.model_dump(
        mode="json", exclude_unset=True, exclude={"expected_version", "reason"}
    )
    for key, value in changes.items():
        if key in ("buyer", "seller"):
            data.setdefault(key, {}).update(value)
        else:
            data[key] = value
    assign_data(row, data)
    row.review_status = "pending_review"
    row.updated_at = now()
    db.flush()
    after = invoice_out(row, True)
    db.add(
        InvoiceRevision(
            invoice_id=row.id,
            actor_id=user_id,
            before=before,
            after=after,
            reason=body.reason,
            operation="patch",
        )
    )
    db.flush()
    return after


def confirm_invoice(db, user_id, invoice_id, body):
    row = owned(db, Invoice, invoice_id, user_id)
    check_version(row, body.expected_version)
    if any(i.get("severity") == "error" for i in row.data.get("issues", [])):
        raise BusinessError("REVIEW_REQUIRED", "请先解决金额或字段核对问题")
    before = invoice_out(row, True)
    row.review_status = "confirmed"
    db.flush()
    after = invoice_out(row, True)
    if before != after:
        db.add(
            InvoiceRevision(
                invoice_id=row.id, actor_id=user_id, before=before, after=after, operation="confirm"
            )
        )
        db.flush()
    return after
