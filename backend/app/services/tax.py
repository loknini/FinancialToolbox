from decimal import ROUND_HALF_UP, Decimal, localcontext

from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.core.security import timestamp
from app.db.models import TaxCalculation
from app.schemas.tax import TaxInput

CONFIG_VERSION = "tax-config-v1"
RULE_VERSION = "gross-split-v1"
RATES = ["0.01", "0.03", "0.06", "0.09", "0.13"]


def config() -> dict:
    return {
        "version": CONFIG_VERSION,
        "rule_version": RULE_VERSION,
        "input_mode": "gross",
        "amount_scale": 2,
        "rounding": "half_up",
        "rates": [{"value": value, "label": f"{int(Decimal(value) * 100)}%"} for value in RATES],
        "invoice_types": [{"code": "general", "label": "通用拆税", "allowed_rates": RATES}],
    }


def serialize(row: TaxCalculation) -> dict:
    return {
        "id": row.id,
        "gross_amount": f"{row.gross_amount:.2f}",
        "net_amount": f"{row.net_amount:.2f}",
        "tax_amount": f"{row.tax_amount:.2f}",
        "tax_rate": format(row.tax_rate.normalize(), "f"),
        "invoice_type_code": row.invoice_type_code,
        "config_version": row.config_version,
        "rule_version": row.rule_version,
        "source_invoice_id": row.source_invoice_id,
        "created_at": timestamp(row.created_at),
    }


def calculate(db: Session, user_id: str, data: TaxInput) -> dict:
    if data.config_version != CONFIG_VERSION:
        raise BusinessError("CONFIG_CHANGED", "计算配置已变更，请刷新", 409)
    if data.invoice_type_code != "general" or Decimal(data.tax_rate) not in map(Decimal, RATES):
        raise BusinessError("VALIDATION_ERROR", "不支持所选发票类型或税率")
    gross = Decimal(data.gross_amount)
    if gross <= 0:
        raise BusinessError("VALIDATION_ERROR", "含税金额必须大于零")
    if data.source_invoice_id is not None:
        from app.db.models import Invoice
        from app.services.files import owned

        owned(db, Invoice, data.source_invoice_id, user_id)
    with localcontext() as context:
        context.prec = 40
        rate = Decimal(data.tax_rate)
        gross = gross.quantize(Decimal("0.01"))
        net = (gross / (Decimal(1) + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        row = TaxCalculation(
            user_id=user_id,
            source_invoice_id=data.source_invoice_id,
            gross_amount=gross,
            net_amount=net,
            tax_amount=gross - net,
            tax_rate=rate,
            invoice_type_code="general",
            config_version=CONFIG_VERSION,
            rule_version=RULE_VERSION,
        )
    db.add(row)
    db.flush()
    return serialize(row)
