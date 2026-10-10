"""Real HTTP API + fresh worker processes; local scripted model, no cloud credentials."""

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.db.models import ChatCheckpoint, Export, ExportShare, TaxCalculation
from app.db.session import make_engine, make_session_factory


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ScriptedModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/chat/completions" and data["tools"]
        tools = [json.loads(msg["content"]) for msg in data["messages"] if msg["role"] == "tool"]
        last = tools[-1] if tools else None
        tool = None
        if last is None:
            tool = ("request_input", {"prompt": "请选择拆税税率", "kind": "tax_rate"})
        elif "user_input" in last:
            tool = (
                "calculate_tax",
                {
                    "gross_amount": "113",
                    "tax_rate": last["user_input"]["tax_rate"],
                    "invoice_type_code": "general",
                    "config_version": "tax-config-v1",
                },
            )
        elif "tax_amount" in last:
            tool = (
                "create_export",
                {
                    "format": "excel",
                    "source_type": "tax_calculations",
                    "selection": {"mode": "ids", "ids": [last["id"]]},
                },
            )
        elif last.get("source_type") == "tax_calculations":
            assert last["status"] == "succeeded"
            tool = ("create_export_share", {"resource_id": last["id"], "expires_in_days": 1})
        else:
            assert last.get("result_available"), last
        message = {"role": "assistant", "content": "拆税、导出和分享已完成" if tool is None else ""}
        if tool:
            message["tool_calls"] = [
                {
                    "id": str(uuid4()),
                    "type": "function",
                    "function": {"name": tool[0], "arguments": json.dumps(tool[1])},
                }
            ]
        response = {
            "choices": [
                {"finish_reason": "stop" if tool is None else "tool_calls", "message": message}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        value = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(value)))
        self.end_headers()
        self.wfile.write(value)


