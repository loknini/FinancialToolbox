import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.agent.tools import ToolContext, build_registry
from app.core.errors import BusinessError
from app.core.security import now
from app.db.models import (
    AgentRun,
    AgentStep,
    ChatAction,
    ChatCheckpoint,
    ChatEvent,
    ChatMessage,
    ChatSession,
    ExportShare,
    Job,
    TaxCalculation,
    User,
)
from app.integrations.model import ChatCompletionsProvider, ModelReply, ToolCall
from app.jobs.worker import Worker
from tests.test_exports import create_export, create_tax, finished

TAX = {
    "gross_amount": "113.00",
    "tax_rate": "0.13",
    "invoice_type_code": "general",
    "config_version": "tax-config-v1",
}


class ScriptModel:
    enabled = True

    def __init__(self, replies):
        self.replies = iter(replies)
        self.messages = []

    def respond(self, messages, tools):
        self.messages.append(messages)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(messages)
        return reply


def call(name, arguments, id="call1"):
    return ModelReply("", [ToolCall(id, name, arguments)], 100, 10)


def text(value="已完成"):
    return ModelReply(value, [], 100, 10)


def session(client, auth, title="测试会话"):
    response = client.post(
        "/api/v1/chat/sessions",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"title": title},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def start(app, client, auth, worker, replies, **kwargs):
    model = ScriptModel(replies)
    app.state.model = worker.model = model
    sid = session(client, auth)["id"]
    body = {"text": "完成请求", "client_message_id": str(uuid4()), **kwargs}
    headers = {**auth, "Idempotency-Key": str(uuid4())}
    response = client.post(f"/api/v1/chat/sessions/{sid}/messages", headers=headers, json=body)
    assert response.status_code == 202, response.text
    return sid, response.json()["data"]["run_id"], model, headers, body


def run(client, auth, rid):
    response = client.get("/api/v1/chat/runs/" + rid, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def pump(app, client, auth, worker, rid, until="succeeded"):
    for _ in range(50):
        current = run(client, auth, rid)
        if current["status"] == until:
            return current
        assert current["status"] not in {"failed", "cancelled"}, current
        # Advance waiting jobs only; input/confirmation expiration jobs stay scheduled.
        with app.state.session_factory.begin() as db:
            row = db.get(AgentRun, rid)
            if row.state["phase"] == "wait":
                for job in db.scalars(
                    select(Job).where(Job.resource_id == rid, Job.status == "queued")
                ):
                    job.available_at = now()
        assert worker.process_one(), current
    raise AssertionError("workflow did not finish")


def submit(client, auth, action, decision="confirm", **kwargs):
    return client.post(
        "/api/v1/chat/actions/" + action["id"] + "/submit",
        headers=auth,
        json={"decision": decision, **kwargs},
    )


def test_disabled_model_capability_and_session(client, auth):
    sid = session(client, auth)["id"]
    assert client.get("/api/v1/capabilities").json()["data"]["chat"]["enabled"] is False
    response = client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        headers={**auth, "Idempotency-Key": "disabled"},
        json={"text": "你好", "client_message_id": "m"},
    )
    assert response.status_code == 403


