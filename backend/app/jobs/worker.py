import argparse
import logging
import shutil
import threading
import time
from datetime import timedelta
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import and_, or_, select, update

from app.agent import engine as agent_engine
from app.core.config import Settings
from app.core.errors import BusinessError
from app.core.security import now
from app.db.models import File, Job, UploadPart, UploadSession
from app.db.session import make_engine, make_session_factory
from app.integrations.model import build_model
from app.integrations.ocr import build_ocr
from app.integrations.storage import build_storage, file_hash
from app.jobs import export_tasks, invoice_tasks

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings, session_factory, storage, ocr=None, model=None):
        self.settings, self.sessions, self.storage = settings, session_factory, storage
        self.ocr = ocr or build_ocr(settings)
        self.model = model or build_model(settings)

    def claim(self):
        instant = now()
        eligible = or_(
            and_(Job.status == "queued", Job.available_at <= instant),
            and_(Job.status == "processing", Job.lease_until <= instant),
        )
        with self.sessions.begin() as db:
            row = db.scalar(
                select(Job)
                .where(eligible)
                .order_by(Job.available_at, Job.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            token = str(uuid4())
            result = db.execute(
                update(Job)
                .where(Job.id == row.id, eligible)
                .values(
                    status="processing",
                    lease_token=token,
                    lease_until=instant + timedelta(seconds=self.settings.job_lease_seconds),
                    attempt=Job.attempt + 1,
                ),
                execution_options={"synchronize_session": False},
            )
            if result.rowcount != 1:
                return None
            if row.type in invoice_tasks.TYPES:
                invoice_tasks.mark_claim(db, row)
            if row.type == "generate_export":
                export_tasks.mark_claim(db, row)
            if row.type == "chat_tick":
                agent_engine.mark_claim(db, row)
            return row.id, token

    def heartbeat(self, job_id, token, stop):
        interval = max(1, self.settings.job_lease_seconds // 3)
        while not stop.wait(interval):
            try:
                with self.sessions.begin() as db:
                    result = db.execute(
                        update(Job)
                        .where(
                            Job.id == job_id,
                            Job.lease_token == token,
                            Job.status == "processing",
                            Job.lease_until > now(),
                        )
                        .values(
                            lease_until=now() + timedelta(seconds=self.settings.job_lease_seconds)
                        )
                    )
                    if result.rowcount != 1:
                        return
            except Exception as exc:
                logger.warning("job heartbeat failed: %s", type(exc).__name__)

    def process_one(self):
        from app.services.chat import purge_expired

        with self.sessions.begin() as db:
            purge_expired(db)
        claim = self.claim()
        if claim is None:
            return False
        job_id, token = claim
        stop = threading.Event()
        thread = threading.Thread(target=self.heartbeat, args=(job_id, token, stop), daemon=True)
        thread.start()
        result = None
        error = None
        try:
            with self.sessions() as db:
                job = db.get(Job, job_id)
                if job.attempt > job.max_attempts:
                    raise BusinessError("JOB_ATTEMPTS_EXHAUSTED", "任务重试次数已耗尽")
                if job.type == "chat_tick":
                    result = agent_engine.perform(db, job, self.settings, self.model)
                elif job.type == "validate_file":
                    file = db.get(File, job.resource_id)
                    if file is None:
                        raise BusinessError("RESOURCE_NOT_FOUND", "文件不存在", 404)
                    result = self.validate_file(db, file, token)
                elif job.type == "generate_export":
                    result = export_tasks.perform(db, job, self.settings, self.storage)
                elif job.type in invoice_tasks.TYPES:
                    result = invoice_tasks.perform(db, job, self.storage, self.ocr)
                else:
                    raise BusinessError("CAPABILITY_DISABLED", "尚未实现该任务类型")
        except BusinessError as exc:
            error = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
        except Exception as exc:
            logger.warning("job execution failed: %s", type(exc).__name__)
            error = {
                "code": "AGENT_EXECUTION_ERROR"
                if job.type == "chat_tick"
                else "STORAGE_UNAVAILABLE",
                "message": "助手处理暂时失败" if job.type == "chat_tick" else "文件处理暂时失败",
                "retryable": True,
            }
        finally:
            stop.set()
            thread.join(timeout=5)
        self.finish(job_id, token, result, error)
        return True

    def validate_file(self, db, file, token):
        parts = list(
            db.scalars(
                select(UploadPart)
                .where(UploadPart.session_id == file.upload_session_id)
                .order_by(UploadPart.part_number)
            )
        )
        upload = db.get(UploadSession, file.upload_session_id)
        if upload.storage_backend != self.storage.backend:
            raise BusinessError("STORAGE_BACKEND_MISMATCH", "当前 Worker 存储配置与文件不一致")
        if self.storage.remote:
            self.storage.complete(upload, parts)
            with self.storage.materialize(file) as path:
                self.verify_content(path, file)
                return {"sha256": file_hash(path)}
        destination = self.storage.object_path(file.object_key)
        temporary = destination.with_name(destination.name + "." + token + ".tmp")
        try:
            with temporary.open("wb") as output:
                for part in parts:
                    source = self.storage.part_path(file.upload_session_id, part.part_number)
                    if '"' + file_hash(source) + '"' != part.etag:
                        raise BusinessError("CORRUPTED_FILE", "上传分片内容校验失败")
                    with source.open("rb") as stream:
                        shutil.copyfileobj(stream, output, length=1024 * 1024)
            if temporary.stat().st_size != int(file.size_bytes):
                raise BusinessError("CORRUPTED_FILE", "文件大小校验失败")
            self.verify_content(temporary, file)
            digest = file_hash(temporary)
            temporary.replace(destination)
            return {"sha256": digest}
        finally:
            if temporary.exists():
                temporary.unlink()

    def verify_content(self, path, file):
        try:
            if file.content_type == "application/pdf":
                with path.open("rb") as source:
                    if source.read(5) != b"%PDF-":
                        raise BusinessError("UNSUPPORTED_FORMAT", "文件内容不是 PDF")
                    source.seek(0)
                    reader = PdfReader(source)
                    if reader.is_encrypted:
                        raise BusinessError("PASSWORD_PROTECTED_PDF", "请上传解密后的 PDF")
                    if len(reader.pages) == 0:
                        raise BusinessError("CORRUPTED_FILE", "PDF 不含页面")
            else:
                with Image.open(path) as image:
                    expected = "JPEG" if file.content_type == "image/jpeg" else "PNG"
                    if image.format != expected:
                        raise BusinessError("UNSUPPORTED_FORMAT", "图片内容与声明格式不一致")
                    image.verify()
        except (
            PdfReadError,
            UnidentifiedImageError,
            ValueError,
            SyntaxError,
            OSError,
            Image.DecompressionBombError,
        ) as exc:
            raise BusinessError("CORRUPTED_FILE", "文件无法正常读取") from exc

    def finish(self, job_id, token, result, error):
        with self.sessions.begin() as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            retry = bool(error and error["retryable"] and job.attempt < job.max_attempts)
            values = {
                "status": "queued" if retry else "failed" if error else "succeeded",
                "error": error,
                "lease_token": None,
                "lease_until": None,
            }
            if retry:
                values["available_at"] = now() + timedelta(seconds=min(60, 2**job.attempt))
            changed = db.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.lease_token == token,
                    Job.status == "processing",
                    Job.lease_until > now(),
                )
                .values(**values),
                execution_options={"synchronize_session": False},
            )
            if changed.rowcount != 1:
                return  # An expired worker may not write final resource state.
            if job.type == "chat_tick":
                agent_engine.apply(
                    db, job, result, error, retry, self.settings, self.storage, self.ocr
                )
                return
            if job.type in invoice_tasks.TYPES:
                invoice_tasks.apply(db, job, result, error, retry)
                return
            if job.type == "generate_export":
                export_tasks.apply(db, job, result, error, retry, self.settings)
                return
            if retry:
                return
            file = db.get(File, job.resource_id)
            if file:
                file.status = "rejected" if error else "ready"
                file.error = error
                if result:
                    file.sha256 = result["sha256"]
                upload = db.get(UploadSession, file.upload_session_id)
                upload.status = "failed" if error else "completed"


def main():
    parser = argparse.ArgumentParser(description="金融工具箱后台任务")
    parser.add_argument("--once", action="store_true", help="执行至多一个任务后退出")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    engine = make_engine(settings.database_url)
    worker = Worker(settings, make_session_factory(engine), build_storage(settings))
    try:
        while True:
            try:
                processed = worker.process_one()
            except Exception as exc:
                logger.warning("job transaction failed: %s", type(exc).__name__)
                processed = False
            if args.once:
                break
            if not processed:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
