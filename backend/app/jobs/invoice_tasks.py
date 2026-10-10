from copy import deepcopy

from pypdf import PdfReader
from sqlalchemy import select

from app.core.errors import BusinessError
from app.db.models import BatchFile, File, Invoice, InvoiceBatch, InvoiceUnit, Job, OcrResponse
from app.integrations.ocr import OcrInput
from app.integrations.storage import file_hash
from app.services.invoices import assign_data, refresh_batch

TYPES = {"discover_file", "expand_file", "recognize_unit"}


def batch_for_job(db, job, lock=False):
    if job.type == "recognize_unit":
        resource = db.get(InvoiceUnit, job.resource_id)
    else:
        resource = db.get(BatchFile, job.resource_id)
    if resource is None:
        raise BusinessError("RESOURCE_NOT_FOUND", "处理单元不存在", 404)
    query = select(InvoiceBatch).where(InvoiceBatch.id == resource.batch_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return resource, db.scalar(query)


def mark_claim(db, job):
    resource, batch = batch_for_job(db, job, lock=True)
    if not batch.cancel_requested:
        batch.status = "processing"
        if job.type == "recognize_unit":
            resource.status = "processing"
            resource.attempt += 1
    db.flush()


def perform(db, job, storage, ocr):
    resource, batch = batch_for_job(db, job)
    if batch.cancel_requested:
        return {"cancelled": True}
    if job.type == "expand_file":
        return {"expand": True}
    if job.type == "discover_file" and resource.total_pages is not None:
        return {"total_pages": resource.total_pages}
    file = db.get(File, resource.file_id)
    if file.status != "ready":
        raise BusinessError("FILE_NOT_READY", "原始文件不可处理", 409)
    with storage.materialize(file) as path:
        if file.sha256 and file_hash(path) != file.sha256:
            raise BusinessError("CORRUPTED_FILE", "原始文件内容已变化")
        if job.type == "discover_file":
            pages = len(PdfReader(path).pages) if file.content_type == "application/pdf" else 1
            return {"total_pages": pages}
        response = ocr.recognize(
            OcrInput(
                str(path),
                resource.page_from,
                resource.page_to,
                job.dedupe_key,
                content_type=file.content_type,
            )
        )
        return {"ocr": response}


def apply(db, job, result, error, retry):
    resource, batch = batch_for_job(db, job, lock=True)
    if batch.cancel_requested or (result and result.get("cancelled")):
        if job.type == "recognize_unit":
            resource.status = "cancelled"
            resource.error = None
        else:
            resource.discovery_complete = True
        refresh_batch(db, batch.id)
        return
    if error:
        if job.type == "recognize_unit":
            resource.status = "queued" if retry else "failed"
            resource.error = error
        elif not retry:
            resource.discovery_error = error
            resource.discovery_complete = True
        refresh_batch(db, batch.id)
        return
    if job.type == "discover_file":
        resource.total_pages = result["total_pages"]
        resource.discovery_error = None
        db.add(
            Job(
                type="expand_file",
                resource_id=resource.id,
                dedupe_key=f"expand:{resource.id}:{resource.generation}:{resource.discovered_pages}",
            )
        )
    elif job.type == "expand_file":
        end = min(resource.total_pages, resource.discovered_pages + 100)
        # Page discovery is chunked so huge PDFs do not require one giant insert transaction.
        for page in range(resource.discovered_pages + 1, end + 1):
            unit = InvoiceUnit(
                batch_id=batch.id,
                batch_file_id=resource.id,
                file_id=resource.file_id,
                page_from=page,
                page_to=page,
            )
            db.add(unit)
            db.flush()
            db.add(Job(type="recognize_unit", resource_id=unit.id, dedupe_key=f"ocr:{unit.id}:0"))
        resource.discovered_pages = end
        resource.discovery_complete = end == resource.total_pages
        if not resource.discovery_complete:
            db.add(
                Job(
                    type="expand_file",
                    resource_id=resource.id,
                    dedupe_key=f"expand:{resource.id}:{resource.generation}:{end}",
                )
            )
    else:
        response = result["ocr"]
        old = db.scalar(
            select(OcrResponse).where(
                OcrResponse.unit_id == resource.id, OcrResponse.generation == resource.generation
            )
        )
        if old is None:
            db.add(
                OcrResponse(
                    unit_id=resource.id,
                    generation=resource.generation,
                    provider_request_id=response.provider_request_id,
                    raw_response=response.raw_response,
                )
            )
        for index, data in enumerate(response.invoices):
            key = data.get("result_key") or str(index)
            existing = db.scalar(
                select(Invoice).where(Invoice.unit_id == resource.id, Invoice.result_key == key)
            )
            if existing is not None:
                continue  # Preserve confirmed and manually corrected results across retries.
            data = deepcopy(data)
            data["source"] = {
                "file_id": resource.file_id,
                "page_from": resource.page_from,
                "page_to": resource.page_to,
                "region": data.pop("region", None),
            }
            invoice = Invoice(
                user_id=batch.user_id,
                batch_id=batch.id,
                unit_id=resource.id,
                result_key=key,
                original_data=deepcopy(data),
            )
            assign_data(invoice, data)
            db.add(invoice)
        resource.status = "succeeded"
        resource.error = None
        if response.item_errors:
            resource.status = "failed"
            resource.error = {
                "code": "PARTIAL_OCR_FAILURE",
                "message": "部分票据识别失败",
                "retryable": False,
                "items": response.item_errors,
            }
        elif not response.invoices:
            resource.status = "failed"
            resource.error = {
                "code": "NO_INVOICE_FOUND",
                "message": "本页未识别出票据",
                "retryable": False,
            }
    refresh_batch(db, batch.id)