def test_chat_tax_checkpoint_events_and_replay(app, client, auth, worker):
    sid, rid, model, headers, body = start(
        app, client, auth, worker, [call("calculate_tax", TAX), text("税额为13元")]
    )
    replay = client.post(f"/api/v1/chat/sessions/{sid}/messages", headers=headers, json=body)
    assert replay.json()["data"]["run_id"] == rid
    changed = {**body, "text": "不同内容"}
    assert (
        client.post(
            f"/api/v1/chat/sessions/{sid}/messages", headers=headers, json=changed
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/chat/sessions/{sid}/messages",
            headers={**auth, "Idempotency-Key": "other"},
            json={"text": "另一个请求", "client_message_id": "other"},
        ).json()["code"]
        == "ACTIVE_RUN_EXISTS"
    )
    assert worker.process_one()
    assert worker.process_one()
    # A new worker has no in-memory graph state, and must continue without repeating tax creation.
    resumed = Worker(app.state.settings, app.state.session_factory, app.state.storage, model=model)
    current = pump(app, client, auth, resumed, rid)
    assert current["usage"]["tool_calls"] == 1
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(TaxCalculation)) == 1
        assert db.scalar(select(func.count()).select_from(ChatCheckpoint)) > 0
        assert db.scalar(select(func.count()).select_from(AgentStep)) == 1
    items = client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=auth).json()["data"][
        "items"
    ]
    assert items[0]["text"] == "税额为13元"
    assert items[0]["cards"][0]["data"]["tax_amount"] == "13.00"
    url = f"/api/v1/chat/sessions/{sid}/events?wait_seconds=0&limit=2"
    events = []
    sequence = 0
    while True:
        page = client.get(url + f"&after_sequence={sequence}", headers=auth).json()["data"]
        events.extend(page["items"])
        sequence = page["next_sequence"]
        if not page["has_more"]:
            break
    assert [v["sequence"] for v in events] == list(range(1, sequence + 1))
    assert events[-1]["type"] == "run.completed"
    assert (
        client.get(url + f"&after_sequence={sequence}", headers=auth).json()["data"]["items"] == []
    )
    assert client.get(url + "&after_sequence=9999", headers=auth).status_code == 400
    # New idempotency key with same client_message_id remains deduplicated forever.
    assert (
        client.post(
            f"/api/v1/chat/sessions/{sid}/messages",
            headers={**auth, "Idempotency-Key": "again"},
            json=body,
        ).json()["data"]["run_id"]
        == rid
    )


def test_input_tax_rate_resume_and_validation(app, client, auth, worker):
    sid, rid, model, _, _ = start(
        app,
        client,
        auth,
        worker,
        [
            call("request_input", {"prompt": "请选择税率", "kind": "tax_rate"}),
            call("calculate_tax", TAX),
            text(),
        ],
    )
    current = pump(app, client, auth, worker, rid, "waiting_input")
    action = current["action"]
    assert action["input_schema"]["properties"]["tax_rate"]["enum"] == [
        "0.01",
        "0.03",
        "0.06",
        "0.09",
        "0.13",
    ]
    assert submit(client, auth, action).status_code == 422
    assert submit(client, auth, action, "submit", values={"tax_rate": "0.99"}).status_code == 422
    assert (
        submit(
            client, auth, action, "submit", values={"tax_rate": "0.13", "user_id": "other"}
        ).status_code
        == 422
    )
    assert submit(client, auth, action, "submit", values={"tax_rate": "0.13"}).status_code == 202
    assert submit(client, auth, action, "submit", values={"tax_rate": "0.13"}).status_code == 202
    assert submit(client, auth, action, "cancel").status_code == 409
    pump(app, client, auth, worker, rid)
    assert any('"tax_rate": "0.13"' in msg.get("content", "") for msg in model.messages[-1])


def test_input_cancel_and_expiry(app, client, auth, worker):
    _, rid, _, _, _ = start(
        app, client, auth, worker, [call("request_input", {"prompt": "需要金额"})]
    )
    action = pump(app, client, auth, worker, rid, "waiting_input")["action"]
    assert submit(client, auth, action, "cancel").json()["data"]["status"] == "cancelled"
    _, rid, _, _, _ = start(
        app, client, auth, worker, [call("request_input", {"prompt": "需要金额"})]
    )
    action = pump(app, client, auth, worker, rid, "waiting_input")["action"]
    with app.state.session_factory.begin() as db:
        db.get(ChatAction, action["id"]).expires_at = now() - timedelta(seconds=1)
    assert submit(client, auth, action, "submit", values={"text": "113"}).status_code == 410
    assert run(client, auth, rid)["status"] == "failed"
    with app.state.session_factory() as db:
        assert db.get(ChatAction, action["id"]).status == "expired"


