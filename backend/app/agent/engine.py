"""One bounded graph tick per leased job. External model IO occurs before apply.

Business writes, run projection, steps, events and graph checkpoints commit together.
Model calls can be retried; successful side effects are never replayed after commit.
"""

import json
from copy import deepcopy
from dataclasses import asdict
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from sqlalchemy import select

from app.agent.checkpoints import TransactionSaver
from app.agent.tools import ToolContext, build_registry
from app.core.errors import BusinessError
from app.core.security import now, timestamp
from app.db.models import (
    AgentRun,
    AgentStep,
    ChatAction,
    ChatMessage,
    ChatSession,
    ExportShare,
    IdempotencyRecord,
)
from app.schemas.chat import AskInput
from app.services import chat


class Tick(TypedDict):
    payload: dict
    reply: dict | None


def perform(db, job, settings, model):
    run = db.get(AgentRun, job.resource_id)
    if run is None or run.status in chat.TERMINAL or job.dedupe_key != f"chat:{run.id}:{run.tick}":
        return None
    state = deepcopy(run.state)
    if state["phase"] != "model":
        return None
    if (
        run.model_calls >= settings.agent_max_model_calls
        or run.deadline_at <= now()
        or run.input_tokens + run.output_tokens >= settings.agent_max_total_tokens
    ):
        raise BusinessError("AGENT_LIMIT_EXCEEDED", "助手执行已达到限制，请拆分请求")
    if len(json.dumps(state["messages"], ensure_ascii=False)) > settings.agent_max_context_chars:
        raise BusinessError("AGENT_CONTEXT_LIMIT", "本次对话内容过多，请开启新对话")
    # Release read transactions before network IO, especially important for SQLite.
    db.rollback()
    return asdict(model.respond(state["messages"], build_registry().definitions()))


