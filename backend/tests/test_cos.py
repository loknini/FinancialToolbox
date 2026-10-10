from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from qcloud_cos.cos_exception import CosServiceError

from app.core.errors import BusinessError
from app.integrations.cos import CosStorage
from app.jobs.worker import Worker
from tests.test_files import pdf_bytes


class Body:
    def __init__(self, data):
        self.stream = BytesIO(data)

    def get_stream_to_file(self, path):
        Path(path).write_bytes(self.stream.getvalue())

    def get_raw_stream(self):
        return self.stream


class FakeCosClient:
    def __init__(self):
        self.parts = {}
        self.objects = {}
        self.complete_count = 0
        self.signed = []

    def create_multipart_upload(self, **kw):
        return {"UploadId": "remote-" + kw["Key"]}

    def get_presigned_url(self, **kw):
        self.signed.append(kw)
        params = kw.get("Params", {})
        return (
            "https://private.cos.example/object?uploadId="
            + params.get("uploadId", "")
            + "&partNumber="
            + params.get("partNumber", "")
        )

    def list_parts(self, **kw):
        raw = [
            {"PartNumber": n, "ETag": etag, "Size": len(data)}
            for n, (etag, data) in sorted(self.parts.get(kw["Key"], {}).items())
        ]
        return {"Part": raw, "IsTruncated": "false"}

    def head_object(self, **kw):
        if kw["Key"] not in self.objects:
            raise CosServiceError("GET", {"Code": "NoSuchKey", "Message": "not found"}, 404)
        return {"Content-Length": str(len(self.objects[kw["Key"]]))}

    def complete_multipart_upload(self, **kw):
        self.complete_count += 1
        selected = kw["MultipartUpload"]["Part"]
        assert all(self.parts[kw["Key"]][p["PartNumber"]][0] == p["ETag"] for p in selected)
        self.objects[kw["Key"]] = b"".join(
            self.parts[kw["Key"]][p["PartNumber"]][1] for p in selected
        )
        return {}

    def get_object(self, **kw):
        return {"Body": Body(self.objects[kw["Key"]])}

    def abort_multipart_upload(self, **kw):
        self.parts.pop(kw["Key"], None)
        return {}


def setup_cos(app):
    app.state.settings.storage_backend = "cos"
    app.state.settings.cos_bucket = "private-test-123"
    client = FakeCosClient()
    storage = CosStorage(app.state.settings, client=client)
    app.state.storage = storage
    return storage, client