def test_profile_confirmation_stale_and_once(app, client, auth, worker):
    profile = client.get("/api/v1/users/me", headers=auth).json()["data"]
    _, rid, _, _, _ = start(
        app,
        client,
        auth,
        worker,
        [
            call(
                "update_profile", {"display_name": "新名字", "expected_version": profile["version"]}
            ),
            text(),
        ],
    )
    action = pump(app, client, auth, worker, rid, "waiting_confirmation")["action"]
    assert client.get("/api/v1/users/me", headers=auth).json()["data"]["display_name"] != "新名字"
    client.patch(
        "/api/v1/users/me",
        headers=auth,
        json={"display_name": "页面修改", "expected_version": profile["version"]},
    )
    stale = submit(client, auth, action)
    assert stale.status_code == 409, stale.text
    replacement = stale.json()["data"]["action"]
    assert replacement["id"] != action["id"]
    assert submit(client, auth, replacement, values={"display_name": "恶意替换"}).status_code == 422
    assert submit(client, auth, replacement).status_code == 202
    assert submit(client, auth, replacement).status_code == 202
    pump(app, client, auth, worker, rid)
    assert client.get("/api/v1/users/me", headers=auth).json()["data"]["display_name"] == "新名字"
    with app.state.session_factory() as db:
        assert db.get(User, profile["id"]).version == profile["version"] + 2


def test_share_confirmation_encrypted_result_and_revocation(app, client, auth, worker):
    create_tax(client, auth)
    export, _, _ = create_export(client, auth)
    finished(client, auth, worker, export)
    sid, rid, model, _, _ = start(
        app,
        client,
        auth,
        worker,
        [
            call("create_export_share", {"resource_id": export["id"], "expires_in_days": 1}),
            text("分享已创建"),
        ],
    )
    action = pump(app, client, auth, worker, rid, "waiting_confirmation")["action"]
    assert "任何持有链接" in action["summary"]["access_scope"]
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ExportShare)) == 0
    assert submit(client, auth, action).status_code == 202
    pump(app, client, auth, worker, rid)
    url = "/api/v1/chat/actions/" + action["id"] + "/result"
    result = client.get(url, headers=auth).json()["data"]
    assert result["expires_at"] <= action["summary"]["expires_at"]
    token = result["token"]
    final_message = client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=auth).json()[
        "data"
    ]["items"][0]
    assert all(
        card["data"]["status"] == "consumed"
        for card in final_message["cards"]
        if card["type"] == "confirmation"
    )
    assert client.get("/api/v1/shares/" + token).status_code == 200
    assert token not in json.dumps(model.messages)
    with app.state.session_factory() as db:
        rows = list(db.scalars(select(ChatCheckpoint)))
        assert token not in json.dumps([r.checkpoint for r in rows])
        assert token not in json.dumps([r.payload for r in db.scalars(select(ChatEvent))])
        assert token not in db.get(ChatAction, action["id"]).sealed_result
        assert db.scalar(select(func.count()).select_from(ExportShare)) == 1
    client.delete("/api/v1/exports/" + export["id"] + "/share", headers=auth)
    assert client.get(url, headers=auth).status_code == 404


def test_confirmation_cancel_continues(app, client, auth, worker):
    profile = client.get("/api/v1/users/me", headers=auth).json()["data"]
    _, rid, _, _, _ = start(
        app,
        client,
        auth,
        worker,
        [
            call(
                "update_profile",
                {"display_name": "不应生效", "expected_version": profile["version"]},
            ),
            text("已取消修改"),
        ],
    )
    action = pump(app, client, auth, worker, rid, "waiting_confirmation")["action"]
    assert submit(client, auth, action, "cancel").status_code == 202
    pump(app, client, auth, worker, rid)
    assert (
        client.get("/api/v1/users/me", headers=auth).json()["data"]["display_name"]
        == profile["display_name"]
    )


