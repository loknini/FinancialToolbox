from typing import Any


class BusinessError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 422,
        *,
        details: list[dict[str, Any]] | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details or []
        self.retryable = retryable


def not_found():
    return BusinessError("RESOURCE_NOT_FOUND", "资源不存在或不可访问", 404)