def test_cloud_direct_upload_manifest_validation_and_worker_recovery(client, auth, app):
    storage, cos = setup_cos(app)
    data = pdf_bytes()
    response = client.post(
        "/api/v1/upload-sessions",
        headers={**auth, "Idempotency-Key": "cos"},
        json={
            "filename": "invoice.pdf",
            "size_bytes": str(len(data)),
            "content_type": "application/pdf",
            "purpose": "invoice",
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()["data"]
    key = storage.key("objects/" + session["id"])
    signed = client.post(
        "/api/v1/upload-sessions/" + session["id"] + "/part-urls",
        headers=auth,
        json={"part_numbers": [1]},
    ).json()["data"]["parts"][0]
    assert signed["headers"] == {"Content-Type": "application/octet-stream"}
    assert parse_qs(urlsplit(signed["url"]).query)["partNumber"] == ["1"]
    assert cos.signed[-1]["Params"]["uploadId"].startswith("remote-")
    cos.parts[key] = {1: ('"real-etag"', data)}
    assert client.get("/api/v1/upload-sessions/" + session["id"], headers=auth).json()["data"][
        "parts"
    ] == [{"part_number": 1, "etag": '"real-etag"'}]
    endpoint = "/api/v1/upload-sessions/" + session["id"] + "/complete"
    bad = client.post(
        endpoint,
        headers={**auth, "Idempotency-Key": "wrong"},
        json={"parts": [{"part_number": 1, "etag": '"fake"'}]},
    )
    assert bad.status_code == 422
    submitted = client.post(
        endpoint,
        headers={**auth, "Idempotency-Key": "complete"},
        json={"parts": [{"part_number": 1, "etag": '"real-etag"'}]},
    )
    assert submitted.status_code == 202, submitted.text
    file_id = submitted.json()["data"]["file_id"]
    # COS completion succeeds before worker recovery: completing it again is safe.
    from sqlalchemy import select

    from app.db.models import UploadPart, UploadSession

    with app.state.session_factory() as db:
        row = db.get(UploadSession, session["id"])
        parts = list(db.scalars(select(UploadPart).where(UploadPart.session_id == row.id)))
        storage.complete(row, parts)
    worker = Worker(app.state.settings, app.state.session_factory, storage)
    assert worker.process_one()
    assert cos.complete_count == 1
    ready = client.get("/api/v1/files/" + file_id, headers=auth).json()["data"]
    assert ready["status"] == "ready" and ready["sha256"]
    download = client.get("/api/v1/files/" + file_id + "/download-url", headers=auth)
    assert download.status_code == 200 and cos.signed[-1]["Method"] == "GET"
    assert list(storage.cache.iterdir()) == []


def test_cloud_part_size_and_cancel(client, auth, app):
    storage, cos = setup_cos(app)
    response = client.post(
        "/api/v1/upload-sessions",
        headers={**auth, "Idempotency-Key": "cos"},
        json={
            "filename": "invoice.pdf",
            "size_bytes": "100",
            "content_type": "application/pdf",
            "purpose": "invoice",
        },
    )
    id = response.json()["data"]["id"]
    key = storage.key("objects/" + id)
    cos.parts[key] = {1: ('"etag"', b"short")}
    assert (
        client.get("/api/v1/upload-sessions/" + id, headers=auth).json()["code"]
        == "PART_SIZE_MISMATCH"
    )
    assert client.delete("/api/v1/upload-sessions/" + id, headers=auth).status_code == 200
    assert key not in cos.parts


def test_cloud_large_upload_changes_part_size_without_product_cap(client, auth, app):
    setup_cos(app)
    size = 1_000_000_000_000
    response = client.post(
        "/api/v1/upload-sessions",
        headers={**auth, "Idempotency-Key": "large"},
        json={
            "filename": "large.pdf",
            "size_bytes": str(size),
            "content_type": "application/pdf",
            "purpose": "invoice",
        },
    )
    assert response.status_code == 201, response.text
    value = response.json()["data"]
    assert value["max_part_count"] <= 10000 and value["part_size_bytes"] >= 100_000_000
    too_large = client.post(
        "/api/v1/upload-sessions",
        headers={**auth, "Idempotency-Key": "too-large"},
        json={
            "filename": "large.pdf",
            "size_bytes": str(10000 * 5 * 1024**3 + 1),
            "content_type": "application/pdf",
            "purpose": "invoice",
        },
    )
    assert too_large.status_code == 413


def test_list_parts_paginates_and_rejects_stalled_marker(app):
    storage, cos = setup_cos(app)
    calls = []

    def pages(**kw):
        calls.append(kw["PartNumberMarker"])
        n = 1 if kw["PartNumberMarker"] == 0 else 2
        return {
            "Part": {"PartNumber": n, "ETag": "etag", "Size": "10"},
            "IsTruncated": "true" if n == 1 else "false",
            "NextPartNumberMarker": "1",
        }

    cos.list_parts = pages
    row = SimpleNamespace(id="row", remote_upload_id="upload")
    assert [p["part_number"] for p in storage.list_parts(row)] == [1, 2] and calls == [0, 1]
    cos.list_parts = lambda **kw: {"Part": [], "IsTruncated": "true", "NextPartNumberMarker": "0"}
    with pytest.raises(BusinessError) as error:
        storage.list_parts(row)
    assert error.value.retryable