def test_chat_export_wait_then_share(app, client, auth, worker):
    tax = create_tax(client, auth)

    def share_after_export(messages):
        value = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
        assert value["status"] == "succeeded"
        return call("create_export_share", {"resource_id": value["id"], "expires_in_days": 1})

    _, rid, _, _, _ = start(
        app,
        client,
        auth,
        worker,
        [
            call(
                "create_export",
                {
                    "format": "excel",
                    "source_type": "tax_calculations",
                    "selection": {"mode": "ids", "ids": [tax["id"]]},
                },
            ),
            share_after_export,
            text(),
        ],
    )
    current = pump(app, client, auth, worker, rid, "waiting_task")
    assert current["linked_resources"][0]["type"] == "export"
    action = pump(app, client, auth, worker, rid, "waiting_confirmation")["action"]
    assert submit(client, auth, action).status_code == 202
    pump(app, client, auth, worker, rid)


def test_cancel_in_flight_model_blocks_side_effect(app, client, auth, worker):
    _, rid, _, _, _ = start(app, client, auth, worker, [call("calculate_tax", TAX)])
    job_id, token = worker.claim()
    from app.agent.engine import perform

    with app.state.session_factory() as db:
        result = perform(db, db.get(Job, job_id), worker.settings, worker.model)
    assert (
        client.post("/api/v1/chat/runs/" + rid + "/cancel", headers=auth, json={}).json()["data"][
            "status"
        ]
        == "cancelled"
    )
    worker.finish(job_id, token, result, None)
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(TaxCalculation)) == 0
        assert db.scalar(select(func.count()).select_from(ChatCheckpoint)) == 0


def test_user_isolation_delete_and_privacy(app, client, auth, worker):
    sid, rid, _, _, _ = start(app, client, auth, worker, [call("calculate_tax", TAX), text()])
    pump(app, client, auth, worker, rid)
    bob = client.post("/api/v1/auth/dev-login", json={"account": "bob"}).json()["data"]
    other = {"Authorization": "Bearer " + bob["access_token"]}
    for path in [
        f"sessions/{sid}",
        f"sessions/{sid}/messages",
        f"sessions/{sid}/events?wait_seconds=0",
        f"runs/{rid}",
    ]:
        assert client.get("/api/v1/chat/" + path, headers=other).status_code == 404
    assert client.delete("/api/v1/chat/sessions/" + sid, headers=other).status_code == 404
    assert client.delete("/api/v1/chat/sessions/" + sid, headers=auth).status_code == 200
    assert client.delete("/api/v1/chat/sessions/" + sid, headers=auth).status_code == 200
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ChatCheckpoint)) == 0
        assert db.scalar(select(func.count()).select_from(ChatMessage)) == 0
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert db.scalar(select(func.count()).select_from(TaxCalculation)) == 1
    prefs = client.get("/api/v1/users/me/privacy-settings", headers=auth).json()["data"]
    client.patch(
        "/api/v1/users/me/privacy-settings",
        headers=auth,
        json={"retain_chat_history": False, "expected_version": prefs["version"]},
    )
    ephemeral = session(client, auth)
    assert ephemeral["history_policy"] == "ephemeral" and ephemeral["expires_at"]
    with app.state.session_factory.begin() as db:
        db.get(ChatSession, ephemeral["id"]).expires_at = now() - timedelta(seconds=1)
    worker.process_one()
    with app.state.session_factory() as db:
        assert db.get(ChatSession, ephemeral["id"]).deleted_at


