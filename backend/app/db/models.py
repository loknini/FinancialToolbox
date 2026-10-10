from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.security import now
from app.db.types import ExactNumeric


def new_id():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Entity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class User(Entity, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("appid", "openid"),)
    appid: Mapped[str] = mapped_column(String(128))
    openid: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(64), default="微信用户")
    avatar_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}


class AuthSession(Entity, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_login_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class UserSettings(Entity, Base):
    __tablename__ = "user_settings"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True)
    in_app_task_updates: Mapped[bool] = mapped_column(default=True)
    retain_chat_history: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}


class TaxCalculation(Entity, Base):
    __tablename__ = "tax_calculations"
    __table_args__ = (Index("ix_tax_user_created", "user_id", "created_at", "id"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    gross_amount: Mapped[Decimal] = mapped_column(ExactNumeric(24, 2))
    net_amount: Mapped[Decimal] = mapped_column(ExactNumeric(24, 2))
    tax_amount: Mapped[Decimal] = mapped_column(ExactNumeric(24, 2))
    tax_rate: Mapped[Decimal] = mapped_column(ExactNumeric(10, 6))
    invoice_type_code: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(64))
    rule_version: Mapped[str] = mapped_column(String(64))
    source_invoice_id: Mapped[str | None] = mapped_column(String(36))


class IdempotencyRecord(Entity, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "operation", "key"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    operation: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_data: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class UploadSession(Entity, Base):
    __tablename__ = "upload_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(ExactNumeric(30, 0))
    purpose: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="uploading")
    part_size_bytes: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    file_id: Mapped[str | None] = mapped_column(String(36))
    storage_backend: Mapped[str] = mapped_column(
        String(16), default="local", server_default="local"
    )
    remote_upload_id: Mapped[str | None] = mapped_column(Text)


class UploadPart(Entity, Base):
    __tablename__ = "upload_parts"
    __table_args__ = (UniqueConstraint("session_id", "part_number"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("upload_sessions.id"))
    part_number: Mapped[int] = mapped_column(Integer)
    etag: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)


class File(Entity, Base):
    __tablename__ = "files"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    upload_session_id: Mapped[str] = mapped_column(ForeignKey("upload_sessions.id"), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(ExactNumeric(30, 0))
    status: Mapped[str] = mapped_column(String(24), default="validating")
    object_key: Mapped[str] = mapped_column(String(128), unique=True)
    sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)


class Job(Entity, Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "available_at"),)
    type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(36))
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    available_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[dict | None] = mapped_column(JSON)


class Feedback(Entity, Base):
    __tablename__ = "feedback"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    attachment_file_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="received")


class InvoiceBatch(Entity, Base):
    __tablename__ = "invoice_batches"
    __table_args__ = (Index("ix_batches_user_created", "user_id", "created_at", "id"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(16), default="page")
    status: Mapped[str] = mapped_column(String(24), default="queued")
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}


