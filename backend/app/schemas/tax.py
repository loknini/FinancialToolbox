from pydantic import BaseModel, Field

from app.schemas.common import Input


class TaxInput(Input):
    gross_amount: str = Field(pattern=r"^(0|[1-9]\d{0,21})(\.\d{1,2})?$")
    tax_rate: str = Field(pattern=r"^0\.\d{1,6}$")
    invoice_type_code: str
    config_version: str
    source_invoice_id: str | None = None


class Rate(BaseModel):
    value: str
    label: str


class TaxType(BaseModel):
    code: str
    label: str
    allowed_rates: list[str]


class TaxConfig(BaseModel):
    version: str
    rule_version: str
    input_mode: str = "gross"
    amount_scale: int = 2
    rounding: str = "half_up"
    rates: list[Rate]
    invoice_types: list[TaxType]


class TaxOut(BaseModel):
    id: str
    gross_amount: str
    net_amount: str
    tax_amount: str
    tax_rate: str
    invoice_type_code: str
    config_version: str
    rule_version: str
    source_invoice_id: str | None
    created_at: str
