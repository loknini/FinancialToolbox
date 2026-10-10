import base64
import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.ocr.v20181119.models import RecognizeGeneralInvoiceResponse

from app.core.errors import BusinessError
from app.integrations.ocr import (
    OcrInput,
    TencentOcrProvider,
    input_bytes,
    money,
    normalize_response,
)
from tests.test_invoices import payload


def test_official_sdk_response_normalization():
    response = RecognizeGeneralInvoiceResponse()
    response.from_json_string(json.dumps(payload(multi=True, failed=True)))
    result = normalize_response(json.loads(response.to_json_string()))
    assert len(result.invoices) == 2 and len(result.item_errors) == 1
    invoice = result.invoices[0]
    assert invoice["invoice_date"] == "2026-10-09"
    assert invoice["gross_amount"] == "113.00" and invoice["lines"][0]["tax_rate"] == "0.13"
    assert invoice["buyer"]["tax_id"] == "TAX1"
    value = payload()
    value["MixedInvoiceItems"][0]["Polygon"] = {"LeftTop": {"X": 100, "Y": 200}}
    assert normalize_response(value).invoices[0]["result_key"] == invoice["result_key"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", None),
        ("not-money", None),
        ("NaN", None),
        ("Infinity", None),
        ("0", "0.00"),
        ("-113", "-113.00"),
        ("1.001", None),
        ("￥1,234.56", "1234.56"),
        ("1e22", None),
    ],
)
def test_money_does_not_silently_guess(value, expected):
    assert money(value) == expected


def test_unknown_fields_preserved_missing_and_zero_distinct():
    value = payload()
    item = value["MixedInvoiceItems"][0]
    item["Type"] = 999
    item["SingleInvoiceInfos"]["VatSpecialInvoice"]["Tax"] = "0"
    item["SingleInvoiceInfos"]["VatSpecialInvoice"]["Total"] = ""
    item["SingleInvoiceInfos"]["VatSpecialInvoice"]["CustomField"] = "raw-value"
    result = normalize_response(value).invoices[0]
    assert result["type_code"] == "unknown" and result["gross_amount"] is None
    assert (
        result["tax_amount"] == "0.00"
        and result["extended_fields"]["fields"]["CustomField"] == "raw-value"
    )


def test_sdk_request_extracts_exact_page(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=101, height=100)
    writer.add_blank_page(width=202, height=200)
    path = tmp_path / "two.pdf"
    with path.open("wb") as output:
        writer.write(output)

    class Client:
        def RecognizeGeneralInvoice(self, request):
            assert (
                request.EnablePdf
                and request.PdfPageNumber == 1
                and request.EnableMultiplePage is False
            )
            pdf = PdfReader(BytesIO(base64.b64decode(request.ImageBase64)))
            assert len(pdf.pages) == 1 and pdf.pages[0].mediabox.width == 202
            response = RecognizeGeneralInvoiceResponse()
            response.from_json_string(json.dumps(payload()))
            return response

    result = TencentOcrProvider(SimpleNamespace(), client=Client()).recognize(
        OcrInput(str(path), 2, 2, "key")
    )
    assert (
        result.provider_request_id == "provider-request"
        and result.raw_response["_preprocessing"]["source_page"] == 2
    )


def test_oversize_pdf_page_falls_back_to_image(tmp_path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject()
    stream.set_data(b" " * 7_100_000)
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "large.pdf"
    with path.open("wb") as output:
        writer.write(output)
    content, is_pdf, converted = input_bytes(OcrInput(str(path), 1, 1, "large"))
    assert converted and not is_pdf and len(content) < 7_000_000
    with Image.open(BytesIO(content)) as image:
        assert image.format == "JPEG"
    assert path.stat().st_size > 7_000_000


def test_small_image_is_preprocessed_for_provider(tmp_path):
    path = tmp_path / "tiny.png"
    Image.new("RGB", (5, 5)).save(path)
    content, is_pdf, converted = input_bytes(
        OcrInput(str(path), 1, 1, "tiny", content_type="image/png")
    )
    assert converted and not is_pdf
    with Image.open(BytesIO(content)) as image:
        assert min(image.size) >= 20


@pytest.mark.parametrize(
    "code,retryable",
    [
        ("RequestLimitExceeded", True),
        ("InternalError", True),
        ("AuthFailure.SecretIdNotFound", False),
    ],
)
def test_sdk_errors_are_sanitized(tmp_path, code, retryable):
    path = tmp_path / "image.png"
    Image.new("RGB", (32, 32)).save(path)

    class Client:
        def RecognizeGeneralInvoice(self, request):
            raise TencentCloudSDKException(code, "secret-details")

    with pytest.raises(BusinessError) as error:
        TencentOcrProvider(SimpleNamespace(), client=Client()).recognize(
            OcrInput(str(path), 1, 1, "key", content_type="image/png")
        )
    assert error.value.retryable == retryable and "secret-details" not in error.value.message


def test_airport_tax_is_not_mapped_to_vat():
    value = payload()
    item = value["MixedInvoiceItems"][0]
    item["Type"] = 5
    item["SubType"] = "AirTransport"
    item["SingleInvoiceInfos"] = {"AirTransport": {"Total": "113", "Tax": "50", "TaxAmount": "6"}}
    assert normalize_response(value).invoices[0]["tax_amount"] == "6.00"
    item["SingleInvoiceInfos"]["AirTransport"].pop("TaxAmount")
    assert normalize_response(value).invoices[0]["tax_amount"] is None


def test_digital_invoice_lines_are_normalized():
    value = payload()
    item = value["MixedInvoiceItems"][0]
    item["Type"] = 16
    item["SubType"] = "VatElectronicInvoiceFull"
    block = item["SingleInvoiceInfos"].pop("VatSpecialInvoice")
    block["VatElectronicItems"] = block.pop("VatInvoiceItemInfos")
    item["SingleInvoiceInfos"]["VatElectronicInvoiceFull"] = block
    assert len(normalize_response(value).invoices[0]["lines"]) == 1


def test_general_line_total_does_not_assume_pretax_amount():
    value = payload()
    item = value["MixedInvoiceItems"][0]
    item["Type"] = 8
    item["SubType"] = "MachinePrintedInvoice"
    item["SingleInvoiceInfos"] = {
        "MachinePrintedInvoice": {
            "Total": "113",
            "GeneralMachineItems": [{"Name": "item", "Total": "113", "Price": "113"}],
        }
    }
    result = normalize_response(value).invoices[0]
    assert result["lines"][0]["net_amount"] is None
    assert result["extended_fields"]["fields"]["GeneralMachineItems"][0]["Total"] == "113"
