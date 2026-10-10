import hashlib
import json
from datetime import timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.core.security import now
from app.db.models import IdempotencyRecord


def execute(
    db: Session, user_id: str, operation: str, key: str, payload: dict, create: Callable[[], dict]
) -> dict:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.key == key,
        )
    )
    if record and record.expires_at <= now():
        db.delete(record)
        db.flush()
        record = None
    if record:
        if record.request_hash != digest:
            raise BusinessError("IDEMPOTENCY_CONFLICT", "操作键已用于其他参数", 409)
        return record.response_data
    try:
        # Savepoint includes resource and job. A concurrent duplicate rolls back
        # its own side effects before replaying the winner's committed response.
        with db.begin_nested():
            result = create()
            db.add(
                IdempotencyRecord(
                    user_id=user_id,
                    operation=operation,
                    key=key,
                    request_hash=digest,
                    response_data=result,
                    expires_at=now() + timedelta(hours=24),
                )
            )
            db.flush()
        return result
    except IntegrityError:
        record = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.user_id == user_id,
                IdempotencyRecord.operation == operation,
                IdempotencyRecord.key == key,
            )
        )
        if record is None:
            raise
        if record.request_hash != digest:
            raise BusinessError("IDEMPOTENCY_CONFLICT", "操作键已用于其他参数", 409) from None
        return record.response_data