def test_tool_errors_do_not_authorize_arbitrary_calls(app, client, auth, worker):
    replies = [
        call("run_sql", {"sql": "select * from users"}),
        call("get_tax_config", {"user_id": "bob"}),
        text("参数有误"),
    ]
    _, rid, model, _, _ = start(app, client, auth, worker, replies)
    pump(app, client, auth, worker, rid)
    assert "TOOL_NOT_ALLOWED" in json.dumps(model.messages)
    assert "VALIDATION_ERROR" in json.dumps(model.messages)
    user_id = client.get("/api/v1/users/me", headers=auth).json()["data"]["id"]
    with app.state.session_factory.begin() as db:
        ctx = ToolContext(db, user_id, "unguarded", settings=app.state.settings)
        with pytest.raises(BusinessError) as exc:
            build_registry().execute("create_export_share", {"resource_id": "any"}, ctx)
        assert exc.value.code == "CONFIRMATION_REQUIRED"


def test_model_failure_retry_and_limits(app, client, auth, worker):
    _, rid, _, _, _ = start(
        app,
        client,
        auth,
        worker,
        [BusinessError("MODEL_UNAVAILABLE", "稍后重试", 503, retryable=True), text()],
    )
    assert worker.process_one()
    with app.state.session_factory.begin() as db:
        db.scalar(select(Job).where(Job.resource_id == rid)).available_at = now()
    pump(app, client, auth, worker, rid)
    worker.settings.agent_max_model_calls = 1
    _, rid, _, _, _ = start(app, client, auth, worker, [call("get_tax_config", {})])
    assert worker.process_one()
    assert worker.process_one()
    assert worker.process_one()
    assert run(client, auth, rid)["error"]["code"] == "AGENT_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "name,args",
    [
        ("list_tax_calculations", {}),
        ("list_invoice_batches", {}),
        ("list_invoices", {}),
        ("list_exports", {}),
        ("list_records", {}),
        ("get_profile", {}),
        ("list_tools", {}),
        ("list_my_tools", {}),
        ("get_security", {}),
        ("get_notification_settings", {}),
        ("get_privacy_settings", {}),
        ("get_capabilities", {}),
        ("get_help_categories", {}),
        ("list_help_articles", {}),
        ("get_help_article", {"slug": "tax-rounding"}),
        ("get_about", {}),
        ("get_invoice_tips", {}),
    ],
)
def test_catalog_reads_use_real_handlers(app, client, auth, name, args):
    user_id = client.get("/api/v1/users/me", headers=auth).json()["data"]["id"]
    with app.state.session_factory.begin() as db:
        value = build_registry().execute(
            name,
            args,
            ToolContext(db, user_id, "read", app.state.ocr, app.state.settings, app.state.storage),
        )
        assert isinstance(value, dict)


def test_http_model_adapter_validates_and_hides_errors(app):
    settings = app.state.settings.model_copy(
        update={"model_base_url": "https://model.test/v1", "model_name": "configured"}
    )
    seen = []

    def responder(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "one",
                                    "function": {"name": "get_tax_config", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 3},
            },
        )

    provider = ChatCompletionsProvider(settings, transport=httpx.MockTransport(responder))
    reply = provider.respond([{"role": "user", "content": "税率"}], build_registry().definitions())
    assert reply.tool_calls[0].name == "get_tax_config" and reply.input_tokens == 9
    assert seen[0]["tools"][0]["type"] == "function"
    for response, code, retryable in [
        (httpx.Response(429, text="secret"), "MODEL_UNAVAILABLE", True),
        (httpx.Response(401, text="secret"), "MODEL_CONFIGURATION_ERROR", False),
        (httpx.Response(200, json={"choices": []}), "MODEL_INVALID_RESPONSE", False),
    ]:
        broken = ChatCompletionsProvider(
            settings, transport=httpx.MockTransport(lambda request, response=response: response)
        )
        with pytest.raises(BusinessError) as exc:
            broken.respond([], [])
        assert exc.value.code == code and exc.value.retryable == retryable
        assert "secret" not in exc.value.message


