import logging
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException

from app.api.v1 import auth, chat, content, exports, files, invoices, records, tax, users
from app.core.config import Settings
from app.core.errors import BusinessError
from app.db.session import make_engine, make_session_factory
from app.integrations.model import build_model
from app.integrations.ocr import build_ocr
from app.integrations.storage import build_storage
from app.integrations.wechat import HttpWechatGateway
from app.schemas.common import ErrorResponse

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None, *, wechat=None, storage=None, ocr=None, model=None
):
    settings = settings or Settings()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        # Schema is managed by Alembic, never implicitly created by API startup.
        yield
        engine.dispose()

    app = FastAPI(title="金融工具箱后端", version="0.4.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.storage = storage or build_storage(settings)
    app.state.ocr = ocr or build_ocr(settings)
    app.state.model = model or build_model(settings)
    app.state.wechat = wechat or HttpWechatGateway(settings)
    locks, guard = {}, threading.Lock()

    def upload_lock(resource_id):
        with guard:
            return locks.setdefault(resource_id, threading.Lock())

    app.state.upload_lock = upload_lock

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        requested = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            requested if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", requested) else str(uuid4())
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    def error_response(request, code, message, status, details=None, retryable=False):
        return JSONResponse(
            {
                "code": code,
                "message": message,
                "request_id": getattr(request.state, "request_id", str(uuid4())),
                "data": None,
                "details": details or [],
                "retryable": retryable,
            },
            status_code=status,
        )

    @app.exception_handler(BusinessError)
    async def business_error(request, exc):
        return error_response(
            request, exc.code, exc.message, exc.status, exc.details, exc.retryable
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        details = [
            {"field": ".".join(str(v) for v in e["loc"] if v != "body"), "reason": e["type"]}
            for e in exc.errors()
        ]
        return error_response(request, "VALIDATION_ERROR", "请求参数不符合要求", 422, details)

    @app.exception_handler(StaleDataError)
    async def version_error(request, exc):
        return error_response(request, "VERSION_CONFLICT", "数据已更新，请刷新", 409)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        return error_response(request, "STATE_CONFLICT", "并发操作冲突，请重试原请求", 409)

    @app.exception_handler(OperationalError)
    async def database_error(request, exc):
        logger.warning("database operation failed: %s", type(exc).__name__)
        return error_response(request, "SERVICE_UNAVAILABLE", "数据库暂不可用", 503, retryable=True)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(
            request,
            "RESOURCE_NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
            "接口不存在" if exc.status_code == 404 else "请求无法处理",
            exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logger.error(
            "unhandled error request=%s type=%s",
            getattr(request.state, "request_id", ""),
            type(exc).__name__,
        )
        return error_response(request, "INTERNAL_ERROR", "服务内部错误", 500)

    @app.get("/health/live", tags=["健康检查"])
    def live():
        return {"status": "ok"}

    migration_config = Config()
    migration_config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    schema_head = ScriptDirectory.from_config(migration_config).get_current_head()

    @app.get("/health/ready", tags=["健康检查"])
    def ready():
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != schema_head:
                raise BusinessError("SERVICE_UNAVAILABLE", "数据库迁移版本尚未就绪", 503)
        return {"status": "ok"}

    responses = {
        status: {"model": ErrorResponse} for status in [400, 401, 403, 404, 409, 410, 422, 503]
    }
    for router in [
        auth.router,
        users.router,
        tax.router,
        content.router,
        records.router,
        files.router,
        invoices.router,
        exports.router,
        chat.router,
    ]:
        app.include_router(router, prefix="/api/v1", responses=responses)
    return app


# Uvicorn factory: uvicorn app.main:create_app --factory
