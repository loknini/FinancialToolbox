from datetime import timedelta
from io import BytesIO
from random import Random

import pytest
from PIL import Image
from pypdf import PdfWriter
from sqlalchemy import select

from app.core.security import now, sign
from app.db.models import File, Job, UploadSession
from tests.helpers import local_url, upload


def pdf_bytes(*, encrypted=False):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    if encrypted:
        writer.encrypt("password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_roundtrip_and_complete_replay(client, auth, worker):
    data = pdf_bytes()
    session, complete, parts = upload(client, auth, data)
    before = client.get("/api/v1/files/" + complete["file_id"], headers=auth)
    assert before.json()["data"]["status"] == "validating"
    assert (
        client.get(
            "/api/v1/files/" + complete["file_id"] + "/download-url", headers=auth
        ).status_code
        == 409
    )
    replay = client.post(
        "/api/v1/upload-sessions/" + session["id"] + "/complete",
        headers={**auth, "Idempotency-Key": "complete-upload-one"},
        json={"parts": parts},
    )
    assert replay.status_code == 202 and replay.json()["data"]["file_id"] == complete["file_id"]
    assert worker.process_one()
    assert not worker.process_one()
    ready = client.get("/api/v1/files/" + complete["file_id"], headers=auth).json()["data"]
    assert ready["status"] == "ready" and ready["sha256"]
    signed = client.get(
        "/api/v1/files/" + complete["file_id"] + "/download-url", headers=auth
    ).json()["data"]
    result = client.get(local_url(signed["url"]))
    assert result.status_code == 200 and result.content == data


def test_image_multipart_roundtrip(client, auth, worker):
    image = Image.frombytes("RGB", (32, 32), Random(42).randbytes(3072))
    data = BytesIO()
    image.save(data, format="PNG")
    session, complete, parts = upload(client, auth, data.getvalue(), content_type="image/png")
    assert session["max_part_count"] > 1 and len(parts) > 1
    assert worker.process_one()
    assert (
        client.get("/api/v1/files/" + complete["file_id"], headers=auth).json()["data"]["status"]
        == "ready"
    )


@pytest.mark.parametrize(
    "data,code", [(b"not-a-pdf", "UNSUPPORTED_FORMAT"), (b"%PDF-broken", "CORRUPTED_FILE")]
)
def test_invalid_pdf_rejected(client, auth, worker, data, code):
    _, complete, _ = upload(client, auth, data)
    assert worker.process_one()
    file = client.get("/api/v1/files/" + complete["file_id"], headers=auth).json()["data"]
    assert file["status"] == "rejected" and file["error"]["code"] == code


def test_encrypted_pdf_rejected(client, auth, worker):
    _, complete, _ = upload(client, auth, pdf_bytes(encrypted=True))
    worker.process_one()
    assert (
        client.get("/api/v1/files/" + complete["file_id"], headers=auth).json()["data"]["error"][
            "code"
        ]
        == "PASSWORD_PROTECTED_PDF"
    )


def test_cross_user_and_purpose_tokens(client, auth, app):
    data = pdf_bytes()
    session, complete, _ = upload(client, auth, data)
    other = client.post("/api/v1/auth/dev-login", json={"account": "bob"}).json()["data"]
    headers = {"Authorization": "Bearer " + other["access_token"]}
    assert client.get("/api/v1/files/" + complete["file_id"], headers=headers).status_code == 404
    assert (
        client.get("/api/v1/upload-sessions/" + session["id"], headers=headers).status_code == 404
    )
    access = auth["Authorization"].removeprefix("Bearer ")
    assert client.put("/api/v1/storage/parts?token=" + access, content=data).status_code == 401
    forged = sign(app.state.settings, "download", {"file_id": complete["file_id"]}, 300)
    assert client.put("/api/v1/storage/parts?token=" + forged, content=data).status_code == 401


def test_expired_lease_recovery_and_stale_worker(client, auth, app, worker):
    _, complete, _ = upload(client, auth, pdf_bytes())
    old_id, old_token = worker.claim()
    with app.state.session_factory.begin() as db:
        job = db.get(Job, old_id)
        job.lease_until = now() - timedelta(seconds=1)
    new_id, new_token = worker.claim()
    assert old_id == new_id and old_token != new_token
    worker.finish(old_id, old_token, {"sha256": "incorrect"}, None)
    with app.state.session_factory() as db:
        assert db.get(File, complete["file_id"]).status == "validating"
        file = db.get(File, complete["file_id"])
        result = worker.validate_file(db, file, new_token)
    worker.finish(new_id, new_token, result, None)
    assert (
        client.get("/api/v1/files/" + complete["file_id"], headers=auth).json()["data"]["status"]
        == "ready"
    )


def test_part_size_and_upload_ownership(client, auth):
    created = client.post(
        "/api/v1/upload-sessions",
        headers={**auth, "Idempotency-Key": "new"},
        json={
            "filename": "test.pdf",
            "content_type": "application/pdf",
            "size_bytes": "5",
            "purpose": "invoice",
        },
    )
    id = created.json()["data"]["id"]
    signed = client.post(
        f"/api/v1/upload-sessions/{id}/part-urls", headers=auth, json={"part_numbers": [1]}
    ).json()["data"]["parts"][0]
    assert client.put(local_url(signed["url"]), content=b"abcdef").status_code == 422
    assert (
        client.post(
            f"/api/v1/upload-sessions/{id}/part-urls", headers=auth, json={"part_numbers": [0]}
        ).status_code
        == 422
    )
    assert client.delete(f"/api/v1/upload-sessions/{id}", headers=auth).status_code == 200
    assert client.put(local_url(signed["url"]), content=b"abcde").status_code == 409


def test_metadata_tampering_does_not_create_file_or_job(client, auth, app):
    created = client.post(
        "/api/v1/upload-sessions",
        headers={**auth, "Idempotency-Key": "new"},
        json={
            "filename": "test.pdf",
            "content_type": "application/pdf",
            "size_bytes": "5",
            "purpose": "invoice",
        },
    )
    id = created.json()["data"]["id"]
    result = client.post(
        f"/api/v1/upload-sessions/{id}/complete",
        headers={**auth, "Idempotency-Key": "bad"},
        json={"parts": [{"part_number": 1, "etag": "made-up"}]},
    )
    assert result.status_code == 422
    with app.state.session_factory() as db:
        assert db.scalar(select(File.id)) is None
        assert db.scalar(select(Job.id)) is None
        assert db.get(UploadSession, id).status == "uploading"


def test_filename_path_rejected(client, auth):
    for name in ["../x.pdf", r"folder\x.pdf", "x\u0000.pdf"]:
        result = client.post(
            "/api/v1/upload-sessions",
            headers={**auth, "Idempotency-Key": "name"},
            json={
                "filename": name,
                "content_type": "application/pdf",
                "size_bytes": "1",
                "purpose": "invoice",
            },
        )
        assert result.status_code == 422