def test_confirmed_scope_changes_before_execution_requires_new_action(app, client, auth, worker):
    profile = client.get("/api/v1/users/me", headers=auth).json()["data"]
    _, rid, _, _, _ = start(
        app,
        client,
        auth,
        worker,
        [
            call(
                "update_profile",
                {"display_name": "预定名称", "expected_version": profile["version"]},
            ),
            text(),
        ],
    )
    action = pump(app, client, auth, worker, rid, "waiting_confirmation")["action"]
    assert submit(client, auth, action).status_code == 202
    client.patch(
        "/api/v1/users/me",
        headers=auth,
        json={"display_name": "并发修改", "expected_version": profile["version"]},
    )
    assert worker.process_one()
    current = run(client, auth, rid)
    assert current["status"] == "waiting_confirmation", current
    assert current["action"]["id"] != action["id"]
    assert client.get("/api/v1/users/me", headers=auth).json()["data"]["display_name"] == "并发修改"
    assert submit(client, auth, current["action"]).status_code == 202
    pump(app, client, auth, worker, rid)
    assert client.get("/api/v1/users/me", headers=auth).json()["data"]["display_name"] == "预定名称"


def test_tool_checkpoint_and_business_rollback_are_atomic(app, client, auth, worker, monkeypatch):
    from app.agent.checkpoints import TransactionSaver

    _, rid, model, _, _ = start(app, client, auth, worker, [call("calculate_tax", TAX), text()])
    assert worker.process_one()  # Persist model plan.
    original = TransactionSaver.flush

    def fail_flush(self, db):
        raise RuntimeError("simulated process failure before commit")

    monkeypatch.setattr(TransactionSaver, "flush", fail_flush)
    with pytest.raises(RuntimeError):
        worker.process_one()
    with app.state.session_factory.begin() as db:
        assert db.scalar(select(func.count()).select_from(TaxCalculation)) == 0
        assert db.scalar(select(func.count()).select_from(AgentStep)) == 0
        job = db.scalar(select(Job).where(Job.resource_id == rid, Job.status == "processing"))
        job.lease_until = now() - timedelta(seconds=1)
    monkeypatch.setattr(TransactionSaver, "flush", original)
    recovered = Worker(
        app.state.settings, app.state.session_factory, app.state.storage, model=model
    )
    pump(app, client, auth, recovered, rid)
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(TaxCalculation)) == 1
        assert db.scalar(select(func.count()).select_from(AgentStep)) == 1


def test_expired_worker_cannot_save_model_reply(app, client, auth, worker):
    from app.agent.engine import perform

    _, rid, _, _, _ = start(
        app, client, auth, worker, [call("calculate_tax", TAX), call("calculate_tax", TAX), text()]
    )
    job_id, token = worker.claim()
    with app.state.session_factory() as db:
        reply = perform(db, db.get(Job, job_id), worker.settings, worker.model)
    with app.state.session_factory.begin() as db:
        db.get(Job, job_id).lease_until = now() - timedelta(seconds=1)
    worker.finish(job_id, token, reply, None)
    with app.state.session_factory() as db:
        assert db.get(AgentRun, rid).model_calls == 0
        assert db.scalar(select(func.count()).select_from(ChatCheckpoint)) == 0
    pump(app, client, auth, worker, rid)
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(TaxCalculation)) == 1


def test_action_watchdog_expires_without_client_submit(app, client, auth, worker):
    _, rid, _, _, _ = start(
        app, client, auth, worker, [call("request_input", {"prompt": "提供金额"})]
    )
    action = pump(app, client, auth, worker, rid, "waiting_input")["action"]
    with app.state.session_factory.begin() as db:
        db.get(ChatAction, action["id"]).expires_at = now() - timedelta(seconds=1)
        db.scalar(
            select(Job).where(Job.resource_id == rid, Job.status == "queued")
        ).available_at = now()
    assert worker.process_one()
    assert run(client, auth, rid)["status"] == "failed"
    assert submit(client, auth, action, "submit", values={"text": "113"}).status_code == 410


