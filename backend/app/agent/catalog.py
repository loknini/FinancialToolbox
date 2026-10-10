"""Reuse authenticated API/service handlers without HTTP loopback or model credentials."""

from datetime import date
from types import SimpleNamespace
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from app.agent.tools import Tool, ToolResourceInput
from app.api.v1 import content, exports, invoices, records, tax, users
from app.core.errors import BusinessError
from app.db.models import Export, File, User
from app.schemas.auth import NotificationPatch, PrivacyPatch, UserPatch
from app.schemas.chat import AskInput, ShareTool, UiTool
from app.schemas.common import EmptyInput, Input
from app.schemas.content import FeedbackInput
from app.services import exports as export_service
from app.services.files import owned


class Listing(Input):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None
    date_from: date | None = None
    date_to: date | None = None


class InvoiceListing(Listing):
    batch_id: str | None = None
    review_status: Literal["pending_review", "confirmed"] | None = None
    type_code: str | None = None
    invoice_date_from: date | None = None
    invoice_date_to: date | None = None


class RecordsListing(Listing):
    type: Literal["invoice_batch", "tax_calculation", "export"] | None = None


class HelpListing(Input):
    category: str | None = None
    keyword: str = Field(default="", max_length=100)
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ToolSearch(Input):
    keyword: str = Field(default="", max_length=100)
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class SlugInput(Input):
    slug: str


def endpoint(handler, *, body=False, key=False):
    def execute(ctx, data):
        request = SimpleNamespace(
            state=SimpleNamespace(request_id="agent"),
            app=SimpleNamespace(
                state=SimpleNamespace(settings=ctx.settings, storage=ctx.storage, ocr=ctx.ocr)
            ),
        )
        principal = SimpleNamespace(user=ctx.db.get(User, ctx.user_id))
        args = {"request": request, "db": ctx.db, "principal": principal}
        import inspect

        signature = inspect.signature(handler)
        args = {k: v for k, v in args.items() if k in signature.parameters}
        if body:
            args["body"] = data
        else:
            args.update(data.model_dump())
        if key:
            args["key"] = ctx.operation_key
        return handler(**args)["data"]

    return execute


def extend_registry(registry):
    for name, description, schema, handler in [
        ("list_tax_calculations", "按日期分页查询本人拆税历史", Listing, tax.list_calculations),
        ("list_invoice_batches", "按日期分页查询本人识别批次", Listing, invoices.batches),
        (
            "list_invoices",
            "按批次、核对状态和日期分页查询本人票据",
            InvoiceListing,
            invoices.list_invoices,
        ),
        ("list_exports", "按日期分页查询本人导出", Listing, exports.listing),
        ("list_records", "分页查询本人使用记录", RecordsListing, records.records),
        ("get_profile", "获取本人资料", EmptyInput, users.me),
        ("list_tools", "搜索工具目录", ToolSearch, content.tools),
        ("list_my_tools", "获取本人使用过的工具", EmptyInput, records.my_tools),
        ("get_notification_settings", "获取本人通知设置", EmptyInput, users.notifications),
        ("get_privacy_settings", "获取本人隐私设置", EmptyInput, users.privacy),
        ("get_capabilities", "获取已启用功能与限制", EmptyInput, content.capabilities),
        ("get_help_categories", "获取帮助分类", EmptyInput, content.categories),
        ("list_help_articles", "搜索帮助文章", HelpListing, content.articles),
        ("get_help_article", "获取帮助正文", SlugInput, content.article),
        ("get_about", "获取应用说明", EmptyInput, content.about),
        ("get_invoice_tips", "获取上传提示", EmptyInput, content.tips),
        (
            "get_export_share",
            "查询本人导出分享状态，不返回访问凭证",
            ToolResourceInput,
            exports.share_detail,
        ),
    ]:
        registry.register(Tool(name, description, schema, endpoint(handler)))
    for name, description, schema, handler, key in [
        ("update_profile", "修改本人显示名称，需要确认", UserPatch, users.patch_me, False),
        (
            "update_notification_settings",
            "修改本人通知设置，需要确认",
            NotificationPatch,
            users.patch_notifications,
            False,
        ),
        (
            "update_privacy_settings",
            "修改未来会话隐私设置，需要确认",
            PrivacyPatch,
            users.patch_privacy,
            False,
        ),
        ("submit_feedback", "提交用户明确提供的反馈", FeedbackInput, content.feedback, True),
    ]:
        registry.register(
            Tool(
                name,
                description,
                schema,
                endpoint(handler, body=True, key=key),
                requires_confirmation=name != "submit_feedback",
            )
        )
    registry.register(
        Tool(
            "revoke_export_share",
            "撤销本人分享，需要确认",
            ToolResourceInput,
            endpoint(exports.revoke),
            requires_confirmation=True,
        )
    )

    def security(ctx, body):
        from app.core.security import timestamp
        from app.db.models import AuthSession

        user = ctx.db.get(User, ctx.user_id)
        latest = ctx.db.scalar(
            select(AuthSession)
            .where(AuthSession.user_id == ctx.user_id)
            .order_by(AuthSession.last_login_at.desc())
            .limit(1)
        )
        return {
            "account_status": user.status,
            "wechat_bound": user.appid != "development",
            "last_login_at": timestamp(latest.last_login_at) if latest else None,
            "account_deletion_available": False,
            "support_contact": "微信客服",
        }

    registry.register(Tool("get_security", "获取本人账号安全摘要", EmptyInput, security))

    def file_info(ctx, body):
        row = owned(ctx.db, File, body.resource_id, ctx.user_id)
        return {
            "id": row.id,
            "status": row.status,
            "purpose": row.purpose,
            "content_type": row.content_type,
        }

    registry.register(
        Tool("get_file", "获取本人上传文件状态，不读取原件正文", ToolResourceInput, file_info)
    )

    def share(ctx, body):
        from app.schemas.exports import ShareRequest

        return export_service.create_share(
            ctx.db,
            ctx.settings,
            ctx.user_id,
            body.resource_id,
            ShareRequest(expires_in_days=body.expires_in_days),
            ctx.operation_key,
        )

    registry.register(
        Tool(
            "create_export_share",
            "准备分享范围，必须经服务端确认后执行",
            ShareTool,
            share,
            requires_confirmation=True,
        )
    )
    registry.register(
        Tool(
            "request_input",
            "缺少必要参数或资源有歧义时向用户提问",
            AskInput,
            lambda ctx, body: body.model_dump(),
        )
    )

    def ui(ctx, body):
        if body.name in {"open_download", "open_share"}:
            if not body.resource_id:
                raise BusinessError("VALIDATION_ERROR", "必须指定本人导出")
            export = owned(ctx.db, Export, body.resource_id, ctx.user_id)
            export_service.available_file(ctx.db, export, ctx.storage)
            if body.name == "open_share" and not export_service.get_share(
                ctx.db, ctx.user_id, export.id
            ):
                raise BusinessError("SHARE_UNAVAILABLE", "请先创建分享")
        elif body.resource_id:
            raise BusinessError("VALIDATION_ERROR", "此界面操作不接受资源参数")
        return {"name": body.name, "resource_id": body.resource_id}

    registry.register(
        Tool(
            "show_ui_action",
            "请求客户端下载、分享、客服、退出或微信授权；不执行客户端动作",
            UiTool,
            ui,
        )
    )
