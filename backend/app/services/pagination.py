import hashlib
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_

from app.core.config import Settings
from app.core.errors import BusinessError
from app.core.security import decode, sign


def date_range(date_from, date_to):
    if date_from and date_to and date_from > date_to:
        raise BusinessError("VALIDATION_ERROR", "开始日期不能晚于结束日期")
    zone = ZoneInfo("Asia/Shanghai")

    def convert(day):
        return (
            datetime.combine(day, time.min, tzinfo=zone)
            .astimezone(ZoneInfo("UTC"))
            .replace(tzinfo=None)
        )

    return (
        convert(date_from) if date_from else None,
        convert(date_to + timedelta(days=1)) if date_to else None,
    )


def paginate(
    db,
    settings: Settings,
    query,
    model,
    filters: dict,
    cursor: str | None,
    limit: int,
    serialize,
    *,
    scalar_rows=True,
):
    signature = hashlib.sha256(
        json.dumps(filters, sort_keys=True, default=str).encode()
    ).hexdigest()
    if cursor:
        try:
            claims = decode(settings, cursor, "cursor")
            if claims.get("filter") != signature:
                raise ValueError("filters changed")
            stamp = datetime.fromisoformat(claims["time"])
            query = query.where(
                or_(
                    model.created_at < stamp,
                    and_(model.created_at == stamp, model.id < claims["id"]),
                )
            )
        except (BusinessError, ValueError, KeyError, TypeError) as exc:
            raise BusinessError("INVALID_CURSOR", "分页游标无效或筛选已变更", 400) from exc
    statement = query.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
    rows = list(db.scalars(statement) if scalar_rows else db.execute(statement))
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more:
        last = rows[-1]
        next_cursor = sign(
            settings,
            "cursor",
            {"filter": signature, "id": last.id, "time": last.created_at.isoformat()},
            86400,
        )
    return {"items": [serialize(row) for row in rows], "has_more": more, "next_cursor": next_cursor}
