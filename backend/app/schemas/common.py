from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Envelope(BaseModel, Generic[T]):
    code: str = "OK"
    message: str = "success"
    request_id: str
    data: T


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


class ErrorDetail(BaseModel):
    field: str
    reason: str


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str
    data: None = None
    details: list[ErrorDetail] = Field(default_factory=list)
    retryable: bool = False


class EmptyInput(Input):
    pass
