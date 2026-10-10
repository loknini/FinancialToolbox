from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.schemas.common import EmptyInput, Input
from app.schemas.tax import TaxInput
from app.services import idempotency, tax


@dataclass(frozen=True)
class ToolContext:
    # Built by the authenticated application, never from model arguments.
    db: Session
    user_id: str
    operation_key: str
    ocr: object | None = None
    settings: object | None = None
    storage: object | None = None
    approved: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Callable
    requires_confirmation: bool = False


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self.tools:
            raise ValueError("duplicate tool")
        self.tools[tool.name] = tool

    def definitions(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.schema.model_json_schema(),
            }
            for tool in self.tools.values()
        ]

    def execute(self, name: str, arguments: dict, context: ToolContext) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            raise BusinessError("TOOL_NOT_ALLOWED", "工具不存在或未启用", 403)
        try:
            body = tool.schema.model_validate(arguments)
        except ValidationError as exc:
            raise BusinessError("VALIDATION_ERROR", "工具参数无效") from exc
        if (
            tool.requires_confirmation or (name == "show_ui_action" and body.name == "logout")
        ) and not context.approved:
            raise BusinessError("CONFIRMATION_REQUIRED", "此工具需要持久化操作确认", 409)
        return tool.handler(context, body)


class ToolTaxInput(TaxInput):
    pass


class ToolResourceInput(Input):
    resource_id: str


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool("get_tax_config", "获取拆税选项和规则版本", EmptyInput, lambda ctx, body: tax.config())
    )

    def calculate(ctx, body):
        return idempotency.execute(
            ctx.db,
            ctx.user_id,
            "tax.create",
            ctx.operation_key,
            body.model_dump(),
            lambda: tax.calculate(ctx.db, ctx.user_id, body),
        )

    registry.register(
        Tool("calculate_tax", "按明确的金额和税率计算并保存拆税记录", ToolTaxInput, calculate)
    )

    def get_calculation(ctx, body):
        from app.db.models import TaxCalculation
        from app.services.files import owned

        return tax.serialize(owned(ctx.db, TaxCalculation, body.resource_id, ctx.user_id))

    registry.register(
        Tool("get_tax_calculation", "获取本人拆税记录", ToolResourceInput, get_calculation)
    )
    from app.db.models import Invoice, InvoiceBatch
    from app.integrations.ocr import DisabledOcrProvider
    from app.schemas.invoices import BatchCreate, BatchRetry, ConfirmInput, InvoicePatch
    from app.services import invoices
    from app.services.files import owned

    class PatchTool(InvoicePatch):
        resource_id: str

    class ConfirmTool(ConfirmInput):
        resource_id: str

    class RetryTool(BatchRetry):
        resource_id: str

    def create_batch(ctx, body):
        return idempotency.execute(
            ctx.db,
            ctx.user_id,
            "batch.create",
            ctx.operation_key,
            body.model_dump(),
            lambda: invoices.create_batch(
                ctx.db, ctx.user_id, body, ctx.ocr or DisabledOcrProvider()
            ),
        )

    def retry_batch(ctx, body):
        data = BatchRetry(scope=body.scope)
        return idempotency.execute(
            ctx.db,
            ctx.user_id,
            "batch.retry:" + body.resource_id,
            ctx.operation_key,
            data.model_dump(),
            lambda: invoices.retry_batch(
                ctx.db, ctx.user_id, body.resource_id, ctx.ocr or DisabledOcrProvider()
            ),
        )

    registry.register(
        Tool("create_invoice_batch", "使用本人已上传文件创建识别批次", BatchCreate, create_batch)
    )
    registry.register(
        Tool(
            "get_invoice_batch",
            "获取本人识别进度",
            ToolResourceInput,
            lambda ctx, body: invoices.batch_out(
                ctx.db, owned(ctx.db, InvoiceBatch, body.resource_id, ctx.user_id)
            ),
        )
    )
    registry.register(
        Tool(
            "get_invoice",
            "获取本人票据及核对问题",
            ToolResourceInput,
            lambda ctx, body: invoices.invoice_out(
                owned(ctx.db, Invoice, body.resource_id, ctx.user_id), True
            ),
        )
    )
    registry.register(
        Tool(
            "patch_invoice",
            "按用户提供的明确字段修正本人票据",
            PatchTool,
            lambda ctx, body: invoices.patch_invoice(
                ctx.db,
                ctx.user_id,
                body.resource_id,
                InvoicePatch.model_validate(
                    body.model_dump(exclude={"resource_id"}, exclude_unset=True)
                ),
            ),
            requires_confirmation=True,
        )
    )
    registry.register(
        Tool(
            "confirm_invoice",
            "确认本人已核对票据",
            ConfirmTool,
            lambda ctx, body: invoices.confirm_invoice(
                ctx.db,
                ctx.user_id,
                body.resource_id,
                ConfirmInput(expected_version=body.expected_version),
            ),
            requires_confirmation=True,
        )
    )
    registry.register(Tool("retry_invoice_batch", "重试本人批次失败单元", RetryTool, retry_batch))
    registry.register(
        Tool(
            "cancel_invoice_batch",
            "取消本人识别批次",
            ToolResourceInput,
            lambda ctx, body: invoices.cancel_batch(ctx.db, ctx.user_id, body.resource_id),
        )
    )
    from app.db.models import Export
    from app.schemas.exports import ExportRequest
    from app.services import exports

    def create_export(ctx, body):
        return idempotency.execute(
            ctx.db,
            ctx.user_id,
            "export.create",
            ctx.operation_key,
            body.model_dump(mode="json"),
            lambda: exports.create_export(ctx.db, ctx.user_id, body),
        )

    registry.register(
        Tool("create_export", "按用户明确范围保存快照并创建导出任务", ExportRequest, create_export)
    )
    registry.register(
        Tool(
            "get_export",
            "查询本人导出任务",
            ToolResourceInput,
            lambda ctx, body: exports.export_out(
                ctx.db, owned(ctx.db, Export, body.resource_id, ctx.user_id)
            ),
        )
    )
    registry.register(
        Tool(
            "cancel_export",
            "取消本人导出任务",
            ToolResourceInput,
            lambda ctx, body: exports.cancel_export(ctx.db, ctx.user_id, body.resource_id),
        )
    )
    # Sensitive tools are registered with a server-built confirmation guard.
    from app.agent.catalog import extend_registry

    extend_registry(registry)
    return registry
