from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.types import TypeDecorator


class ExactNumeric(TypeDecorator):
    """NUMERIC in PostgreSQL; exact decimal strings in local SQLite (no float round trip)."""

    impl = Numeric
    cache_ok = True

    def __init__(self, precision, scale):
        self.precision, self.scale = precision, scale
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(self.precision + 3))
        return dialect.type_descriptor(Numeric(self.precision, self.scale))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return format(Decimal(value), "f") if dialect.name == "sqlite" else Decimal(value)

    def process_result_value(self, value, dialect):
        return Decimal(value) if value is not None else None
