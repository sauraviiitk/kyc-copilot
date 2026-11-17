from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.entities import AgentAction, AgentRun, AuditLog, HumanEscalation

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
def list_audit(
    session_id: str | None = None,
    run_id: str | None = None,
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if session_id:
        query = query.where(AuditLog.session_id == session_id)
    if run_id:
        query = query.where(AuditLog.run_id == run_id)
    rows = db.scalars(query).all()
    return {
        "entries": [
            {
                "id": row.id,
                "session_id": row.session_id,
                "run_id": row.run_id,
                "actor": row.actor,
                "type": row.event_type,
                "severity": row.severity,
                "summary": row.summary,
                "detail": row.detail,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.get("/{run_id}")
def audit_for_run(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": "Run not found"})
    entries = db.scalars(
        select(AuditLog).where(AuditLog.run_id == run_id).order_by(AuditLog.created_at)
    ).all()
    actions = db.scalars(
        select(AgentAction).where(AgentAction.run_id == run_id).order_by(AgentAction.created_at)
    ).all()
    escalations = db.scalars(
        select(HumanEscalation).where(HumanEscalation.run_id == run_id)
    ).all()
    return {
        "run_id": run_id,
        "status": run.status,
        "entries": [
            {
                "id": row.id,
                "actor": row.actor,
                "type": row.event_type,
                "severity": row.severity,
                "summary": row.summary,
                "detail": row.detail,
                "created_at": row.created_at.isoformat(),
            }
            for row in entries
        ],
        "actions": [
            {
                "id": action.id,
                "type": action.action_type,
                "field": action.field,
                "status": action.status,
                "reason": action.reason,
                "value_preview": action.value_masked,
                "created_at": action.created_at.isoformat(),
            }
            for action in actions
        ],
        "escalations": [
            {
                "id": esc.id,
                "reason": esc.reason,
                "status": esc.status,
                "priority": esc.priority,
                "created_at": esc.created_at.isoformat(),
            }
            for esc in escalations
        ],
    }


@router.get("/escalations/all")
def list_escalations(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(HumanEscalation).order_by(HumanEscalation.created_at.desc()).limit(100)).all()
    return {
        "escalations": [
            {
                "id": row.id,
                "session_id": row.session_id,
                "run_id": row.run_id,
                "reason": row.reason,
                "status": row.status,
                "priority": row.priority,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }
