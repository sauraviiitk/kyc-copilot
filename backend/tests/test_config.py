"""Configuration parsing tests for deployment-critical settings."""

from __future__ import annotations

from app.core.config import DEFAULT_DATABASE_URL, Settings, settings


def test_placeholder_url_is_detected() -> None:
    placeholder = (
        "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres"
    )

    assert settings.copy_with(database_url=placeholder).database_url_is_placeholder


def test_real_url_is_not_a_placeholder() -> None:
    real = (
        "postgresql://postgres.abcdefghij:secret@aws-0-ap-southeast-2.pooler.supabase.com:6543/postgres"
    )
    configured = settings.copy_with(database_url=real)

    assert not configured.database_url_is_placeholder
    assert configured.database_host == "aws-0-ap-southeast-2.pooler.supabase.com"


def test_placeholder_host_is_safe_to_log() -> None:
    placeholder = (
        "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres"
    )

    host = settings.copy_with(database_url=placeholder).database_host

    assert "password" not in host
    assert "<region>" in host


def test_empty_url_falls_back_to_sqlite() -> None:
    configured = Settings(database_url="")

    assert configured.database_url == DEFAULT_DATABASE_URL
    assert configured.is_sqlite


def test_source_reports_environment_variable(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@db.example.com:5432/postgres")

    assert Settings().database_url_source == "environment variable"


def test_source_reports_dotenv_value(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("database_url", raising=False)

    configured = Settings(database_url="postgresql://user:secret@db.example.com:5432/postgres")

    assert configured.database_url_source == ".env file"


def test_source_reports_built_in_default(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("database_url", raising=False)

    assert Settings(database_url=DEFAULT_DATABASE_URL).database_url_source == "built-in default"
