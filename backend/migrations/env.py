from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import Settings
from app.db.models import Base
from app.db.types import ExactNumeric

config = context.config
target_metadata = Base.metadata
url = Settings().database_url


def render_item(kind, obj, autogen_context):
    if kind == "type" and isinstance(obj, ExactNumeric):
        return (
            f"sa.Numeric({obj.precision}, {obj.scale}).with_variant("
            f"sa.String({obj.precision + 3}), 'sqlite')"
        )
    return False


def run_offline():
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online():
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
