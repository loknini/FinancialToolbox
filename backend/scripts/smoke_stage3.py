"""Verify a real local API process and worker without cloud credentials."""

import io
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from pypdf import PdfWriter


def main():
    root = Path(__file__).resolve().parents[1]
    run_dir = root / "var" / ("stage3-smoke-" + str(uuid4()))
    run_dir.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = "http://127.0.0.1:" + str(port)
    env = {
        **os.environ,
        "FT_DATABASE_URL": "sqlite:///" + (run_dir / "smoke.db").as_posix(),
        "FT_AUTH_SIGNING_KEY": secrets.token_urlsafe(48),
        "FT_ENVIRONMENT": "test",
        "FT_ENABLE_DEV_LOGIN": "true",
        "FT_PUBLIC_BASE_URL": url,
        "FT_STORAGE_ROOT": str(run_dir / "storage"),
        "FT_STORAGE_BACKEND": "local",
        "FT_OCR_PROVIDER": "disabled",
    }
    prior = dict(os.environ)
    try:
        os.environ.update(env)
        command.upgrade(Config(str(root / "alembic.ini")), "head")
    finally:
        os.environ.clear()
        os.environ.update(prior)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (run_dir / "api.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            cwd=root,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=flags,
        )
        try:
            with httpx.Client(base_url=url, timeout=5) as client:
                for _ in range(60):
                    if process.poll() is not None:
                        raise RuntimeError("API failed to start")
                    try:
                        if client.get("/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("API readiness timed out")
                login = client.post("/api/v1/auth/dev-login", json={"account": "smoke"})
                assert login.status_code == 200
                headers = {"Authorization": "Bearer " + login.json()["data"]["access_token"]}
                tax = client.post(
                    "/api/v1/tax-calculations",
                    headers={**headers, "Idempotency-Key": "smoke-tax"},
                    json={
                        "gross_amount": "113",
                        "tax_rate": "0.13",
                        "invoice_type_code": "general",
                        "config_version": "tax-config-v1",
                    },
                )
                assert tax.status_code == 201 and tax.json()["data"]["tax_amount"] == "13.00"
                writer = PdfWriter()
                writer.add_blank_page(width=100, height=100)
                buffer = io.BytesIO()
                writer.write(buffer)
                data = buffer.getvalue()
                uploaded = client.post(
                    "/api/v1/upload-sessions",
                    headers={**headers, "Idempotency-Key": "smoke-upload"},
                    json={
                        "filename": "smoke.pdf",
                        "size_bytes": str(len(data)),
                        "content_type": "application/pdf",
                        "purpose": "invoice",
                    },
                )
                assert uploaded.status_code == 201
                id = uploaded.json()["data"]["id"]
                part = client.post(
                    "/api/v1/upload-sessions/" + id + "/part-urls",
                    headers=headers,
                    json={"part_numbers": [1]},
                ).json()["data"]["parts"][0]
                signed = urlsplit(part["url"])
                result = client.put(
                    signed.path + "?" + signed.query, headers=part["headers"], content=data
                )
                assert result.status_code == 200
                submitted = client.post(
                    "/api/v1/upload-sessions/" + id + "/complete",
                    headers={**headers, "Idempotency-Key": "smoke-complete"},
                    json={"parts": [{"part_number": 1, "etag": result.headers["ETag"]}]},
                )
                assert submitted.status_code == 202
                subprocess.run(
                    [sys.executable, "-m", "app.jobs.worker", "--once"],
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=log,
                    check=True,
                    timeout=30,
                    creationflags=flags,
                )
                file_id = submitted.json()["data"]["file_id"]
                assert (
                    client.get("/api/v1/files/" + file_id, headers=headers).json()["data"]["status"]
                    == "ready"
                )
                assert (
                    client.post(
                        "/api/v1/invoice-batches",
                        headers={**headers, "Idempotency-Key": "disabled"},
                        json={"file_ids": [file_id]},
                    ).status_code
                    == 403
                )
                export = client.post(
                    "/api/v1/exports",
                    headers={**headers, "Idempotency-Key": "smoke-export"},
                    json={
                        "format": "pdf",
                        "source_type": "tax_calculations",
                        "selection": {"mode": "filter"},
                    },
                )
                assert export.status_code == 202
                export_id = export.json()["data"]["id"]
                subprocess.run(
                    [sys.executable, "-m", "app.jobs.worker", "--once"],
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=log,
                    check=True,
                    timeout=30,
                    creationflags=flags,
                )
                assert (
                    client.get("/api/v1/exports/" + export_id, headers=headers).json()["data"][
                        "status"
                    ]
                    == "succeeded"
                )
                share = client.post(
                    "/api/v1/exports/" + export_id + "/share",
                    headers={**headers, "Idempotency-Key": "smoke-share"},
                    json={},
                )
                assert share.status_code == 201
                share_token = share.json()["data"]["token"]
                public = client.get("/api/v1/shares/" + share_token + "/download-url")
                assert public.status_code == 200
                signed = urlsplit(public.json()["data"]["url"])
                downloaded = client.get(signed.path + "?" + signed.query)
                assert downloaded.status_code == 200 and downloaded.content.startswith(b"%PDF-")
                assert (
                    client.delete(
                        "/api/v1/exports/" + export_id + "/share", headers=headers
                    ).status_code
                    == 200
                )
                assert client.get("/api/v1/shares/" + share_token).status_code == 404
                report = {
                    "version": "0.3.0",
                    "live_http_smoke": "passed",
                    "ready": "passed",
                    "tax": "passed",
                    "upload_and_worker": "passed",
                    "disabled_ocr_gate": "passed",
                    "cloud_network_used": False,
                    "export_share_download_revoke": "passed",
                }
                (run_dir / "result.json").write_text(
                    json.dumps(report, indent=2) + "\n", encoding="utf-8"
                )
                print(json.dumps(report))
                print("Artifact: " + str(run_dir / "result.json"))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
