from contextlib import contextmanager
from uuid import uuid4

from qcloud_cos import CosConfig, CosS3Client
from qcloud_cos.cos_exception import CosClientError, CosServiceError

from app.core.errors import BusinessError


class CosStorage:
    remote = True
    backend = "cos"
    max_parts = 10000
    min_part_bytes = 1024 * 1024
    max_part_bytes = 5 * 1024**3

    def __init__(self, settings, client=None):
        self.settings = settings
        self.bucket = settings.cos_bucket
        self.cache = settings.storage_root.resolve() / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = client or CosS3Client(
            CosConfig(
                Region=settings.cos_region,
                SecretId=settings.cos_secret_id.get_secret_value(),
                SecretKey=settings.cos_secret_key.get_secret_value(),
                Scheme="https",
                Timeout=60,
            )
        )

    def key(self, key):
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError("invalid object key")
        return self.settings.cos_prefix.strip("/") + "/" + key

    def call(self, method, **kwargs):
        try:
            return getattr(self.client, method)(Bucket=self.bucket, **kwargs)
        except (CosServiceError, CosClientError) as exc:
            status = exc.get_status_code() if isinstance(exc, CosServiceError) else None
            raise BusinessError(
                "STORAGE_UNAVAILABLE",
                "对象存储暂不可用",
                503,
                retryable=status is None or int(status) >= 500 or int(status) == 429,
            ) from exc

    def begin(self, upload_id, content_type):
        return self.call(
            "create_multipart_upload",
            Key=self.key("objects/" + upload_id),
            ContentType=content_type,
        )["UploadId"]

    def part_url(self, row, number, seconds):
        return self.call(
            "get_presigned_url",
            Key=self.key("objects/" + row.id),
            Method="PUT",
            Expired=seconds,
            Params={"uploadId": row.remote_upload_id, "partNumber": str(number)},
            Headers={"Content-Type": "application/octet-stream"},
        )

    def list_parts(self, row):
        result = []
        marker = 0
        while True:
            page = self.call(
                "list_parts",
                Key=self.key("objects/" + row.id),
                UploadId=row.remote_upload_id,
                MaxParts=1000,
                PartNumberMarker=marker,
            )
            raw = page.get("Part", [])
            if isinstance(raw, dict):
                raw = [raw]
            result.extend(
                {
                    "part_number": int(p["PartNumber"]),
                    "etag": p["ETag"],
                    "size_bytes": int(p["Size"]),
                }
                for p in raw
            )
            if str(page.get("IsTruncated", "false")).lower() != "true":
                break
            next_marker = int(page["NextPartNumberMarker"])
            if next_marker <= marker:
                raise BusinessError(
                    "STORAGE_UNAVAILABLE", "对象存储分片分页异常", 503, retryable=True
                )
            marker = next_marker
        return result

    def complete(self, row, parts):
        key = self.key("objects/" + row.id)
        # Completion can have succeeded before a worker crash. Unique immutable object key
        # and size allow recovery without repeating a no-longer-existing upload ID.
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except CosServiceError as exc:
            if int(exc.get_status_code()) != 404:
                raise BusinessError(
                    "STORAGE_UNAVAILABLE", "无法查询对象存储状态", 503, retryable=True
                ) from exc
        except CosClientError as exc:
            raise BusinessError(
                "STORAGE_UNAVAILABLE", "对象存储连接失败", 503, retryable=True
            ) from exc
        else:
            if int(head["Content-Length"]) != int(row.size_bytes):
                raise BusinessError("CORRUPTED_FILE", "云端文件大小不一致")
            return
        self.call(
            "complete_multipart_upload",
            Key=key,
            UploadId=row.remote_upload_id,
            MultipartUpload={
                "Part": [{"PartNumber": p.part_number, "ETag": p.etag} for p in parts]
            },
        )
        head = self.call("head_object", Key=key)
        if int(head["Content-Length"]) != int(row.size_bytes):
            raise BusinessError("CORRUPTED_FILE", "云端文件大小不一致")

    def abort(self, row):
        if row.remote_upload_id:
            try:
                self.client.abort_multipart_upload(
                    Bucket=self.bucket,
                    Key=self.key("objects/" + row.id),
                    UploadId=row.remote_upload_id,
                )
            except CosServiceError as exc:
                if int(exc.get_status_code()) != 404:
                    raise BusinessError(
                        "STORAGE_UNAVAILABLE", "终止云分片失败", 503, retryable=True
                    ) from exc
            except CosClientError as exc:
                raise BusinessError(
                    "STORAGE_UNAVAILABLE", "对象存储连接失败", 503, retryable=True
                ) from exc

    @contextmanager
    def materialize(self, file):
        path = self.cache / (str(uuid4()) + ".data")
        response = self.call("get_object", Key=self.key(file.object_key))
        body = response["Body"]
        try:
            body.get_stream_to_file(str(path))
            if path.stat().st_size != int(file.size_bytes):
                raise BusinessError("CORRUPTED_FILE", "云端文件大小不一致")
            yield path
        finally:
            try:
                body.get_raw_stream().close()
            finally:
                if path.exists():
                    path.unlink()

    def publish(self, key, source, content_type):
        self.call(
            "upload_file",
            Key=self.key(key),
            LocalFilePath=str(source),
            ContentType=content_type,
            PartSize=8,
            MAXThread=2,
            EnableMD5=True,
        )

    def download_url(self, file, seconds):
        return self.call(
            "get_presigned_url", Key=self.key(file.object_key), Method="GET", Expired=seconds
        )
