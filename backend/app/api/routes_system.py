from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.agents.llm import provider_health
from app.api.deps import get_db, require_admin
from app.core.config import settings
from app.observability.metrics import snapshot
from app.schemas import KillSwitchRequest
from app.services.cache import cache_health
from app.services.kill_switch import get_kill_switch, set_kill_switch
from app.services.policies import get_policy_retriever

router = APIRouter(tags=["system"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    retriever = get_policy_retriever()
    kill = get_kill_switch(db)
    return {
        "status": "ok" if db_ok else "degraded",
        "version": "1.0.0",
        "time": datetime.now(UTC).isoformat(),
        "checks": {
            "database": {"ok": db_ok, "driver": settings.database_url.split(":")[0]},
            "cache": cache_health(),
            "policy_store": {"backend": retriever.backend, "documents": len(retriever.documents)},
            "llm": provider_health(),
        },
        "kill_switch": kill,
        "simulated_integrations": ["bank_mandate_gateway", "ocr", "pan_registry", "address_geocoder"],
        "secrets_configured": {
            "gemini": bool(settings.gemini_api_key),
            "groq": bool(settings.groq_api_key),
            "redis": bool(settings.redis_url),
            "admin_token_custom": settings.admin_token != "dev-admin-token",
        },
    }


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)) -> dict[str, Any]:
    counter = db.execute(text("SELECT COUNT(*) FROM agent_runs")).scalar() if _has_table(db, "agent_runs") else 0
    data = snapshot()
    data["agent_runs_total"] = int(counter or 0)
    data["kill_switch"] = get_kill_switch(db)
    return data


@router.post("/admin/kill-switch")
def kill_switch(payload: KillSwitchRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    require_admin(x_admin_token=payload.admin_token)
    state = set_kill_switch(db, enabled=payload.enabled, reason=payload.reason, actor="admin")
    db.commit()
    return {"ok": True, **state}


@router.get("/admin/kill-switch")
def read_kill_switch(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_kill_switch(db)


def _has_table(db: Session, name: str) -> bool:
    try:
        return bool(db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}).first())
    except Exception:
        try:
            return bool(db.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar())
        except Exception:
            return False


__all__ = ["router", "func", "select"]
