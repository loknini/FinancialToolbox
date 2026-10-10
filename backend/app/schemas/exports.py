from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import Input


class IdSelection(Input):
    mode: Literal["ids"]
    ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("资源不能重复")
        return self


class BatchSelection(Input):
    mode: Literal["batch"]
    batch_id: str


class FilterSelection(Input):
    mode: Literal["filter"]
    date_from: date | None = None
    date_to: date | None = None
    type_code: str | None = None
    invoice_date_from: date | None = None
    invoice_date_to: date | None = None

    @model_validator(mode="after")
    def dates(self):
        for start, end in [
            (self.date_from, self.date_to),
            (self.invoice_date_from, self.invoice_date_to),
        ]:
            if start and end and start > end:
                raise ValueError("开始日期不能晚于结束日期")
        return self


class ExportRequest(Input):
    format: Literal["excel", "pdf"]
    source_type: Literal["invoices", "tax_calculations"]
    selection: Annotated[
        IdSelection | BatchSelection | FilterSelection, Field(discriminator="mode")
    ]
    review_scope: Literal["confirmed", "all"] | None = None
    group_by: Literal["none", "seller", "month"] = "none"

    @model_validator(mode="after")
    def source_rules(self):
        if self.source_type == "invoices":
            if self.review_scope is None:
                self.review_scope = "confirmed"
        else:
            if (
                self.review_scope is not None
                or self.group_by == "seller"
                or self.selection.mode == "batch"
            ):
                raise ValueError("拆税导出参数不匹配")
            if self.selection.mode == "filter" and self.selection.model_fields_set & {
                "type_code",
                "invoice_date_from",
                "invoice_date_to",
            }:
                raise ValueError("拆税不接受票面筛选")
        return self


class ExportOut(BaseModel):
    id: str
    format: str
    source_type: str
    status: str
    record_count: int
    pending_review_count: int
    snapshot_at: str
    progress: dict[str, int | None]
    file: dict | None
    error: dict | None
    can_cancel: bool
    created_at: str


class ShareRequest(Input):
    expires_in_days: int = Field(default=7, ge=1, le=30)


class ShareOut(BaseModel):
    id: str
    export_id: str
    status: str
    expires_at: str
    created_at: str


class ShareCreated(ShareOut):
    token: str
    share_path: str


class ShareView(BaseModel):
    filename: str
    format: str
    size_bytes: str
    expires_at: str
    description: str
