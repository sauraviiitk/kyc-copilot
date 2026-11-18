"""FastAPI application entry point.

    uvicorn app.main:app --reload --port 8000

All secrets come from environment variables. With no LLM key configured the
app runs the deterministic offline planner, so every feature (including the
evaluation harness) is usable offline.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.api import (
    routes_agent,
    routes_audit,
    routes_eval,
    routes_kyc,
    routes_screen,
    routes_sessions,
    routes_system,
)
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.base import Base
from app.db.seed import seed_all
from app.db.session import engine

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "app.config.database source=%s host=%s placeholder=%s",
        settings.database_url_source,
        settings.database_host,
        settings.database_url_is_placeholder,
    )

    if settings.database_url_is_placeholder:
        raise RuntimeError(
            "DATABASE_URL still contains the placeholder example from .env.example "
            f"(read from: {settings.database_url_source}, host: {settings.database_host}). "
            "Set the real Supabase/Postgres connection string in your hosting "
            "provider's environment variables, or remove DATABASE_URL entirely to "
            "run on local SQLite. If you already edited the dashboard variable, "
            "check for a duplicate variable, an environment group, or a .env "
            "secret file overriding it."
        )

    if settings.database_host.endswith(".supabase.co"):
        logger.warning(
            "app.config.database host=%s is the Supabase direct endpoint, which is "
            "IPv6-only unless the project has the IPv4 add-on. Networks without "
            "IPv6 egress (Render, most CI runners) must use the shared pooler: "
            "aws-<n>-<region>.pooler.supabase.com with user postgres.<project-ref> "
            "on port 5432 (session) or 6543 (transaction).",
            settings.database_host,
        )

    if settings.auto_create_schema:
        try:
            Base.metadata.create_all(engine)
        except OperationalError as exc:
            logger.error(
                "app.database.unavailable host=%s error=%s. Check DATABASE_URL "
                "(host, project ref, password, sslmode=require) and that the database "
                "is reachable from this environment.",
                settings.database_host,
                exc.orig,
            )
            raise
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            seed_all(db)
            db.commit()
        logger.info("app.schema.ready mode=create_all")
    else:
        logger.info("app.schema.ready mode=alembic")

    warnings = settings.validate_production()
    for warning in warnings:
        logger.warning("app.config.warning %s", warning)

    # A worker restart cannot resume an in-flight evaluation. Fail any orphaned
    # run so the dashboard stops polling and shows a terminal state.
    from app.evaluation.harness import recover_orphaned_runs

    recovered = recover_orphaned_runs()
    if recovered:
        logger.warning("app.eval.recovered_orphans count=%s", recovered)

    # Prime the policy retriever once instead of on the first user request.
    from app.services.policies import get_policy_retriever

    retriever = get_policy_retriever()
    logger.info(
        "app.started provider=%s policy_backend=%s policies=%s",
        settings.effective_llm_provider(),
        retriever.backend,
        len(retriever.documents),
    )
    yield
    logger.info("app.stopped")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "KYC Copilot: live screen context, guarded tool execution, PII tokenisation "
        "and a prompt-version evaluation harness. All external integrations are simulated."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    routes_system.router,
    routes_sessions.router,
    routes_screen.router,
    routes_agent.router,
    routes_kyc.router,
    routes_audit.router,
    routes_eval.router,
):
    app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("app.unhandled path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": "internal_error",
                "message": "The copilot hit an unexpected error. Nothing was changed.",
            }
        },
    )


@app.get("/", tags=["system"])
def root() -> dict[str, Any]:
    return {
        "name": settings.app_name,
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "evaluation": "/api/eval/runs",
        "simulated": True,
    }