@pytest.mark.parametrize("cancel_tasks", [False, True])
def test_run_cancellation_has_explicit_business_task_scope(app, client, auth, worker, cancel_tasks):
    tax = create_tax(client, auth)
    _, rid, _, _, _ = start(
        app,
        client,
        auth,
        worker,
        [
            call(
                "create_export",
                {
                    "format": "excel",
                    "source_type": "tax_calculations",
                    "selection": {"mode": "ids", "ids": [tax["id"]]},
                },
            )
        ],
    )
    current = pump(app, client, auth, worker, rid, "waiting_task")
    export_id = current["linked_resources"][0]["id"]
    response = client.post(
        "/api/v1/chat/runs/" + rid + "/cancel",
        headers=auth,
        json={"cancel_linked_tasks": cancel_tasks},
    )
    assert response.status_code == 200
    value = client.get("/api/v1/exports/" + export_id, headers=auth).json()["data"]
    assert value["status"] == ("cancelled" if cancel_tasks else "queued")
    assert bool(response.json()["data"]["cancelled_linked_resources"]) == cancel_tasks


def test_wait_polls_do_not_grow_checkpoints_or_jobs(app, client, auth, worker):
    tax = create_tax(client, auth)
    _, rid, _, _, _ = start(
        app,
        client,
        auth,
        worker,
        [
            call(
                "create_export",
                {
                    "format": "excel",
                    "source_type": "tax_calculations",
                    "selection": {"mode": "ids", "ids": [tax["id"]]},
                },
            )
        ],
    )
    pump(app, client, auth, worker, rid, "waiting_task")
    with app.state.session_factory.begin() as db:
        # Simulate a slow separate export worker, while the chat worker polls.
        export_job = db.scalar(select(Job).where(Job.type == "generate_export"))
        export_job.available_at = now() + timedelta(hours=1)
        checkpoints = db.scalar(select(func.count()).select_from(ChatCheckpoint))
        jobs = db.scalar(select(func.count()).select_from(Job))
    for _ in range(5):
        with app.state.session_factory.begin() as db:
            db.scalar(
                select(Job).where(Job.resource_id == rid, Job.status == "queued")
            ).available_at = now()
        assert worker.process_one()
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ChatCheckpoint)) == checkpoints
        assert db.scalar(select(func.count()).select_from(Job)) == jobs
        assert (
            db.scalar(select(Job).where(Job.resource_id == rid, Job.status == "queued")).attempt
            == 0
        )


def test_multi_call_plan_continues_after_input(app, client, auth, worker):
    plan = ModelReply(
        "",
        [
            ToolCall("input", "request_input", {"prompt": "请提供说明"}),
            ToolCall("config", "get_tax_config", {}),
        ],
        1,
        1,
    )
    _, rid, model, _, _ = start(app, client, auth, worker, [plan, text()])
    action = pump(app, client, auth, worker, rid, "waiting_input")["action"]
    submit(client, auth, action, "submit", values={"text": "继续"})
    pump(app, client, auth, worker, rid)
    results = [m["tool_call_id"] for m in model.messages[-1] if m["role"] == "tool"]
    assert results == ["input", "config"]


def test_context_and_candidates_never_grant_foreign_access(app, client, auth, worker):
    tax = create_tax(client, auth)
    bob = client.post("/api/v1/auth/dev-login", json={"account": "bob"}).json()["data"]
    other = {"Authorization": "Bearer " + bob["access_token"]}
    app.state.model = worker.model = ScriptModel([text()])
    sid = session(client, other)["id"]
    response = client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        headers={**other, "Idempotency-Key": "foreign"},
        json={
            "client_message_id": "foreign",
            "text": "查询",
            "context": {"tax_calculation_id": tax["id"]},
        },
    )
    assert response.status_code == 404
    _, rid, _, _, _ = start(
        app,
        client,
        other,
        worker,
        [
            call(
                "request_input",
                {
                    "prompt": "选一个",
                    "kind": "choice",
                    "choices": [
                        {"type": "tax_calculation", "id": tax["id"], "label": "别人的记录"}
                    ],
                },
            ),
            text(),
        ],
    )
    pump(app, client, other, worker, rid)
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ChatAction)) == 0


