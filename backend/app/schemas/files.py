from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import Input


class UploadCreate(Input):
    filename: str = Field(min_length=1, max_length=255)
    content_type: Literal["application/pdf", "image/jpeg", "image/png"]
    size_bytes: str = Field(pattern=r"^[1-9]\d{0,29}$")
    purpose: Literal["invoice", "feedback", "chat_attachment"]

    @model_validator(mode="after")
    def safe_filename(self):
        if (
            "/" in self.filename
            or chr(92) in self.filename
            or any(ord(c) < 32 or ord(c) == 127 for c in self.filename)
        ):
            raise ValueError("文件名不能包含路径或控制字符")
        return self


class PartNumbers(Input):
    part_numbers: list[int] = Field(min_length=1, max_length=100)


class CompletedPart(Input):
    part_number: int = Field(ge=1)
    etag: str = Field(min_length=1, max_length=128)


class UploadComplete(Input):
    parts: list[CompletedPart] = Field(min_length=1)


class PartOut(BaseModel):
    part_number: int
    etag: str


class UploadOut(BaseModel):
    id: str
    status: str
    filename: str
    size_bytes: str
    purpose: str
    part_size_bytes: int
    max_part_count: int
    suggested_concurrency: int
    expires_at: str
    parts: list[PartOut]
    file_id: str | None


class PartUrl(BaseModel):
    part_number: int
    url: str
    method: str = "PUT"
    headers: dict[str, str]
    expires_at: str


class PartUrlsOut(BaseModel):
    parts: list[PartUrl]


class CompleteOut(BaseModel):
    upload_session_id: str
    file_id: str
    status: str


class FileOut(BaseModel):
    id: str
    filename: str
    content_type: str
    size_bytes: str
    purpose: str
    status: str
    sha256: str | None
    error: dict | None
    created_at: str
    expires_at: str | None


class DownloadOut(BaseModel):
    url: str
    expires_at: str
    filename: str
    content_type: str
    size_bytes: str
