from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import Input

Money = Annotated[str, Field(pattern=r"^-?(0|[1-9]\d{0,21})(\.\d{1,2})?$")]
DecimalText = Annotated[str, Field(pattern=r"^-?(0|[1-9]\d{0,21})(\.\d{1,6})?$")]


class BatchCreate(Input):
    file_ids: list[str] = Field(min_length=1)
    source: Literal["page", "chat"] = "page"

    @model_validator(mode="after")
    def unique_files(self):
        if len(set(self.file_ids)) != len(self.file_ids):
            raise ValueError("文件不能重复")
        return self


class BatchRetry(Input):
    scope: Literal["failed"] = "failed"


class PartyPatch(Input):
    name: str | None = Field(default=None, max_length=1024)
    tax_id: str | None = Field(default=None, max_length=128)


class InvoiceLine(Input):
    line_number: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=2048)
    quantity: DecimalText | None = None
    unit_price: DecimalText | None = None
    net_amount: Money | None = None
    tax_rate: Annotated[str, Field(pattern=r"^(0|1)(\.\d{1,6})?$")] | None = None
    tax_treatment: Literal["taxable", "exempt", "non_taxable", "zero_rated", "unknown"] = "unknown"
    tax_amount: Money | None = None

    @model_validator(mode="after")
    def valid_rate(self):
        if self.tax_rate is not None and not Decimal("0") <= Decimal(self.tax_rate) <= Decimal("1"):
            raise ValueError("税率必须在 0 和 1 之间")
        return self


class InvoicePatch(Input):
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=1024)
    invoice_number: str | None = Field(default=None, max_length=128)
    invoice_code: str | None = Field(default=None, max_length=128)
    invoice_date: date | None = None
    buyer: PartyPatch | None = None
    seller: PartyPatch | None = None
    gross_amount: Money | None = None
    net_amount: Money | None = None
    tax_amount: Money | None = None
    lines: list[InvoiceLine] | None = None

    @model_validator(mode="after")
    def require_change(self):
        changes = self.model_fields_set - {"expected_version", "reason", "resource_id"}
        if not changes:
            raise ValueError("必须提供修改字段")
        if (
            ("buyer" in changes and self.buyer is None)
            or ("seller" in changes and self.seller is None)
            or ("lines" in changes and self.lines is None)
        ):
            raise ValueError("buyer、seller 和 lines 不允许设为 null")
        if self.lines is not None and len({line.line_number for line in self.lines}) != len(
            self.lines
        ):
            raise ValueError("明细行号不能重复")
        return self


class ConfirmInput(Input):
    expected_version: int = Field(ge=1)


class BatchOut(BaseModel):
    id: str
    status: str
    source: str
    file_count: int
    discovery_complete: bool
    progress: dict[str, int | None]
    invoice_count: int
    pending_review_count: int
    can_retry: bool
    can_cancel: bool
    error: dict | None
    created_at: str
    updated_at: str


class UnitOut(BaseModel):
    id: str
    file_id: str
    page_from: int
    page_to: int
    status: str
    attempt: int
    invoice_count: int
    error: dict | None


class InvoiceSummary(BaseModel):
    id: str
    batch_id: str
    type_code: str
    type_label: str
    invoice_number: str | None
    invoice_date: str | None
    seller_name: str | None
    gross_amount: str | None
    net_amount: str | None
    tax_amount: str | None
    currency: str
    review_status: str
    version: int
    issues: list[dict]
    created_at: str


class InvoiceDetail(InvoiceSummary):
    invoice_code: str | None
    buyer: dict
    seller: dict
    lines: list[dict]
    source: dict
    extended_fields: dict
