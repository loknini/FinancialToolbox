from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy import literal, select, union_all

from app.api.dependencies import DB, Current, ok
from app.db.models import Export, InvoiceBatch, TaxCalculation
from app.schemas.common import Envelope, Page
from app.schemas.content import ToolOut
from app.services import content, exports, invoices, tax
from app.services.pagination import date_range, paginate

router = APIRouter(tags=["使用记录"])


@router.get("/records", response_model=Envelope[Page[dict]])
def records(
    request: Request,
    db: DB,
    principal: Current,
    type: Literal["invoice_batch", "tax_calculation", "export"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    start, end = date_range(date_from, date_to)
    catalog = union_all(
        select(
            TaxCalculation.id.label("resource_id"),
            (literal("tax_calculation:") + TaxCalculation.id).label("id"),
            literal("tax_calculation").label("type"),
            TaxCalculation.created_at.label("created_at"),
        ).where(TaxCalculation.user_id == principal.user.id),
        select(
            InvoiceBatch.id.label("resource_id"),
            (literal("invoice_batch:") + InvoiceBatch.id).label("id"),
            literal("invoice_batch").label("type"),
            InvoiceBatch.created_at.label("created_at"),
        ).where(InvoiceBatch.user_id == principal.user.id),
        select(
            Export.id.label("resource_id"),
            (literal("export:") + Export.id).label("id"),
            literal("export").label("type"),
            Export.created_at.label("created_at"),
        ).where(Export.user_id == principal.user.id),
    ).subquery()
    query = select(catalog)
    if type:
        query = query.where(catalog.c.type == type)
    if start:
        query = query.where(catalog.c.created_at >= start)
    if end:
        query = query.where(catalog.c.created_at < end)

    def record(row):
        if row.type == "tax_calculation":
            value = tax.serialize(db.get(TaxCalculation, row.resource_id))
            title, status = "拆税计算", "succeeded"
        elif row.type == "export":
            value = exports.export_out(db, db.get(Export, row.resource_id))
            title, status = "导出文件", value["status"]
        else:
            value = invoices.batch_out(db, db.get(InvoiceBatch, row.resource_id))
            title, status = "发票识别", value["status"]
        return {
            "id": row.id,
            "type": row.type,
            "resource_id": row.resource_id,
            "title": title,
            "status": status,
            "summary": value,
            "created_at": value["created_at"],
        }

    return ok(
        request,
        paginate(
            db,
            request.app.state.settings,
            query,
            catalog.c,
            {
                "operation": "records",
                "user_id": principal.user.id,
                "type": type,
                "date_from": date_from,
                "date_to": date_to,
            },
            cursor,
            limit,
            record,
            scalar_rows=False,
        ),
    )


@router.get("/users/me/tools", response_model=Envelope[Page[ToolOut]])
def my_tools(
    request: Request,
    db: DB,
    principal: Current,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
):
    from app.api.v1.content import static_page

    used = db.scalar(
        select(TaxCalculation.id).where(TaxCalculation.user_id == principal.user.id).limit(1)
    )
    invoice_used = db.scalar(
        select(InvoiceBatch.id).where(InvoiceBatch.user_id == principal.user.id).limit(1)
    )
    export_used = db.scalar(select(Export.id).where(Export.user_id == principal.user.id).limit(1))
    codes = (
        (["tax"] if used else [])
        + (["invoice"] if invoice_used else [])
        + (["export"] if export_used else [])
    )
    items = [
        {**t, "enabled": request.app.state.ocr.enabled if t["code"] == "invoice" else t["enabled"]}
        for t in content.TOOLS
        if t["code"] in codes
    ]
    return ok(
        request,
        static_page(
            items,
            request.app.state.settings,
            "user.tools",
            {"user_id": principal.user.id},
            limit,
            cursor,
        ),
    )
