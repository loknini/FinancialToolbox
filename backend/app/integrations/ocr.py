import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Protocol

from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.core.errors import BusinessError

TYPES = {
    0: ("taxi_ticket", "出租车票"),
    1: ("quota_invoice", "定额发票"),
    2: ("train_ticket", "火车票"),
    3: ("vat_invoice", "增值税发票"),
    5: ("air_transport", "机票行程单"),
    8: ("machine_printed", "机打发票"),
    9: ("bus_ticket", "汽车票"),
    10: ("shipping_ticket", "轮船票"),
    11: ("vat_roll", "增值税卷票"),
    12: ("vehicle_invoice", "机动车票据"),
    13: ("toll_invoice", "通行费票据"),
    15: ("non_tax_bill", "非税票据"),
    16: ("digital_invoice", "数电发票"),
    17: ("medical_bill", "医疗票据"),
    18: ("tax_payment", "完税凭证"),
    19: ("customs_payment", "海关缴款"),
    20: ("bank_slip", "银行回单"),
    21: ("taxi_itinerary", "网约车行程"),
    22: ("customs_declaration", "报关单"),
    23: ("overseas_invoice", "海外票据"),
    24: ("shopping_receipt", "购物小票"),
    25: ("sales_inventory", "销货清单"),
    26: ("toll_summary", "通行费汇总"),
    -1: ("unknown", "其他票据"),
}


@dataclass(frozen=True)
class OcrInput:
    file_path: str
    page_from: int
    page_to: int
    request_key: str
    content_type: str = "application/pdf"


@dataclass(frozen=True)
class OcrResult:
    provider_request_id: str
    invoices: list[dict]
    raw_response: dict
    item_errors: list[dict] = field(default_factory=list)


class OcrProvider(Protocol):
    enabled: bool

    def recognize(self, request: OcrInput) -> OcrResult: ...


class DisabledOcrProvider:
    enabled = False

    def recognize(self, request: OcrInput) -> OcrResult:
        raise BusinessError("CAPABILITY_DISABLED", "尚未接入票据识别供应商", 403)


def money(value):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace("￥", "").replace("¥", "").strip())
        if (
            not number.is_finite()
            or abs(number) >= Decimal("1e22")
            or number != number.quantize(Decimal(".01"))
        ):
            return None
        return format(number.quantize(Decimal(".01")), "f")
    except (InvalidOperation, ValueError):
        return None


def decimal_text(value):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
        return format(number, "f") if number.is_finite() else None
    except InvalidOperation:
        return None


