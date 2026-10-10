from copy import deepcopy
from datetime import timedelta
from io import BytesIO

import pytest
from pypdf import PdfWriter
from sqlalchemy import func, select

from app.core.errors import BusinessError
from app.core.security import now
from app.db.models import Invoice, InvoiceRevision, Job, OcrResponse
from app.integrations.ocr import normalize_response
from app.jobs.worker import Worker
from tests.helpers import upload


def payload(number="100", *, total="113.00", failed=False, multi=False):
    item = {
        "Code": "OK",
        "Type": 3,
        "SubType": "VatSpecialInvoice",
        "Polygon": {"LeftTop": {"X": 0, "Y": 0}},
        "SingleInvoiceInfos": {
            "VatSpecialInvoice": {
                "Number": number,
                "Code": "001",
                "Date": "2026年10月9日",
                "Total": total,
                "PretaxAmount": "100.00",
                "Tax": "13.00",
                "Buyer": "购买方",
                "BuyerTaxID": "TAX1",
                "Seller": "供应商",
                "SellerTaxID": "TAX2",
                "VatInvoiceItemInfos": [
                    {
                        "Name": "服务",
                        "Quantity": "1",
                        "Price": "100",
                        "Total": "100",
                        "TaxRate": "13%",
                        "Tax": "13",
                    }
                ],
            }
        },
    }
    items = [item]
    if multi:
        other = deepcopy(item)
        other["SingleInvoiceInfos"]["VatSpecialInvoice"]["Number"] = "200"
        items.append(other)
    if failed:
        items.append({"Code": "FailedOperation.UnsupportedInvoice", "Type": -1})
    return {"RequestId": "provider-request", "MixedInvoiceItems": items}


class ScriptedOcr:
    enabled = True

    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def recognize(self, request):
        self.calls.append(request)
        return (
            self.callback(request)
            if self.callback
            else normalize_response(payload(str(request.page_from)))
        )


def setup_batch(client, auth, app, *, pages=1, callback=None):
    app.state.ocr = ScriptedOcr(callback)
    worker = Worker(app.state.settings, app.state.session_factory, app.state.storage, app.state.ocr)
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    buffer = BytesIO()
    writer.write(buffer)
    _, file, _ = upload(client, auth, buffer.getvalue())
    assert worker.process_one()
    response = client.post(
        "/api/v1/invoice-batches",
        headers={**auth, "Idempotency-Key": "batch-one"},
        json={"file_ids": [file["file_id"]]},
    )
    assert response.status_code == 202, response.text
    return response.json()["data"], worker, file


def drain(worker):
    for _ in range(600):
        if not worker.process_one():
            return
    pytest.fail("queue did not drain")


def get_batch(client, auth, batch):
    response = client.get("/api/v1/invoice-batches/" + batch["id"], headers=auth)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_multpage_pdf_and_ordered_cursor(client, auth, app):
    batch, worker, file = setup_batch(client, auth, app, pages=31)
    assert batch["progress"]["total_units"] is None
    drain(worker)
    result = get_batch(client, auth, batch)
    assert result["status"] == "succeeded"
    assert result["invoice_count"] == 31 and result["progress"]["total_units"] == 31
    assert result["progress"]["percent"] == 100 and len(app.state.ocr.calls) == 31
    cursor = None
    pages = []
    while True:
        params = {"limit": 7}
        if cursor:
            params["cursor"] = cursor
        response = client.get(
            "/api/v1/invoice-batches/" + batch["id"] + "/items", headers=auth, params=params
        ).json()["data"]
        pages.extend(i["page_from"] for i in response["items"])
        cursor = response["next_cursor"]
        if not cursor:
            break
    assert pages == list(range(1, 32))
    replay = client.post(
        "/api/v1/invoice-batches",
        headers={**auth, "Idempotency-Key": "batch-one"},
        json={"file_ids": [file["file_id"]]},
    )
    assert replay.json()["data"]["id"] == batch["id"]
    assert (
        client.get("/api/v1/records?type=invoice_batch", headers=auth).json()["data"]["items"][0][
            "resource_id"
        ]
        == batch["id"]
    )
    assert client.get("/api/v1/users/me/tools", headers=auth).json()["data"]["items"][0]["enabled"]


