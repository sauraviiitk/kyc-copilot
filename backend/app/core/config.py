"""Application configuration loaded from environment variables only.

No secrets are ever hardcoded. Every integration degrades to a local/mock
implementation so the whole product runs offline for review and CI.
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent

DEFAULT_DATABASE_URL = "sqlite:///./data/kyc_copilot.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "KYC Copilot"
    app_env: str = "development"
    debug: bool = True

    secret_key: str = "dev-only-secret-key-change-me-please-32"
    admin_token: str = "dev-admin-token"

    database_url: str = DEFAULT_DATABASE_URL

    redis_url: str | None = "redis://localhost:6379/0"

    policy_backend: Literal["auto", "chroma", "local"] = "auto"
    chroma_dir: str = "./data/chroma"

    llm_provider: Literal["auto", "gemini", "groq", "mock"] = "auto"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    llm_timeout_seconds: float = 25.0
    llm_max_retries: int = 2
    llm_max_output_tokens: int = 1200

    price_input_per_mtok: float = 0.30
    price_output_per_mtok: float = 2.50

    mock_mandate_failure_rate: float = 0.2
    mock_ocr_min_confidence: float = 0.85
    mock_seed: str = "kyc-copilot"

    log_level: str = "INFO"
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str | None = None

    cors_origins: str = "http://localhost:5173,http://localhost:4173,http://localhost:3000"

    kill_switch_default: bool = False
    app_data_dir: str = "./data"
    auto_create_schema: bool = True
    evals_dir: str = ""
    evals_reports_dir: str = ""
    max_agent_tool_calls: int = 6
    max_planning_attempts: int = 2

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            # An empty dashboard value (common on Render/Vercel UIs) should
            # behave like the documented local default, not crash the app.
            return DEFAULT_DATABASE_URL
        # Supabase / Heroku hand out postgres:// or postgresql://; SQLAlchemy
        # needs the driver-qualified URL.
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value[len("postgresql://") :]
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def database_host(self) -> str:
        """Hostname for logs (never includes the username or password)."""
        try:
            return make_url(self.database_url).host or "local"
        except Exception:
            return "invalid-url"

    @property
    def database_url_is_placeholder(self) -> bool:
        """True when the example connection string from .env.example was used."""
        lowered = self.database_url.lower()
        return any(marker in lowered for marker in ("<region>", "<ref>", "<password>", "<host>"))

    @property
    def database_url_source(self) -> str:
        """Where DATABASE_URL came from, without revealing its value.

        Hosting dashboards and stray `.env` files inside a container are both
        plausible sources, and they are fixed in completely different places,
        so the startup error names the one that is actually in use.
        """
        if "DATABASE_URL" in os.environ or "database_url" in os.environ:
            return "environment variable"
        if self.database_url != DEFAULT_DATABASE_URL:
            return ".env file"
        return "built-in default"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def data_dir(self) -> Path:
        path = Path(self.app_data_dir)
        if not path.is_absolute():
            path = (BACKEND_ROOT / path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def upload_dir(self) -> Path:
        path = self.data_dir / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def chroma_path(self) -> Path:
        path = Path(self.chroma_dir)
        if not path.is_absolute():
            path = (BACKEND_ROOT / path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def scenarios_dir(self) -> Path:
        if self.evals_dir:
            path = Path(self.evals_dir)
            path = path if path.is_absolute() else (BACKEND_ROOT / path).resolve()
        else:
            path = (REPO_ROOT / "evals" / "scenarios").resolve()
        return path

    @property
    def reports_dir(self) -> Path:
        if self.evals_reports_dir:
            path = Path(self.evals_reports_dir)
            path = path if path.is_absolute() else (BACKEND_ROOT / path).resolve()
        else:
            path = (REPO_ROOT / "evals" / "reports").resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def secret_fingerprint(self) -> str:
        return hashlib.sha256(self.secret_key.encode()).hexdigest()[:12]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    def effective_llm_provider(self) -> str:
        if self.llm_provider == "mock":
            return "mock"
        if self.llm_provider in {"gemini", "groq"}:
            return self.llm_provider
        if self.gemini_api_key:
            return "gemini"
        if self.groq_api_key:
            return "groq"
        return "mock"

    def validate_production(self) -> list[str]:
        """Return warnings for unsafe production configuration."""
        problems: list[str] = []
        if self.is_production:
            if "change-me" in self.secret_key or len(self.secret_key) < 32:
                problems.append("SECRET_KEY must be a unique value of >=32 chars in production")
            if "change-me" in self.admin_token or len(self.admin_token) < 16:
                problems.append("ADMIN_TOKEN must be set to a strong value in production")
            if self.is_sqlite:
                problems.append("SQLite is not a production database; use Postgres/Supabase")
        return problems

    # Used by tests to build isolated settings.
    def copy_with(self, **overrides) -> Settings:
        data = self.model_dump()
        data.update(overrides)
        return Settings(**data)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
