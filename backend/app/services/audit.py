"""Audit trail. Every mutation and every agent decision is recorded here.

Summaries and details are scrubbed defensively even though callers should pass
masked data.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger, redact_obj
from app.models.entities import AuditLog

logger = get_logger(__name__)


def record_audit(
    db: Session,
    *,
    event_type: str,
    summary: str,
    session_id: str | None = None,
    run_id: str | None = None,
    actor: str = "system",
    severity: str = "info",
    detail: dict[str, Any] | None = None,
    trace_id: str | None = None,
    flush: bool = True,
) -> AuditLog:
    entry = AuditLog(
        session_id=session_id,
        run_id=run_id,
        actor=actor,
        event_type=event_type,
        severity=severity,
        summary=redact_obj(summary),
        detail=redact_obj(detail or {}),
        trace_id=trace_id,
    )
    db.add(entry)
    if flush:
        db.flush()
    logger.info(
        f"audit.{event_type}",
        extra={
            "event": event_type,
            "session_id": session_id,
            "run_id": run_id,
            "extra": redact_obj(detail or {}),
        },
    )
    return entry
