"""SQLite engine factory with WAL + foreign_keys; create_all + session_factory."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

# Ensure table metadata is registered on import of create_all / session_factory.
from packages.domain import models as _models  # noqa: F401

_engines: dict[str, Engine] = {}


def _resolve_path(db_path: str | None = None) -> str:
    path = db_path or os.environ.get("DB_PATH")
    if not path:
        raise ValueError("db_path argument or DB_PATH environment variable is required")
    return str(Path(path))


def get_engine(db_path: str | None = None) -> Engine:
    """Return a cached SQLite engine with WAL and foreign_keys=ON."""
    path = _resolve_path(db_path)
    if path in _engines:
        return _engines[path]

    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    _engines[path] = engine
    return engine


def create_all(db_path: str | None = None) -> None:
    """Create all SQLModel tables against the given (or DB_PATH) SQLite file."""
    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)


def session_factory(db_path: str | None = None) -> Callable[[], Session]:
    """Return a zero-arg factory that opens a Session on the configured engine."""
    engine = get_engine(db_path)

    def _factory() -> Session:
        return Session(engine)

    return _factory