def test_delete_blocks_idempotent_message_replay(app, client, auth, worker):
    sid, rid, _, headers, body = start(app, client, auth, worker, [text()])
    client.delete("/api/v1/chat/sessions/" + sid, headers=auth)
    assert (
        client.post(f"/api/v1/chat/sessions/{sid}/messages", headers=headers, json=body).status_code
        == 404
    )
    assert worker.process_one()
    with app.state.session_factory() as db:
        assert db.get(AgentRun, rid) is None


def test_invoice_chat_wait_and_model_data_minimization(app, client, auth):
    from tests.test_invoices import drain, setup_batch

    batch, worker, file = setup_batch(client, auth, app, pages=2)
    drain(worker)

    def query_invoice(messages):
        result = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
        assert result["status"] == "succeeded" and result["invoice_count"] == 2
        return call("list_invoices", {"batch_id": result["id"]})

    def detail(messages):
        result = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
        return call("get_invoice", {"resource_id": result["items"][0]["id"]})

    _, rid, model, _, _ = start(
        app,
        client,
        auth,
        worker,
        [
            call("create_invoice_batch", {"file_ids": [file["file_id"]], "source": "chat"}),
            query_invoice,
            detail,
            text(),
        ],
        file_ids=[file["file_id"]],
    )
    pump(app, client, auth, worker, rid)
    content = json.dumps(model.messages, ensure_ascii=False)
    assert "TAX1" not in content and "TAX2" not in content
    assert "provider-request" not in content and "购买方" not in content
    assert "113.00" in content


def test_file_picker_action_accepts_only_ready_owned_files(app, client, auth):
    from tests.test_invoices import drain, setup_batch

    _, worker, file = setup_batch(client, auth, app)
    drain(worker)
    _, rid, _, _, _ = start(
        app,
        client,
        auth,
        worker,
        [call("request_input", {"prompt": "上传文件", "kind": "file_ids"}), text()],
    )
    action = pump(app, client, auth, worker, rid, "waiting_input")["action"]
    assert (
        submit(client, auth, action, "submit", values={"file_ids": ["missing"]}).status_code == 404
    )
    assert (
        submit(
            client, auth, action, "submit", values={"file_ids": [file["file_id"], file["file_id"]]}
        ).status_code
        == 422
    )
    assert (
        submit(client, auth, action, "submit", values={"file_ids": [file["file_id"]]}).status_code
        == 202
    )
    pump(app, client, auth, worker, rid)


def test_model_usage_budget_stops_plan_before_side_effects(app, client, auth, worker):
    worker.settings.agent_max_total_tokens = 1000
    response = ModelReply("", [ToolCall("budget", "calculate_tax", TAX)], 1000, 100)
    _, rid, _, _, _ = start(app, client, auth, worker, [response])
    assert worker.process_one()
    current = run(client, auth, rid)
    assert current["status"] == "failed" and current["usage"]["input_tokens"] == 1000
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(TaxCalculation)) == 0


def test_share_tool_defaults_are_in_prepared_scope(app, client, auth, worker):
    create_tax(client, auth)
    export, _, _ = create_export(client, auth)
    finished(client, auth, worker, export)
    _, rid, _, _, _ = start(
        app,
        client,
        auth,
        worker,
        [call("create_export_share", {"resource_id": export["id"]}), text()],
    )
    action = pump(app, client, auth, worker, rid, "waiting_confirmation")["action"]
    assert action["summary"]["parameters"]["expires_in_days"] == 7
    assert submit(client, auth, action).status_code == 202
    pump(app, client, auth, worker, rid)
