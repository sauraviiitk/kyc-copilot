"""Kill switch: stops all new agent actions immediately.

State is persisted in `system_flags` so it survives restarts and is shared
across workers. Every agent node checks it; running runs stop at the next node
and emit a terminal `agent_complete` event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import SystemFlag
from app.services.audit import record_audit
from app.services.cache import get_cache

FLAG_KEY = "kill_switch"
CACHE_KEY = "system:kill_switch"


def _cache():
    return get_cache()


def get_kill_switch(db: Session) -> dict[str, Any]:
    cached = _cache().get_json(CACHE_KEY)
    if cached is not None:
        return cached
    flag = db.get(SystemFlag, FLAG_KEY)
    state = flag.value if flag else {
        "enabled": settings.kill_switch_default,
        "reason": "default",
        "updated_at": None,
    }
    _cache().set_json(CACHE_KEY, state, ttl_seconds=5)
    return state


def is_killed(db: Session) -> bool:
    return bool(get_kill_switch(db).get("enabled"))


def set_kill_switch(db: Session, *, enabled: bool, reason: str, actor: str = "admin") -> dict[str, Any]:
    state = {
        "enabled": enabled,
        "reason": reason,
        "updated_at": datetime.now(UTC).isoformat(),
        "actor": actor,
    }
    flag = db.get(SystemFlag, FLAG_KEY)
    if flag is None:
        flag = SystemFlag(key=FLAG_KEY, value=state)
        db.add(flag)
    else:
        flag.value = state
    db.flush()
    _cache().set_json(CACHE_KEY, state, ttl_seconds=5)
    record_audit(
        db,
        event_type="kill_switch.changed",
        summary=f"Kill switch {'enabled' if enabled else 'disabled'}: {reason}",
        actor=actor,
        severity="critical" if enabled else "info",
        detail={"enabled": enabled, "reason": reason},
    )
    return state
