from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy import and_, or_, select

from app.api.dependencies import DB, Current, Key, ok
from app.core.errors import BusinessError
from app.core.security import decode, sign
from app.db.models import BatchFile, Invoice, InvoiceBatch, InvoiceUnit
from app.schemas.common import EmptyInput, Envelope, Page
from app.schemas.invoices import (
    BatchCreate,
    BatchOut,
    BatchRetry,
    ConfirmInput,
    InvoiceDetail,
    InvoicePatch,
    InvoiceSummary,
    UnitOut,
)
from app.services import idempotency, invoices
from app.services.files import owned
from app.services.pagination import date_range, paginate

router = APIRouter(tags=["发票"])


@router.post("/invoice-batches", status_code=202, response_model=Envelope[BatchOut])
def create(body: BatchCreate, request: Request, db: DB, principal: Current, key: Key):
    return ok(
        request,
        idempotency.execute(
            db,
            principal.user.id,
            "batch.create",
            key,
            body.model_dump(),
            lambda: invoices.create_batch(db, principal.user.id, body, request.app.state.ocr),
        ),
    )


@router.get("/invoice-batches", response_model=Envelope[Page[BatchOut]])
def batches(
    request: Request,
    db: DB,
    principal: Current,
    status: Literal[
        "queued", "processing", "succeeded", "partial_failed", "failed", "cancelling", "cancelled"
    ]
    | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    start, end = date_range(date_from, date_to)
    query = select(InvoiceBatch).where(InvoiceBatch.user_id == principal.user.id)
    if status:
        query = query.where(InvoiceBatch.status == status)
    if start:
        query = query.where(InvoiceBatch.created_at >= start)
    if end:
        query = query.where(InvoiceBatch.created_at < end)
    return ok(
        request,
        paginate(
            db,
            request.app.state.settings,
            query,
            InvoiceBatch,
            {
                "operation": "batches",
                "user_id": principal.user.id,
                "status": status,
                "date_from": date_from,
                "date_to": date_to,
            },
            cursor,
            limit,
            lambda row: invoices.batch_out(db, row),
        ),
    )


@router.get("/invoice-batches/{resource_id}", response_model=Envelope[BatchOut])
def batch(resource_id: str, request: Request, db: DB, principal: Current):
    return ok(
        request, invoices.batch_out(db, owned(db, InvoiceBatch, resource_id, principal.user.id))
    )


@router.get("/invoice-batches/{resource_id}/items", response_model=Envelope[Page[UnitOut]])
def items(
    resource_id: str,
    request: Request,
    db: DB,
    principal: Current,
    status: Literal["queued", "processing", "succeeded", "failed", "cancelled"] | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    owned(db, InvoiceBatch, resource_id, principal.user.id)
    query = (
        select(InvoiceUnit, BatchFile.position)
        .join(BatchFile, InvoiceUnit.batch_file_id == BatchFile.id)
        .where(InvoiceUnit.batch_id == resource_id)
    )
    filters = {"user_id": principal.user.id, "batch_id": resource_id, "status": status}
    if status:
        query = query.where(InvoiceUnit.status == status)
    if cursor:
        try:
            claims = decode(request.app.state.settings, cursor, "unit_cursor")
            if claims["filters"] != filters:
                raise ValueError()
            position, page = int(claims["position"]), int(claims["page"])
            query = query.where(
                or_(
                    BatchFile.position > position,
                    and_(BatchFile.position == position, InvoiceUnit.page_from > page),
                )
            )
        except (BusinessError, ValueError, KeyError, TypeError) as exc:
            raise BusinessError("INVALID_CURSOR", "分页游标无效或筛选已变更", 400) from exc
    rows = list(
        db.execute(query.order_by(BatchFile.position, InvoiceUnit.page_from).limit(limit + 1))
    )
    more = len(rows) > limit
    rows = rows[:limit]
    token = (
        sign(
            request.app.state.settings,
            "unit_cursor",
            {"filters": filters, "position": rows[-1][1], "page": rows[-1][0].page_from},
            86400,
        )
        if more
        else None
    )
    return ok(
        request,
        {
            "items": [invoices.unit_out(db, row) for row, _ in rows],
            "has_more": more,
            "next_cursor": token,
        },
    )


@router.post(
    "/invoice-batches/{resource_id}/retry", status_code=202, response_model=Envelope[BatchOut]
)
def retry(
    resource_id: str, body: BatchRetry, request: Request, db: DB, principal: Current, key: Key
):
    return ok(
        request,
        idempotency.execute(
            db,
            principal.user.id,
            "batch.retry:" + resource_id,
            key,
            body.model_dump(),
            lambda: invoices.retry_batch(db, principal.user.id, resource_id, request.app.state.ocr),
        ),
    )


@router.post("/invoice-batches/{resource_id}/cancel", response_model=Envelope[BatchOut])
def cancel(resource_id: str, body: EmptyInput, request: Request, db: DB, principal: Current):
    return ok(request, invoices.cancel_batch(db, principal.user.id, resource_id))


@router.get("/invoices", response_model=Envelope[Page[InvoiceSummary]])
def list_invoices(
    request: Request,
    db: DB,
    principal: Current,
    batch_id: str | None = None,
    type_code: str | None = None,
    review_status: Literal["pending_review", "confirmed"] | None = None,
    keyword: Annotated[str, Query(max_length=256)] = "",
    date_from: date | None = None,
    date_to: date | None = None,
    invoice_date_from: date | None = None,
    invoice_date_to: date | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    start, end = date_range(date_from, date_to)
    if invoice_date_from and invoice_date_to and invoice_date_from > invoice_date_to:
        raise BusinessError("VALIDATION_ERROR", "开始日期不能晚于结束日期")
    query = select(Invoice).where(Invoice.user_id == principal.user.id)
    if batch_id:
        owned(db, InvoiceBatch, batch_id, principal.user.id)
        query = query.where(Invoice.batch_id == batch_id)
    if type_code:
        query = query.where(Invoice.type_code == type_code)
    if review_status:
        query = query.where(Invoice.review_status == review_status)
    if keyword:
        query = query.where(
            or_(
                Invoice.invoice_number.contains(keyword, autoescape=True),
                Invoice.seller_name.contains(keyword, autoescape=True),
            )
        )
    if start:
        query = query.where(Invoice.created_at >= start)
    if end:
        query = query.where(Invoice.created_at < end)
    if invoice_date_from:
        query = query.where(Invoice.invoice_date >= invoice_date_from)
    if invoice_date_to:
        query = query.where(Invoice.invoice_date <= invoice_date_to)
    filters = {
        "operation": "invoices",
        "user_id": principal.user.id,
        "batch_id": batch_id,
        "type_code": type_code,
        "review_status": review_status,
        "keyword": keyword,
        "date_from": date_from,
        "date_to": date_to,
        "invoice_date_from": invoice_date_from,
        "invoice_date_to": invoice_date_to,
    }
    return ok(
        request,
        paginate(
            db,
            request.app.state.settings,
            query,
            Invoice,
            filters,
            cursor,
            limit,
            invoices.invoice_out,
        ),
    )


@router.get("/invoices/{resource_id}", response_model=Envelope[InvoiceDetail])
def detail(resource_id: str, request: Request, db: DB, principal: Current):
    return ok(
        request, invoices.invoice_out(owned(db, Invoice, resource_id, principal.user.id), True)
    )


@router.patch("/invoices/{resource_id}", response_model=Envelope[InvoiceDetail])
def patch(resource_id: str, body: InvoicePatch, request: Request, db: DB, principal: Current):
    return ok(request, invoices.patch_invoice(db, principal.user.id, resource_id, body))


@router.post("/invoices/{resource_id}/confirm", response_model=Envelope[InvoiceDetail])
def confirm(resource_id: str, body: ConfirmInput, request: Request, db: DB, principal: Current):
    return ok(request, invoices.confirm_invoice(db, principal.user.id, resource_id, body))