def test_partial_retry_preserves_corrected_confirmed_invoice(client, auth, app):
    calls = []

    def recognize(request):
        calls.append(request)
        value = payload(failed=len(calls) == 1, multi=len(calls) > 1)
        # Provider region jitter must not duplicate the manually corrected invoice.
        value["MixedInvoiceItems"][0]["Polygon"] = {"LeftTop": {"X": 2, "Y": 3}}
        return normalize_response(value)

    batch, worker, _ = setup_batch(client, auth, app, callback=recognize)
    drain(worker)
    assert get_batch(client, auth, batch)["status"] == "partial_failed"
    row = client.get("/api/v1/invoices", headers=auth).json()["data"]["items"][0]
    patched = client.patch(
        "/api/v1/invoices/" + row["id"],
        headers=auth,
        json={
            "expected_version": row["version"],
            "invoice_number": "corrected",
            "seller": {"name": "核对名称"},
        },
    )
    assert patched.status_code == 200, patched.text
    value = patched.json()["data"]
    assert value["seller"]["tax_id"] == "TAX2"
    confirmed = client.post(
        "/api/v1/invoices/" + row["id"] + "/confirm",
        headers=auth,
        json={"expected_version": value["version"]},
    ).json()["data"]
    response = client.post(
        "/api/v1/invoice-batches/" + batch["id"] + "/retry",
        headers={**auth, "Idempotency-Key": "retry-one"},
        json={"scope": "failed"},
    )
    assert response.status_code == 202, response.text
    drain(worker)
    result = get_batch(client, auth, batch)
    assert result["status"] == "succeeded" and result["invoice_count"] == 2
    detail = client.get("/api/v1/invoices/" + row["id"], headers=auth).json()["data"]
    assert detail["invoice_number"] == "corrected" and detail["review_status"] == "confirmed"
    assert detail["version"] == confirmed["version"]
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(OcrResponse)) == 2
        assert db.scalar(select(func.count()).select_from(InvoiceRevision)) == 2
        assert db.get(Invoice, row["id"]).original_data["invoice_number"] == "100"
    assert len(calls) == 2


