from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.dependencies import DB, Current, Key, ok
from app.core.errors import BusinessError, not_found
from app.core.security import timestamp
from app.db.models import Feedback, File
from app.integrations.ocr import TYPES
from app.schemas.common import Envelope, Page
from app.schemas.content import FeedbackInput, FeedbackOut, ToolOut
from app.services import content, idempotency
from app.services.files import owned

router = APIRouter(tags=["工具与内容"])


def static_page(items, settings, operation, filters, limit, cursor):
    # Stable integer positions are signed and tied to each query for this seeded catalog.
    from app.core.security import decode, sign

    start = 0
    if cursor:
        try:
            value = decode(settings, cursor, "catalog_cursor")
            if value["operation"] != operation or value["filters"] != filters:
                raise ValueError()
            start = int(value["offset"])
            if start < 0 or start > len(items):
                raise ValueError()
        except (BusinessError, ValueError, KeyError, TypeError) as exc:
            raise BusinessError("INVALID_CURSOR", "分页游标无效", 400) from exc
    more = start + limit < len(items)
    token = (
        sign(
            settings,
            "catalog_cursor",
            {"operation": operation, "filters": filters, "offset": start + limit},
            86400,
        )
        if more
        else None
    )
    return {"items": items[start : start + limit], "next_cursor": token, "has_more": more}


@router.get("/tools", response_model=Envelope[Page[ToolOut]])
def tools(
    request: Request,
    keyword: str = "",
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    items = [
        {**t, "enabled": request.app.state.ocr.enabled if t["code"] == "invoice" else t["enabled"]}
        for t in content.TOOLS
        if keyword.casefold() in (t["name"] + t["description"]).casefold()
    ]
    return ok(
        request,
        static_page(
            items, request.app.state.settings, "tools", {"keyword": keyword}, limit, cursor
        ),
    )


@router.get("/capabilities", response_model=Envelope[dict])
def capabilities(request: Request):
    settings = request.app.state.settings
    return ok(
        request,
        {
            "uploads": {
                "supported_formats": ["pdf", "jpeg", "png"],
                "product_max_size_bytes": None,
                "product_max_pdf_pages": None,
                "multipart": True,
                "storage_backend": settings.storage_backend,
                "technical_limits": {
                    "max_part_count": getattr(request.app.state.storage, "max_parts", None),
                    "max_object_size_bytes": getattr(request.app.state.storage, "max_parts", 0)
                    * getattr(request.app.state.storage, "max_part_bytes", 0)
                    or None,
                },
                "unsupported_cases": ["password_protected_pdf"],
            },
            "invoice_types": [
                {"code": code, "label": label, "recognition_status": "adapter_only"}
                for code, label in TYPES.values()
            ],
            "invoice_recognition_enabled": request.app.state.ocr.enabled,
            "export_enabled": True,
            "chat": {
                "enabled": getattr(request.app.state, "model", None).enabled
                if getattr(request.app.state, "model", None)
                else settings.model_provider != "disabled",
                "event_transport": "http_long_poll",
            },
            "wechat_login_enabled": bool(
                settings.wechat_app_id and settings.wechat_app_secret.get_secret_value()
            ),
            "dev_login_enabled": settings.enable_dev_login,
            "share_default_days": 7,
            "account_deletion_available": False,
            "wechat_subscription_available": False,
        },
    )


@router.get("/content/tips/invoice", response_model=Envelope[dict])
def tips(request: Request):
    return ok(
        request,
        {
            "items": [
                {
                    "id": "clear-image",
                    "title": "上传清晰文件",
                    "body": "使用完整、清晰的图片或可读取的 PDF，上传不代表已完成识别。",
                }
            ]
        },
    )


@router.get("/help/categories", response_model=Envelope[dict])
def categories(request: Request):
    return ok(request, {"items": content.CATEGORIES})


@router.get("/help/articles", response_model=Envelope[Page[dict]])
def articles(
    request: Request,
    category: str | None = None,
    keyword: str = "",
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    items = [
        a
        for a in content.ARTICLES
        if (category is None or a["category"] == category)
        and keyword.casefold() in (a["title"] + a["body"]).casefold()
    ]
    return ok(
        request,
        static_page(
            items,
            request.app.state.settings,
            "articles",
            {"keyword": keyword, "category": category},
            limit,
            cursor,
        ),
    )


@router.get("/help/articles/{slug}", response_model=Envelope[dict])
def article(slug: str, request: Request):
    item = next((a for a in content.ARTICLES if a["slug"] == slug), None)
    if item is None:
        raise not_found()
    return ok(request, item)


@router.get("/app/about", response_model=Envelope[dict])
def about(request: Request):
    return ok(
        request,
        {
            "app_name": "金融工具箱",
            "version": "0.4.0",
            "description": "财税效率工具",
            "support_contact": "微信客服",
        },
    )


@router.post("/feedback", status_code=201, response_model=Envelope[FeedbackOut])
def feedback(body: FeedbackInput, request: Request, db: DB, principal: Current, key: Key):
    def create():
        if len(set(body.attachment_file_ids)) != len(body.attachment_file_ids):
            raise BusinessError("VALIDATION_ERROR", "附件不能重复")
        for file_id in body.attachment_file_ids:
            file = owned(db, File, file_id, principal.user.id)
            if file.status != "ready" or file.purpose != "feedback":
                raise BusinessError("VALIDATION_ERROR", "附件不可用或用途错误")
        row = Feedback(user_id=principal.user.id, **body.model_dump())
        db.add(row)
        db.flush()
        return {"id": row.id, "status": row.status, "created_at": timestamp(row.created_at)}

    return ok(
        request,
        idempotency.execute(
            db, principal.user.id, "feedback.create", key, body.model_dump(), create
        ),
    )
