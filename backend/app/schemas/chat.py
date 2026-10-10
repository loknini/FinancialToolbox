from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import Input


class SessionCreate(Input):
    title: str = Field(default="新对话", min_length=1, max_length=100)


class SessionPatch(SessionCreate):
    expected_version: int = Field(ge=1)


class ResourceContext(Input):
    batch_id: str | None = None
    selected_invoice_ids: list[str] = Field(default_factory=list, max_length=100)
    export_id: str | None = None
    tax_calculation_id: str | None = None


class MessageRequest(Input):
    client_message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=10000)
    file_ids: list[str] = Field(default_factory=list, max_length=100)
    context: ResourceContext | None = None

    @model_validator(mode="after")
    def content(self):
        if not self.text and not self.file_ids:
            raise ValueError("消息或附件至少提供一项")
        if len(set(self.file_ids)) != len(self.file_ids):
            raise ValueError("附件不能重复")
        return self


class ActionSubmission(Input):
    decision: Literal["confirm", "submit", "cancel"]
    values: dict = Field(default_factory=dict)


class CancelRequest(Input):
    cancel_linked_tasks: bool = False


class ResourceChoice(Input):
    type: Literal["invoice", "invoice_batch", "export", "tax_calculation", "file"]
    id: str
    label: str = Field(min_length=1, max_length=100)


class AskInput(Input):
    prompt: str = Field(min_length=1, max_length=500)
    kind: Literal["text", "tax_rate", "file_ids", "choice"] = "text"
    choices: list[ResourceChoice] = Field(default_factory=list, max_length=20)


class ShareTool(Input):
    resource_id: str
    expires_in_days: int = Field(default=7, ge=1, le=30)


class UiTool(Input):
    name: Literal[
        "contact_customer_service",
        "open_download",
        "open_share",
        "logout",
        "wechat_authorization",
    ]
    resource_id: str | None = None


class ResourceRefOut(BaseModel):
    type: str
    id: str
    status: str


class CardOut(BaseModel):
    card_id: str
    type: str
    resource_id: str | None
    data: dict


class ActionOut(BaseModel):
    id: str
    run_id: str
    type: Literal["input", "confirmation"]
    status: Literal["pending", "consumed", "expired", "cancelled"]
    prompt: str
    summary: dict
    input_schema: dict | None
    options: list[dict[str, str]]
    expires_at: str
    result_available: bool


class RunOut(BaseModel):
    id: str
    session_id: str
    status: Literal[
        "queued",
        "running",
        "waiting_input",
        "waiting_confirmation",
        "waiting_task",
        "succeeded",
        "failed",
        "cancelled",
    ]
    current_step: str
    linked_resources: list[ResourceRefOut]
    action: ActionOut | None
    error: dict | None
    usage: dict[str, int]
    created_at: str
    updated_at: str


class CancelledRunOut(RunOut):
    cancelled_linked_resources: list[ResourceRefOut]


class SessionOut(BaseModel):
    id: str
    title: str
    version: int
    history_policy: Literal["retained", "ephemeral"]
    expires_at: str | None
    last_sequence: int
    resource_context: dict
    active_run: RunOut | None
    created_at: str
    updated_at: str


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant"]
    client_message_id: str | None
    text: str
    file_ids: list[str]
    cards: list[CardOut]
    created_at: str


class MessageAccepted(BaseModel):
    message_id: str
    run_id: str
    status: str


class EventOut(BaseModel):
    sequence: int
    run_id: str
    type: Literal[
        "run.status",
        "message.created",
        "card.upsert",
        "action.required",
        "action.resolved",
        "run.completed",
        "run.failed",
    ]
    created_at: str
    payload: dict


class EventPage(BaseModel):
    items: list[EventOut]
    next_sequence: int
    has_more: bool