class BatchFile(Entity, Base):
    __tablename__ = "batch_files"
    __table_args__ = (UniqueConstraint("batch_id", "file_id"),)
    batch_id: Mapped[str] = mapped_column(ForeignKey("invoice_batches.id"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"))
    position: Mapped[int] = mapped_column(Integer)
    total_pages: Mapped[int | None] = mapped_column(Integer)
    discovered_pages: Mapped[int] = mapped_column(Integer, default=0)
    discovery_complete: Mapped[bool] = mapped_column(default=False)
    discovery_error: Mapped[dict | None] = mapped_column(JSON)
    generation: Mapped[int] = mapped_column(Integer, default=0)


class InvoiceUnit(Entity, Base):
    __tablename__ = "invoice_units"
    __table_args__ = (
        UniqueConstraint("batch_file_id", "page_from"),
        Index("ix_units_batch_status", "batch_id", "status"),
    )
    batch_id: Mapped[str] = mapped_column(ForeignKey("invoice_batches.id"))
    batch_file_id: Mapped[str] = mapped_column(ForeignKey("batch_files.id"))
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"))
    page_from: Mapped[int] = mapped_column(Integer)
    page_to: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[dict | None] = mapped_column(JSON)


class Invoice(Entity, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("unit_id", "result_key"),
        Index("ix_invoices_user_created", "user_id", "created_at", "id"),
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    batch_id: Mapped[str] = mapped_column(ForeignKey("invoice_batches.id"), index=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("invoice_units.id"), index=True)
    result_key: Mapped[str] = mapped_column(String(64))
    type_code: Mapped[str] = mapped_column(String(64))
    invoice_number: Mapped[str | None] = mapped_column(String(128))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    seller_name: Mapped[str | None] = mapped_column(String(1024))
    gross_amount: Mapped[Decimal | None] = mapped_column(ExactNumeric(24, 2))
    net_amount: Mapped[Decimal | None] = mapped_column(ExactNumeric(24, 2))
    tax_amount: Mapped[Decimal | None] = mapped_column(ExactNumeric(24, 2))
    data: Mapped[dict] = mapped_column(JSON)
    original_data: Mapped[dict] = mapped_column(JSON)
    review_status: Mapped[str] = mapped_column(String(24), default="pending_review")
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}


class OcrResponse(Entity, Base):
    __tablename__ = "ocr_results"
    __table_args__ = (UniqueConstraint("unit_id", "generation"),)
    unit_id: Mapped[str] = mapped_column(ForeignKey("invoice_units.id"))
    generation: Mapped[int] = mapped_column(Integer)
    provider_request_id: Mapped[str] = mapped_column(String(128))
    raw_response: Mapped[dict] = mapped_column(JSON)


class InvoiceRevision(Entity, Base):
    __tablename__ = "invoice_revisions"
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    before: Mapped[dict] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(1024))
    operation: Mapped[str] = mapped_column(String(32))


class Export(Entity, Base):
    __tablename__ = "exports"
    __table_args__ = (Index("ix_exports_user_created", "user_id", "created_at", "id"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    format: Mapped[str] = mapped_column(String(16))
    source_type: Mapped[str] = mapped_column(String(32))
    request_data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    snapshot_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    record_count: Mapped[int] = mapped_column(Integer)
    pending_review_count: Mapped[int] = mapped_column(Integer)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    error: Mapped[dict | None] = mapped_column(JSON)
    active_share_id: Mapped[str | None] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}


class ExportSnapshot(Entity, Base):
    __tablename__ = "export_snapshots"
    __table_args__ = (UniqueConstraint("export_id", "position"),)
    export_id: Mapped[str] = mapped_column(ForeignKey("exports.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)


class ExportFile(Entity, Base):
    __tablename__ = "export_files"
    export_id: Mapped[str] = mapped_column(ForeignKey("exports.id"), unique=True)
    filename: Mapped[str] = mapped_column(String(128))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(ExactNumeric(30, 0))
    storage_backend: Mapped[str] = mapped_column(String(16))
    object_key: Mapped[str] = mapped_column(String(256), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class ExportShare(Entity, Base):
    __tablename__ = "export_shares"
    export_id: Mapped[str] = mapped_column(ForeignKey("exports.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class ChatSession(Entity, Base):
    __tablename__ = "chat_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(100), default="新对话")
    history_policy: Mapped[str] = mapped_column(String(24), default="retained")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    active_run_id: Mapped[str | None] = mapped_column(String(36))
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    resource_context: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}


class AgentRun(Entity, Base):
    __tablename__ = "agent_runs"
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    current_step: Mapped[str] = mapped_column(String(100), default="等待助手处理")
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    linked_resources: Mapped[list] = mapped_column(JSON, default=list)
    action_id: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[dict | None] = mapped_column(JSON)
    tick: Mapped[int] = mapped_column(Integer, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    deadline_at: Mapped[datetime] = mapped_column(DateTime)


class ChatMessage(Entity, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("session_id", "client_message_id"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    role: Mapped[str] = mapped_column(String(16))
    client_message_id: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    file_ids: Mapped[list] = mapped_column(JSON, default=list)
    cards: Mapped[list] = mapped_column(JSON, default=list)


class AgentStep(Entity, Base):
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("run_id", "position"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)


class ChatAction(Entity, Base):
    __tablename__ = "chat_actions"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    type: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    prompt: Mapped[str] = mapped_column(Text)
    summary: Mapped[dict] = mapped_column(JSON)
    input_schema: Mapped[dict | None] = mapped_column(JSON)
    options: Mapped[list] = mapped_column(JSON, default=list)
    prepared: Mapped[dict] = mapped_column(JSON)
    submission: Mapped[dict | None] = mapped_column(JSON)
    sealed_result: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class ChatEvent(Entity, Base):
    __tablename__ = "chat_events"
    __table_args__ = (UniqueConstraint("session_id", "sequence"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)


class ChatCheckpoint(Base):
    __tablename__ = "chat_checkpoints"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    checkpoint: Mapped[dict] = mapped_column(JSON)
    meta: Mapped[dict] = mapped_column(JSON)
    parent_id: Mapped[str | None] = mapped_column(String(64))
    writes: Mapped[list] = mapped_column(JSON, default=list)