def main():
    root = Path(__file__).resolve().parents[1]
    run_dir = root / "var" / ("stage4-smoke-" + str(uuid4()))
    run_dir.mkdir(parents=True)
    api_port = port()
    url = f"http://127.0.0.1:{api_port}"
    model_server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedModel)
    thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    thread.start()
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
        "FT_MODEL_PROVIDER": "chat_completions",
        "FT_MODEL_BASE_URL": f"http://127.0.0.1:{model_server.server_port}",
        "FT_MODEL_NAME": "local-scripted-test",
        "FT_MODEL_API_KEY": "local-test-key-no-cloud-credentials",
    }
    prior = dict(os.environ)
    os.environ.update(env)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = None
    with (run_dir / "process.log").open("w", encoding="utf-8") as log:

        def api_start(client):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                    "--no-access-log",
                ],
                cwd=root,
                env=env,
                stdout=log,
                stderr=log,
                creationflags=flags,
            )
            for _ in range(100):
                if child.poll() is not None:
                    raise RuntimeError("API startup failed")
                try:
                    if client.get("/health/ready").status_code == 200:
                        return child
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            child.terminate()
            child.wait(timeout=10)
            raise RuntimeError("API readiness timed out")

        def worker_once():
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

        try:
            command.upgrade(Config(str(root / "alembic.ini")), "head")
            with httpx.Client(base_url=url, timeout=30) as client:
                process = api_start(client)
                login = client.post("/api/v1/auth/dev-login", json={"account": "chat-smoke"})
                assert login.status_code == 200
                headers = {"Authorization": "Bearer " + login.json()["data"]["access_token"]}
                assert client.get("/api/v1/capabilities").json()["data"]["chat"]["enabled"]
                response = client.post(
                    "/api/v1/chat/sessions",
                    headers={**headers, "Idempotency-Key": "session"},
                    json={"title": "拆税导出与分享"},
                )
                assert response.status_code == 201
                sid = response.json()["data"]["id"]
                msg = {"client_message_id": "message1", "text": "帮我拆税113元，再导出Excel并分享"}
                response = client.post(
                    f"/api/v1/chat/sessions/{sid}/messages",
                    headers={**headers, "Idempotency-Key": "message"},
                    json=msg,
                )
                assert response.status_code == 202
                rid = response.json()["data"]["run_id"]
                worker_once()
                worker_once()
                current = client.get("/api/v1/chat/runs/" + rid, headers=headers).json()["data"]
                assert current["status"] == "waiting_input", current
                process.terminate()
                process.wait(timeout=10)
                process = api_start(client)
                response = client.post(
                    "/api/v1/chat/actions/" + current["action"]["id"] + "/submit",
                    headers=headers,
                    json={"decision": "submit", "values": {"tax_rate": "0.13"}},
                )
                assert response.status_code == 202
                action_id = None
                for _ in range(25):
                    worker_once()  # Every tick is a newly started worker process.
                    current = client.get("/api/v1/chat/runs/" + rid, headers=headers).json()["data"]
                    assert current["status"] not in {"failed", "cancelled"}, current
                    if current["status"] == "waiting_confirmation":
                        action_id = current["action"]["id"]
                        response = client.post(
                            "/api/v1/chat/actions/" + action_id + "/submit",
                            headers=headers,
                            json={"decision": "confirm"},
                        )
                        assert response.status_code == 202
                    if current["status"] == "succeeded":
                        break
                    if current["status"] == "waiting_task":
                        time.sleep(0.3)
                else:
                    raise RuntimeError("chat workflow did not finish")
                result = client.get(
                    "/api/v1/chat/actions/" + action_id + "/result", headers=headers
                )
                assert result.status_code == 200
                token = result.json()["data"]["token"]
                share = client.get("/api/v1/shares/" + token)
                assert share.status_code == 200
                link = client.get("/api/v1/shares/" + token + "/download-url").json()["data"]["url"]
                download = client.get(link)
                assert download.status_code == 200 and download.content.startswith(b"PK")
                sequence, count = 0, 0
                while True:
                    page = client.get(
                        f"/api/v1/chat/sessions/{sid}/events",
                        headers=headers,
                        params={"after_sequence": sequence, "wait_seconds": 0, "limit": 2},
                    ).json()["data"]
                    assert all(v["sequence"] > sequence for v in page["items"])
                    count += len(page["items"])
                    sequence = page["next_sequence"]
                    if not page["has_more"]:
                        break
                assert count == sequence
                engine = make_engine(env["FT_DATABASE_URL"])
                with make_session_factory(engine)() as db:
                    counts = {
                        name: db.scalar(select(func.count()).select_from(model))
                        for name, model in [
                            ("tax", TaxCalculation),
                            ("exports", Export),
                            ("shares", ExportShare),
                            ("checkpoints", ChatCheckpoint),
                        ]
                    }
                engine.dispose()
                assert counts["tax"] == counts["exports"] == counts["shares"] == 1
                assert counts["checkpoints"] > 0
                client.delete("/api/v1/chat/sessions/" + sid, headers=headers)
                assert client.get("/api/v1/chat/runs/" + rid, headers=headers).status_code == 404
                assert client.get("/api/v1/shares/" + token).status_code == 200
                result = {
                    "passed": True,
                    "version": "0.4.0",
                    "model": "local_scripted_http_not_real_llm",
                    "api_restart": True,
                    "fresh_worker_each_tick": True,
                    "usage": current["usage"],
                    "resource_counts": counts,
                    "event_count": count,
                    "shared_xlsx_bytes": len(download.content),
                    "deletion_preserves_business_resources": True,
                    "cloud_credentials_used": False,
                }
                (run_dir / "result.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                print(
                    json.dumps(
                        {"passed": True, "result": str(run_dir / "result.json")}, ensure_ascii=False
                    )
                )
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            model_server.shutdown()
            model_server.server_close()
            os.environ.clear()
            os.environ.update(prior)


if __name__ == "__main__":
    main()