def parse_date(value):
    if not value:
        return None
    text = re.sub(r"[年月/.]", "-", str(value)).replace("日", "").strip()
    try:
        if re.fullmatch(r"\d{8}", text):
            text = text[:4] + "-" + text[4:6] + "-" + text[6:]
        parts = text.split("-")
        if len(parts) == 3:
            return date(*(int(v) for v in parts)).isoformat()
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def normalize_response(payload):
    invoices, errors = [], []
    for index, item in enumerate(payload.get("MixedInvoiceItems") or []):
        if item.get("Code") != "OK":
            errors.append(
                {"code": "OCR_ITEM_FAILED", "provider_code": item.get("Code"), "item_index": index}
            )
            continue
        blocks = item.get("SingleInvoiceInfos") or {}
        subtype = item.get("SubType")
        block = blocks.get(subtype) or next(
            (v for v in blocks.values() if isinstance(v, dict) and v), {}
        )
        code, label = TYPES.get(item.get("Type"), TYPES[-1])

        def first(*keys, block=block):
            return next((block[k] for k in keys if block.get(k) not in (None, "")), None)

        lines = []
        for line_number, line in enumerate(
            block.get("VatInvoiceItemInfos")
            or block.get("VatElectronicItems")
            or block.get("GeneralMachineItems")
            or block.get("VatRollItems")
            or block.get("ItemInfos")
            or [],
            1,
        ):
            rate = line.get("TaxRate")
            treatment = "unknown"
            if rate in ("免税", "不征税"):
                treatment = "exempt" if rate == "免税" else "non_taxable"
                rate = None
            elif rate not in (None, ""):
                try:
                    parsed = Decimal(str(rate).rstrip("%")) / (
                        100 if str(rate).endswith("%") else 1
                    )
                    rate = format(parsed, "f") if parsed.is_finite() else None
                    treatment = "zero_rated" if parsed == 0 else "taxable"
                except InvalidOperation:
                    rate = None
            lines.append(
                {
                    "line_number": line_number,
                    "name": line.get("Name"),
                    "quantity": decimal_text(line.get("Quantity")),
                    "unit_price": decimal_text(line.get("Price")),
                    "net_amount": money(
                        line.get("Total")
                        if block.get("VatInvoiceItemInfos") is not None
                        or block.get("VatElectronicItems") is not None
                        else line.get("AmountWithoutTax")
                    ),
                    "tax_rate": rate,
                    "tax_treatment": treatment,
                    "tax_amount": money(line.get("Tax")),
                }
            )
        data = {
            "type_code": code,
            "type_label": item.get("SubTypeDescription") or item.get("TypeDescription") or label,
            "invoice_number": first("ElectronicFullNumber", "Number", "InvoiceNumber"),
            "invoice_code": first("Code", "InvoiceCode"),
            "invoice_date": parse_date(first("Date", "InvoiceDate")),
            "buyer": {"name": first("Buyer", "BuyerName"), "tax_id": first("BuyerTaxID")},
            "seller": {"name": first("Seller", "SellerName"), "tax_id": first("SellerTaxID")},
            "gross_amount": money(first("Total", "TotalAmount", "Amount", "Fare")),
            "net_amount": money(first("PretaxAmount", "AmountWithoutTax")),
            "tax_amount": money(
                first("TaxAmount") if code == "air_transport" else first("Tax", "TaxAmount")
            ),
            "currency": first("Currency", "CurrencyCode")
            or ("UNKNOWN" if code == "overseas_invoice" else "CNY"),
            "lines": lines,
            "region": item.get("Polygon"),
            "extended_fields": {
                "provider_type": item.get("Type"),
                "subtype": subtype,
                "fields": block,
                "normalization_version": "tencent-general-v1",
            },
        }
        identity = {
            "type": code,
            "number": data["invoice_number"],
            "code": data["invoice_code"],
            "index": index if not data["invoice_number"] else None,
        }
        data["result_key"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        invoices.append(data)
    return OcrResult(payload.get("RequestId", ""), invoices, payload, errors)


def input_bytes(request: OcrInput):
    limit = 7_000_000  # Encoded payload remains below the provider's 10 MB request boundary.
    path = Path(request.file_path)
    if request.content_type == "application/pdf":
        with path.open("rb") as stream:
            reader = PdfReader(stream)
            writer = PdfWriter()
            writer.add_page(reader.pages[request.page_from - 1])
            buffer = BytesIO()
            writer.write(buffer)
        if len(buffer.getvalue()) <= limit:
            return buffer.getvalue(), True, False
        import pymupdf

        with pymupdf.open(path) as pdf:
            page = pdf.load_page(request.page_from - 1)
            scale = min(2.0, 8000 / max(page.rect.width, page.rect.height))
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    else:
        with Image.open(path) as original:
            if (
                path.stat().st_size <= limit
                and min(original.size) >= 20
                and max(original.size) <= 10000
            ):
                return path.read_bytes(), False, False
            image = original.convert("RGB")
    image.thumbnail((8000, 8000))
    if min(image.size) < 20:
        image = image.resize(tuple(max(20, v) for v in image.size))
    for _ in range(10):
        for quality in (85, 70, 50):
            output = BytesIO()
            image.save(output, format="JPEG", quality=quality)
            if output.tell() <= limit:
                return output.getvalue(), False, True
        image = image.resize(tuple(max(20, int(v * 0.8)) for v in image.size))
    raise BusinessError("OCR_INPUT_LIMIT", "单页转换后仍超过识别供应商能力，请核对原文件")


class TencentOcrProvider:
    enabled = True

    def __init__(self, settings, client=None):
        if client is None:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.ocr.v20181119.ocr_client import OcrClient

            profile = ClientProfile(
                httpProfile=HttpProfile(reqTimeout=settings.ocr_timeout_seconds)
            )
            client = OcrClient(
                credential.Credential(
                    settings.ocr_secret_id.get_secret_value(),
                    settings.ocr_secret_key.get_secret_value(),
                ),
                settings.ocr_region,
                profile,
            )
        self.client = client

    def recognize(self, request):
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
            TencentCloudSDKException,
        )
        from tencentcloud.ocr.v20181119.models import RecognizeGeneralInvoiceRequest

        content, is_pdf, converted = input_bytes(request)
        body = RecognizeGeneralInvoiceRequest()
        body.from_json_string(
            json.dumps(
                {
                    "ImageBase64": base64.b64encode(content).decode(),
                    "EnablePdf": is_pdf,
                    "PdfPageNumber": 1,
                    "EnableMultiplePage": False,
                    "EnableOther": True,
                }
            )
        )
        try:
            result = self.client.RecognizeGeneralInvoice(body)
            payload = json.loads(result.to_json_string())
        except TencentCloudSDKException as exc:
            retryable = any(
                str(exc.code).startswith(v)
                for v in (
                    "RequestLimitExceeded",
                    "InternalError",
                    "ClientNetworkError",
                    "RequestTimeout",
                )
            )
            raise BusinessError(
                "OCR_PROVIDER_ERROR",
                "票据识别服务调用失败",
                503 if retryable else 422,
                retryable=retryable,
            ) from exc
        payload["_preprocessing"] = {"converted": converted, "source_page": request.page_from}
        return normalize_response(payload)


def build_ocr(settings):
    return (
        TencentOcrProvider(settings)
        if settings.ocr_provider == "tencent"
        else DisabledOcrProvider()
    )
