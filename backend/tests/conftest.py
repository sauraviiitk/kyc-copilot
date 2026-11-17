"""Isolated test environment.

Environment variables are set *before* any app module is imported so the
settings singleton, SQLAlchemy engine, policy retriever and cache all point at
temporary test resources. Test data is cleared between tests; no real secrets,
external services or developer SQLite files are touched.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

TEST_ROOT = Path(tempfile.mkdtemp(prefix="kyc-copilot-test-"))

import os  # noqa: E402

os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{TEST_ROOT / 'test.db'}",
        "SECRET_KEY": "test-only-secret-key-0123456789abcdef",
        "ADMIN_TOKEN": "test-admin-token-0123456789",
        "LLM_PROVIDER": "mock",
        "POLICY_BACKEND": "local",
        "REDIS_URL": "",
        "AUTO_CREATE_SCHEMA": "false",
        "APP_DATA_DIR": str(TEST_ROOT / "data"),
        "MOCK_MANDATE_FAILURE_RATE": "0",
        "MOCK_SEED": "test-seed",
        "LOG_LEVEL": "WARNING",
    }
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.entities import UserSession  # noqa: E402
from app.services.cache import reset_cache  # noqa: E402
from app.services.events import hub  # noqa: E402
from app.services.journey import create_session  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database_schema() -> Iterator[None]:
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


@pytest.fixture
def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
    # Keep tests independent of one another.
    with SessionLocal() as cleanup:
        for table in reversed(Base.metadata.sorted_tables):
            cleanup.execute(table.delete())
        cleanup.commit()
    reset_cache()
    hub.clear()
    settings.mock_mandate_failure_rate = 0.0


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session(db: Session) -> UserSession:
    record = create_session(db)
    db.commit()
    return record


def dump(value: object) -> str:
    """Stable JSON dump used by the "no raw PII anywhere" assertions."""
    return json.dumps(value, default=str, ensure_ascii=False)