def test_review_amount_validation_versions_and_ownership(client, auth, app):
    batch, worker, _ = setup_batch(
        client,
        auth,
        app,
        callback=lambda req: normalize_response(payload(total="114.00", multi=True)),
    )
    drain(worker)
    rows = client.get("/api/v1/invoices", headers=auth).json()["data"]["items"]
    assert len(rows) == 2
    row = rows[0]
    path = "/api/v1/invoices/" + row["id"]
    assert (
        client.post(
            path + "/confirm", headers=auth, json={"expected_version": row["version"]}
        ).json()["code"]
        == "REVIEW_REQUIRED"
    )
    corrected = client.patch(
        path, headers=auth, json={"expected_version": row["version"], "gross_amount": "113"}
    ).json()["data"]
    assert corrected["gross_amount"] == "113.00"
    assert (
        client.patch(
            path, headers=auth, json={"expected_version": row["version"], "gross_amount": "113"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            path + "/confirm", headers=auth, json={"expected_version": corrected["version"]}
        ).status_code
        == 200
    )
    other = client.post("/api/v1/auth/dev-login", json={"account": "bob"}).json()["data"]
    headers = {"Authorization": "Bearer " + other["access_token"]}
    assert client.get(path, headers=headers).status_code == 404
    assert client.get("/api/v1/invoice-batches/" + batch["id"], headers=headers).status_code == 404
    assert (
        client.get("/api/v1/invoices?batch_id=" + batch["id"], headers=headers).status_code == 404
    )
    assert (
        client.get("/api/v1/invoices?keyword=供应商&review_status=confirmed", headers=auth).json()[
            "data"
        ]["items"][0]["id"]
        == row["id"]
    )
    response = client.post(
        "/api/v1/tax-calculations",
        headers={**auth, "Idempotency-Key": "tax-related"},
        json={
            "gross_amount": "113",
            "tax_rate": "0.13",
            "invoice_type_code": "general",
            "config_version": "tax-config-v1",
            "source_invoice_id": row["id"],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["source_invoice_id"] == row["id"]


def test_cancel_queued_and_inflight_result_ignored(client, auth, app):
    batch, worker, _ = setup_batch(client, auth, app)
    assert worker.process_one()  # Discover
    assert worker.process_one()  # Expand
    job_id, token = worker.claim()
    response = client.post(
        "/api/v1/invoice-batches/" + batch["id"] + "/cancel", headers=auth, json={}
    )
    assert response.status_code == 200 and response.json()["data"]["status"] == "cancelling"
    worker.finish(job_id, token, {"ocr": normalize_response(payload())}, None)
    final = get_batch(client, auth, batch)
    assert final["status"] == "cancelled" and final["invoice_count"] == 0
    assert final["progress"]["cancelled_units"] == 1
    assert not worker.process_one()


def test_retryable_provider_error_and_stale_worker(client, auth, app):
    def recognize(req):
        if len(app.state.ocr.calls) == 1:
            raise BusinessError("OCR_PROVIDER_ERROR", "temporary", 503, retryable=True)
        return normalize_response(payload())

    batch, worker, _ = setup_batch(client, auth, app, callback=recognize)
    drain(worker)
    assert get_batch(client, auth, batch)["status"] == "processing"
    with app.state.session_factory.begin() as db:
        job = db.scalar(select(Job).where(Job.type == "recognize_unit", Job.status == "queued"))
        job.available_at = now() - timedelta(seconds=1)
    old_id, old_token = worker.claim()
    with app.state.session_factory.begin() as db:
        db.get(Job, old_id).lease_until = now() - timedelta(seconds=1)
    new_id, new_token = worker.claim()
    worker.finish(old_id, old_token, {"ocr": normalize_response(payload("stale"))}, None)
    assert client.get("/api/v1/invoices", headers=auth).json()["data"]["items"] == []
    worker.finish(new_id, new_token, {"ocr": normalize_response(payload("winner"))}, None)
    assert get_batch(client, auth, batch)["status"] == "succeeded"
    assert (
        client.get("/api/v1/invoices", headers=auth).json()["data"]["items"][0]["invoice_number"]
        == "winner"
    )


def test_disabled_ocr_rejects_batch_without_jobs(client, auth, app):
    response = client.post(
        "/api/v1/invoice-batches",
        headers={**auth, "Idempotency-Key": "disabled"},
        json={"file_ids": ["missing"]},
    )
    assert response.status_code == 403
    with app.state.session_factory() as db:
        assert db.scalar(select(Job.id)) is None


def test_empty_recognition_does_not_create_fake_invoice(client, auth, app):
    batch, worker, _ = setup_batch(
        client,
        auth,
        app,
        callback=lambda req: normalize_response({"MixedInvoiceItems": [], "RequestId": "empty"}),
    )
    drain(worker)
    result = get_batch(client, auth, batch)
    assert result["status"] == "failed" and result["invoice_count"] == 0
    assert result["error"]["code"] == "NO_INVOICE_FOUND"


def test_discovery_error_retry_and_chunked_expansion(client, auth, app):
    from app.db.models import BatchFile, File, InvoiceUnit
    from app.integrations.storage import file_hash

    batch, worker, file = setup_batch(client, auth, app, pages=101)
    with app.state.session_factory() as db:
        row = db.get(File, file["file_id"])
        path = app.state.storage.object_path(row.object_key)
    saved = path.read_bytes()
    path.write_bytes(b"broken")
    drain(worker)
    assert get_batch(client, auth, batch)["status"] == "failed"
    path.write_bytes(saved)
    response = client.post(
        "/api/v1/invoice-batches/" + batch["id"] + "/retry",
        headers={**auth, "Idempotency-Key": "discover-retry"},
        json={"scope": "failed"},
    )
    assert response.status_code == 202, response.text
    assert worker.process_one() and worker.process_one()
    with app.state.session_factory.begin() as db:
        link = db.scalar(select(BatchFile).where(BatchFile.batch_id == batch["id"]))
        assert link.discovered_pages == 100 and not link.discovery_complete
        assert db.scalar(select(func.count()).select_from(InvoiceUnit)) == 100
        assert file_hash(path) == db.get(File, file["file_id"]).sha256
        # Let expansion proceed before OCR; discovery does not need one huge insert.
        for job in db.scalars(
            select(Job).where(Job.type == "recognize_unit", Job.status == "queued")
        ):
            job.available_at = now() + timedelta(hours=1)
    assert worker.process_one()
    result = get_batch(client, auth, batch)
    assert result["discovery_complete"] and result["progress"]["total_units"] == 101


def test_mixed_records_cursor_and_filter_binding(client, auth, app):
    batch, worker, _ = setup_batch(client, auth, app)
    drain(worker)
    for n in range(2):
        response = client.post(
            "/api/v1/tax-calculations",
            headers={**auth, "Idempotency-Key": "record-tax-" + str(n)},
            json={
                "gross_amount": "113",
                "tax_rate": "0.13",
                "invoice_type_code": "general",
                "config_version": "tax-config-v1",
            },
        )
        assert response.status_code == 201
    first = client.get("/api/v1/records?limit=1", headers=auth).json()["data"]
    assert first["has_more"]
    next_page = client.get(
        "/api/v1/records", headers=auth, params={"limit": 100, "cursor": first["next_cursor"]}
    ).json()["data"]
    assert len(next_page["items"]) == 2 and not next_page["has_more"]
    assert len({item["id"] for item in first["items"] + next_page["items"]}) == 3
    assert (
        client.get(
            "/api/v1/records",
            headers=auth,
            params={"type": "invoice_batch", "cursor": first["next_cursor"]},
        ).status_code
        == 400
    )


def test_invoice_tools_share_permissions_versions_and_idempotency(client, auth, app):
    from app.agent.tools import ToolContext, build_registry

    batch, worker, file = setup_batch(client, auth, app)
    drain(worker)
    user_id = client.get("/api/v1/users/me", headers=auth).json()["data"]["id"]
    registry = build_registry()
    row = client.get("/api/v1/invoices", headers=auth).json()["data"]["items"][0]
    with app.state.session_factory.begin() as db:
        ctx = ToolContext(db, user_id, "agent-step", app.state.ocr, approved=True)
        assert registry.execute("get_invoice", {"resource_id": row["id"]}, ctx)["id"] == row["id"]
        with pytest.raises(BusinessError):
            registry.execute(
                "patch_invoice", {"resource_id": row["id"], "expected_version": row["version"]}, ctx
            )
        changed = registry.execute(
            "patch_invoice",
            {
                "resource_id": row["id"],
                "expected_version": row["version"],
                "seller": {"name": "agent-correction"},
            },
            ctx,
        )
        assert changed["seller"]["name"] == "agent-correction"
        created = registry.execute(
            "create_invoice_batch", {"file_ids": [file["file_id"]], "source": "chat"}, ctx
        )
        assert (
            registry.execute(
                "create_invoice_batch", {"file_ids": [file["file_id"]], "source": "chat"}, ctx
            )
            == created
        )
        with pytest.raises(BusinessError):
            registry.execute("get_invoice", {"resource_id": row["id"], "user_id": "other"}, ctx)
    with app.state.session_factory() as db:
        with pytest.raises(BusinessError):
            registry.execute(
                "get_invoice", {"resource_id": row["id"]}, ToolContext(db, "other-user", "key")
            )
