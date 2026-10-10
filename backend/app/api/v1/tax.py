from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.dependencies import DB, Current, Key, ok
from app.db.models import TaxCalculation
from app.schemas.common import Envelope, Page
from app.schemas.tax import TaxConfig, TaxInput, TaxOut
from app.services import idempotency, tax
from app.services.files import owned
from app.services.pagination import date_range, paginate

router = APIRouter(prefix="/tax-calculations", tags=["拆税"])


@router.get("/config", response_model=Envelope[TaxConfig])
def config(request: Request):
    return ok(request, tax.config())


@router.post("", status_code=201, response_model=Envelope[TaxOut])
def calculate(body: TaxInput, request: Request, db: DB, principal: Current, key: Key):
    result = idempotency.execute(
        db,
        principal.user.id,
        "tax.create",
        key,
        body.model_dump(),
        lambda: tax.calculate(db, principal.user.id, body),
    )
    return ok(request, result)


@router.get("", response_model=Envelope[Page[TaxOut]])
def list_calculations(
    request: Request,
    db: DB,
    principal: Current,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    query = select(TaxCalculation).where(TaxCalculation.user_id == principal.user.id)
    start, end = date_range(date_from, date_to)
    if start:
        query = query.where(TaxCalculation.created_at >= start)
    if end:
        query = query.where(TaxCalculation.created_at < end)
    filters = {
        "operation": "tax.list",
        "user_id": principal.user.id,
        "date_from": date_from,
        "date_to": date_to,
    }
    return ok(
        request,
        paginate(
            db,
            request.app.state.settings,
            query,
            TaxCalculation,
            filters,
            cursor,
            limit,
            tax.serialize,
        ),
    )


@router.get("/{resource_id}", response_model=Envelope[TaxOut])
def detail(resource_id: str, request: Request, db: DB, principal: Current):
    return ok(request, tax.serialize(owned(db, TaxCalculation, resource_id, principal.user.id)))