def apply(db, job, result, error, retry, settings, storage, ocr):
    run = db.get(AgentRun, job.resource_id)
    if run is None:
        return
    session = db.scalar(
        select(ChatSession).where(ChatSession.id == run.session_id).with_for_update()
    )
    db.refresh(run, with_for_update=True)
    if (
        session.deleted_at
        or run.status in chat.TERMINAL
        or job.dedupe_key != f"chat:{run.id}:{run.tick}"
    ):
        return
    if error:
        if not retry:
            chat.complete(db, session, run, "failed", error)
        return
    if run.deadline_at <= now():
        chat.complete(
            db,
            session,
            run,
            "failed",
            {"code": "AGENT_LIMIT_EXCEEDED", "message": "助手请求已超时，请重新发起"},
        )
        return
    registry = build_registry()
    state = deepcopy(run.state)
    if state["phase"] == "wait":
        from datetime import timedelta

        ref = state["waiting"]
        name = "get_export" if ref["kind"] == "export" else "get_invoice_batch"
        value = registry.execute(
            name,
            {"resource_id": ref["id"]},
            ToolContext(db, run.user_id, "wait", ocr, settings, storage),
        )
        if value["status"] in {"queued", "processing", "cancelling"} and chat.sanitize(
            value
        ) == ref.get("snapshot"):
            job.status, job.attempt, job.available_at = "queued", 0, now() + timedelta(seconds=5)
            return
    if state["phase"] not in {"action", "wait"}:
        chat.status(db, session, run, "running", "助手正在处理")

    def remember(call, value):
        db.add(
            AgentStep(
                run_id=run.id,
                position=run.tool_calls,
                tool_name=call["name"],
                arguments=call["arguments"],
                result=chat.sanitize(value),
            )
        )
        db.flush()

    def model_node(tick):
        value = tick["reply"]
        if value is None:
            raise BusinessError("MODEL_INVALID_RESPONSE", "模型结果缺失")
        calls = value["tool_calls"]
        if len(calls) > 20 or len({v["id"] for v in calls}) != len(calls):
            raise BusinessError("MODEL_INVALID_RESPONSE", "模型工具调用格式无效")
        run.model_calls += 1
        run.input_tokens += value["input_tokens"]
        run.output_tokens += value["output_tokens"]
        if run.input_tokens + run.output_tokens > settings.agent_max_total_tokens:
            raise BusinessError("AGENT_LIMIT_EXCEEDED", "助手请求已达到用量限制")
        assistant = {"role": "assistant", "content": value["text"]}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": v["id"],
                    "type": "function",
                    "function": {
                        "name": v["name"],
                        "arguments": json.dumps(v["arguments"], ensure_ascii=False),
                    },
                }
                for v in calls
            ]
            state["calls"], state["index"], state["phase"] = calls, 0, "tools"
        else:
            msg = ChatMessage(
                session_id=session.id,
                run_id=run.id,
                role="assistant",
                text=value["text"],
                cards=state["cards"],
            )
            db.add(msg)
            db.flush()
            chat.emit(db, session, run, "message.created", chat.message_out(msg))
            state["phase"] = "done"
            chat.complete(db, session, run)
        state["messages"].append(assistant)
        return {"payload": state, "reply": None}

    def tools_node(tick):
        call = state["calls"][state["index"]]
        if run.tool_calls >= settings.agent_max_tool_calls:
            raise BusinessError("AGENT_LIMIT_EXCEEDED", "工具调用次数已达到限制")
        tool = registry.tools.get(call["name"])
        # Validate before preparing actions; model parameters never define confirmation schemas.
        value = None
        try:
            if tool is None:
                raise BusinessError("TOOL_NOT_ALLOWED", "工具不存在或未启用", 403)
            from pydantic import ValidationError

            try:
                body = tool.schema.model_validate(call["arguments"])
            except ValidationError as exc:
                raise BusinessError("VALIDATION_ERROR", "工具参数无效") from exc
            if call["name"] == "create_export_share":
                call["arguments"].setdefault("expires_in_days", body.expires_in_days)
            if call["name"] == "request_input":
                chat.prepare_action(
                    db,
                    settings,
                    session,
                    run,
                    state,
                    call,
                    input_body=AskInput.model_validate(call["arguments"]),
                )
                run.tool_calls += 1
                remember(call, {"waiting_input": True})
                return {"payload": state}
            sensitive = tool.requires_confirmation or (
                call["name"] == "show_ui_action" and body.name == "logout"
            )
            action = (
                db.get(ChatAction, state.get("approved_action"))
                if state.get("approved_action")
                else None
            )
            if sensitive:
                if action is None:
                    chat.prepare_action(db, settings, session, run, state, call)
                    return {"payload": state}
                if (
                    action.run_id != run.id
                    or action.submission != {"decision": "confirm", "values": {}}
                    or action.prepared["call"] != call
                ):
                    raise BusinessError("ACTION_UNAVAILABLE", "确认范围不匹配", 409)
                if action.expires_at <= now():
                    raise BusinessError("ACTION_EXPIRED", "确认已过期，请重新准备", 410)
                refreshed = chat.refresh_call(db, run.user_id, call)
                summary, version = chat.prepared_summary(
                    db, run.user_id, refreshed["name"], refreshed["arguments"]
                )
                if "expires_at" in summary:
                    summary["expires_at"] = action.summary["expires_at"]
                if (
                    version != action.prepared["version"]
                    or chat.digest(summary) != action.prepared["fingerprint"]
                ):
                    state.pop("approved_action", None)
                    state["calls"][state["index"]] = refreshed
                    chat.prepare_action(db, settings, session, run, state, refreshed)
                    return {"payload": state}
            ctx = ToolContext(
                db,
                run.user_id,
                f"agent:{run.id}:tool:{run.tool_calls}",
                ocr,
                settings,
                storage,
                approved=bool(action),
            )
            # Roll back partially executed tools on business errors, while retaining graph progress.
            with db.begin_nested():
                value = registry.execute(call["name"], call["arguments"], ctx)
                if call["name"] == "create_export_share":
                    # The approved absolute expiry is an upper bound, even if execution is delayed.
                    from datetime import datetime

                    share = db.get(ExportShare, value["id"])
                    expiry = datetime.fromisoformat(
                        action.summary["expires_at"].replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    share.expires_at = min(share.expires_at, expiry)
                    value["expires_at"] = timestamp(share.expires_at)
                    replay = db.scalar(
                        select(IdempotencyRecord).where(
                            IdempotencyRecord.user_id == run.user_id,
                            IdempotencyRecord.operation
                            == "share.create:" + call["arguments"]["resource_id"],
                            IdempotencyRecord.key == ctx.operation_key,
                        )
                    )
                    replay.response_data = {
                        **replay.response_data,
                        "expires_at": value["expires_at"],
                    }
                    action.sealed_result = chat.seal(settings, value)
                    chat.resolve_action_card(db, session, run, state, action)
                    value = {
                        **chat.sanitize(value),
                        "action_id": action.id,
                        "result_available": True,
                    }
        except BusinessError as exc:
            value = {
                "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
            }
        run.tool_calls += 1
        remember(call, value)
        kind = {
            "calculate_tax": "tax_calculation",
            "get_tax_calculation": "tax_calculation",
            "create_invoice_batch": "invoice_batch",
            "retry_invoice_batch": "invoice_batch",
            "get_invoice_batch": "invoice_batch",
            "get_invoice": "invoice",
            "patch_invoice": "invoice",
            "confirm_invoice": "invoice",
            "create_export": "export",
            "get_export": "export",
            "create_export_share": "share",
            "show_ui_action": "ui_action",
        }.get(call["name"])
        if kind and not value.get("error"):
            resource_id = value.get("id") if kind != "ui_action" else value.get("resource_id")
            chat.card(db, session, run, state, kind, resource_id, chat.sanitize(value))
        if kind in {"invoice_batch", "export"} and value.get("status") in {
            "queued",
            "processing",
            "cancelling",
        }:
            state["waiting"] = {
                "kind": kind,
                "id": value["id"],
                "call": call,
                "snapshot": chat.sanitize(value),
            }
            state["phase"] = "wait"
            chat.status(db, session, run, "waiting_task", "等待后台任务完成")
        else:
            chat.tool_result(state, call, value)
        return {"payload": state}

    def wait_node(tick):
        ref = state["waiting"]
        name = "get_export" if ref["kind"] == "export" else "get_invoice_batch"
        value = registry.execute(
            name,
            {"resource_id": ref["id"]},
            ToolContext(db, run.user_id, "wait", ocr, settings, storage),
        )
        chat.card(db, session, run, state, ref["kind"], ref["id"], chat.sanitize(value))
        state["waiting"]["snapshot"] = chat.sanitize(value)
        if value["status"] not in {"queued", "processing", "cancelling"}:
            chat.tool_result(state, ref["call"], value)
            chat.status(db, session, run, "queued", "后台任务已结束，继续处理")
        return {"payload": state}

    def action_node(tick):
        action = db.get(ChatAction, run.action_id)
        if action.expires_at <= now():
            action.status = "expired"
            chat.resolve_action_card(db, session, run, state, action)
            chat.emit(
                db,
                session,
                run,
                "action.resolved",
                {"action_id": action.id, "status": "expired", "decision": "expired"},
            )
            state["phase"] = "done"
            chat.complete(
                db,
                session,
                run,
                "failed",
                {"code": "ACTION_EXPIRED", "message": "操作已过期，请重新发起请求"},
            )
        return {"payload": state}

    # Only one business/model step runs per tick. Waiting does not occupy a worker.
    graph = StateGraph(Tick)
    for name, node in [
        ("model", model_node),
        ("tools", tools_node),
        ("wait", wait_node),
        ("action", action_node),
    ]:
        graph.add_node(name, node)
        graph.add_edge(name, END)
    graph.add_conditional_edges(
        START,
        lambda v: v["payload"]["phase"],
        {name: name for name in ["model", "tools", "wait", "action"]},
    )
    saver = TransactionSaver(db, run.id)
    compiled = graph.compile(checkpointer=saver)
    try:
        # Invoke supplies authoritative action changes too; checkpoint is the durable execution trace.
        # Disable implicit remote trace uploads even if developer machine has tracing env vars.
        with tracing_context(enabled=False):
            output = compiled.invoke(
                {"payload": state, "reply": result},
                {"configurable": {"thread_id": run.id}},
                durability="sync",
            )
        run.state = output["payload"]
        saver.flush(db)
    except BusinessError as exc:
        chat.complete(db, session, run, "failed", {"code": exc.code, "message": exc.message})
        return
    if state["phase"] == "wait" and run.status not in chat.TERMINAL:
        from datetime import timedelta

        job.status, job.attempt, job.available_at = "queued", 0, now() + timedelta(seconds=5)
        return
    run.tick += 1
    if run.status not in chat.TERMINAL:
        delay = (
            5
            if state["phase"] == "wait"
            else max(
                1, int((db.get(ChatAction, run.action_id).expires_at - now()).total_seconds()) + 1
            )
            if state["phase"] == "action"
            else 0
        )
        chat.queue(db, run, delay)


def mark_claim(db, job):
    run = db.get(AgentRun, job.resource_id)
    if run is None or run.status in chat.TERMINAL or job.dedupe_key != f"chat:{run.id}:{run.tick}":
        return
    session = db.scalar(
        select(ChatSession).where(ChatSession.id == run.session_id).with_for_update()
    )
    db.refresh(run, with_for_update=True)
    if not session.deleted_at and run.status == "queued":
        chat.status(db, session, run, "running", "助手正在处理")
