from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def make_engine(url: str):
    args = (
        {"check_same_thread": False, "timeout": 30, "autocommit": False}
        if url.startswith("sqlite")
        else {}
    )
    if url.startswith("sqlite:///./"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args=args, pool_pre_ping=True)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _):
            previous = connection.autocommit
            connection.autocommit = True
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.autocommit = previous

    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
