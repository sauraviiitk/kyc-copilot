"""Client action execution protocol.

The backend never touches the DOM. It emits an action, the client executes it,
and the client posts the result here. This module verifies the claim before it
is recorded, so a compromised or buggy client cannot silently "confirm" a
different value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.entities import AgentAction, AgentRun, MandateRecord, UserSession
from app.services.audit import record_audit
from app.services.events import hub
from app.services.pii import fernet

logger = get_logger(__name__)

TERMINAL = {"applied", "failed", "rejected", "skipped", "cancelled"}


class ActionProtocolError(ValueError):
    pass


def decrypt_action_value(action: AgentAction) -> str:
    if not action.value_ciphertext:
        raise ActionProtocolError("action has no value payload")
    return fernet().decrypt(action.value_ciphertext.encode()).decode()


def _emit(run_id: str, event: str, data: dict[str, Any]) -> None:
    hub.publish(run_id, {"event": event, "data": data})


def _remaining_actions(db: Session, run_id: str) -> list[AgentAction]:
    return list(
        db.scalars(
            select(AgentAction).where(
                AgentAction.run_id == run_id,
                AgentAction.status.in_(["dispatched", "pending_confirmation"]),
            )
        ).all()
    )


def evaluate_run_status(db: Session, run: AgentRun) -> str:
    remaining = _remaining_actions(db, run.id)
    if any(a.status == "pending_confirmation" for a in remaining):
        return "awaiting_confirmation"
    if remaining:
        return "awaiting_result"
    actions = db.scalars(select(AgentAction).where(AgentAction.run_id == run.id)).all()
    if not actions:
        return "completed"
    if any(a.status in {"failed", "rejected"} for a in actions):
        return "completed_with_errors"
    return "completed"


def _verify_retry(db: Session, action: AgentAction, detail: dict[str, Any]) -> tuple[bool, str]:
    mandate_ref = str(detail.get("mandate_ref") or action.result.get("mandate_ref") or "")
    record = db.scalar(select(MandateRecord).where(MandateRecord.mandate_ref == mandate_ref)) if mandate_ref else None
    if record is None:
        return False, "retry result does not match a known mandate"
    if record.session_id != action.session_id:
        return False, "retry result belongs to a different session"
    # The action is only valid if the server actually executed a new attempt.
    expected_min = int(action.result.get("attempts") or 0) + 1
    if record.attempts < expected_min:
        return False, "no new mandate attempt was recorded by the server"
    if record.status == "unknown":
        return False, "unknown transaction status must not be retried"
    return True, ""


def apply_action_result(
    db: Session,
    *,
    run: AgentRun,
    action: AgentAction,
    session: UserSession,
    status: str,
    detail: dict[str, Any] | None = None,
    executed_value_hash: str | None = None,
    actor: str = "user",
) -> tuple[str, str]:
    detail = detail or {}
    if action.session_id != session.id or run.session_id != session.id:
        raise ActionProtocolError("action does not belong to this session")
    if action.status in TERMINAL:
        # Idempotent replay of a duplicate result post.
        return action.status, run.status

    final_status = status
    error: str | None = None

    if status == "applied":
        if action.action_type == "fill":
            if action.value_hash and executed_value_hash and executed_value_hash != action.value_hash:
                final_status, error = "failed", "executed value did not match the authorised value"
            elif action.value_hash and not executed_value_hash:
                final_status, error = "failed", "client did not attest the executed value"
        elif action.action_type == "retry":
            ok, reason = _verify_retry(db, action, detail)
            if not ok:
                final_status, error = "failed", reason
        elif action.action_type == "navigate":
            allowed = {"kyc.pan_entry", "kyc.document_upload", "kyc.bank_mandate", "kyc.address", "kyc.review"}
            if detail.get("screen_id") not in allowed and action.target not in allowed:
                final_status, error = "failed", "navigation target not allowed"

    action.status = final_status
    action.error = error
    action.executed_at = datetime.now(UTC)
    action.result = {**(action.result or {}), **detail, "reported_status": status}

    if action.action_type in {"fill", "upload", "retry"}:
        session.updated_at = datetime.now(UTC)

    # SessionLocal uses autoflush=False, so the status change must be flushed
    # before it can be observed by evaluate_run_status below.
    db.flush()
    run.status = evaluate_run_status(db, run)
    if run.status in {"completed", "completed_with_errors"}:
        run.completed_at = datetime.now(UTC)

    record_audit(
        db,
        event_type=f"agent.action.{final_status}",
        summary=f"Action {action.action_type} {action.field or action.target or ''} -> {final_status}".strip(),
        session_id=session.id,
        run_id=run.id,
        actor=actor,
        severity="warning" if final_status == "failed" else "info",
        detail={
            "action_id": action.id,
            "type": action.action_type,
            "field": action.field,
            "target": action.target,
            "error": error,
            **{k: v for k, v in detail.items() if k in {"mandate_ref", "screen_id", "attempts", "message"}},
        },
    )
    db.flush()

    _emit(
        run.id,
        "action_result",
        {
            "action_id": action.id,
            "type": action.action_type,
            "status": final_status,
            "error": error,
            "run_status": run.status,
        },
    )
    if run.status in {"completed", "completed_with_errors"}:
        _emit(run.id, "agent_status", {"status": run.status})
        hub.close(run.id)
    return final_status, run.status


def resolve_mandate_confirmation(db: Session, session: UserSession, record: MandateRecord) -> int:
    """Treat a user-initiated mandate retry as the confirmation for a proposed action."""
    runs = db.scalars(
        select(AgentRun).where(
            AgentRun.session_id == session.id,
            AgentRun.status.in_(["awaiting_confirmation", "awaiting_result"]),
        )
    ).all()
    resolved = 0
    for run in runs:
        actions = db.scalars(
            select(AgentAction).where(
                AgentAction.run_id == run.id,
                AgentAction.action_type == "retry",
                AgentAction.status == "pending_confirmation",
            )
        ).all()
        for action in actions:
            if action.idempotency_key != record.idempotency_key:
                continue
            apply_action_result(
                db,
                run=run,
                action=action,
                session=session,
                status="applied",
                detail={"mandate_ref": record.mandate_ref, "attempts": record.attempts},
                actor="user",
            )
            resolved += 1
    return resolved


def finalize_pending_runs_for_session(
    db: Session,
    session: UserSession,
    *,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> int:
    """Complete runs that were waiting on a manual user confirmation.

    Called when the user submits something themselves (e.g. clicks Verify PAN),
    which is the confirmation the copilot was waiting for.
    """
    runs = db.scalars(
        select(AgentRun).where(
            AgentRun.session_id == session.id,
            AgentRun.status.in_(["awaiting_confirmation", "awaiting_result"]),
        )
    ).all()
    count = 0
    for run in runs:
        for action in db.scalars(
            select(AgentAction).where(
                AgentAction.run_id == run.id, AgentAction.status == "dispatched"
            )
        ).all():
            action.status = "applied"
            action.executed_at = datetime.now(UTC)
            action.result = {**(action.result or {}), "resolved_by": reason, **(detail or {})}
        run.status = "completed"
        run.completed_at = datetime.now(UTC)
        record_audit(
            db,
            event_type="agent.run.confirmed",
            summary=f"Run confirmed by user action: {reason}",
            session_id=session.id,
            run_id=run.id,
            actor="user",
            detail=detail or {},
        )
        _emit(run.id, "agent_status", {"status": "completed", "reason": reason})
        hub.close(run.id)
        count += 1
    db.flush()
    return count
