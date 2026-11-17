"""Database engine/session helpers (sync SQLAlchemy 2.0).

Sync is a deliberate choice: the whole stack stays simple and portable, SQLite
works out of the box, and the Supabase pooler works with psycopg 3. Agent work
runs in a worker thread, so the event loop is never blocked by DB I/O.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

_engine_kwargs: dict = {"pool_pre_ping": True, "future": True}
if settings.is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Supabase's transaction pooler (port 6543) is PgBouncer in transaction
    # mode: server-side prepared statements survive across pooled clients and
    # produce "prepared statement _pg3_0 already exists". Disabling psycopg3
    # auto-prepare makes the app work with both pooler modes.
    _engine_kwargs["connect_args"] = {"prepare_threshold": None}

engine = create_engine(settings.database_url, **_engine_kwargs)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for background threads."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
