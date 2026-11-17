from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_runtime_helpers import rehydrate_event
from app.agents.runner import start_agent_run
from app.api.deps import get_db, get_session_or_404
from app.core.logging import get_logger
from app.models.entities import AgentAction, AgentRun, AuditLog
from app.observability.metrics import incr
from app.schemas import (
    ActionResultRequest,
    ActionResultResponse,
    ActionValueResponse,
    AgentRunRequest,
    AgentRunResponse,
)
from app.services.actions import ActionProtocolError, apply_action_result, decrypt_action_value
from app.services.events import hub, sse_format
from app.services.journey import latest_screen_state
from app.services.kill_switch import is_killed
from app.services.pii import PIIVault

logger = get_logger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

TERMINAL_STREAM_EVENTS = {"agent_complete", "stream_end", "error"}


@router.post("/run", response_model=AgentRunResponse)
def run_agent(payload: AgentRunRequest, db: Session = Depends(get_db)) -> AgentRunResponse:
    session = get_session_or_404(db, payload.session_id)
    if latest_screen_state(db, session.id) is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "no_screen_context",
                "message": "The app has not shared the current screen yet. Reload the KYC page and try again.",
            },
        )
    if is_killed(db):
        raise HTTPException(
            status_code=503,
            detail={"code": "kill_switch", "message": "Agent actions are temporarily paused by an operator."},
        )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - sync test client
        loop = None
    run = start_agent_run(
        db,
        session_id=session.id,
        message=payload.message,
        prompt_version=payload.prompt_version,
        loop=loop,
    )
    return AgentRunResponse(
        run_id=run.id,
        session_id=session.id,
        status=run.status,
        stream_url=f"/api/agent/stream/{run.id}?session_id={session.id}",
    )


@router.get("/stream/{run_id}")
async def stream_run(run_id: str, session_id: str, request: Request) -> StreamingResponse:
    from app.db.session import SessionLocal

    db = SessionLocal()
    run = db.get(AgentRun, run_id)
    if run is None or run.session_id != session_id:
        db.close()
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": "Agent run not found"})
    vault = PIIVault(db, session_id, create_if_missing=False)

    async def event_stream():
        loop = asyncio.get_running_loop()
        queue, history = hub.subscribe(run_id, loop)
        if not history:
            history = list(run.events or [])
        finished = run.status in {"completed", "completed_with_errors", "failed", "stopped"}
        try:
            yield "retry: 3000\n\n"
            for event in history:
                yield sse_format(rehydrate_event(event, vault))
                if event.get("event") == "agent_complete":
                    finished = True
            if finished:
                yield sse_format({"event": "stream_end", "data": {"reason": "finished"}})
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield sse_format(rehydrate_event(event, vault))
                if event.get("event") in {"agent_complete", "stream_end"}:
                    break
        finally:
            hub.unsubscribe(run_id, queue)
            db.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/action-result", response_model=ActionResultResponse)
def action_result(payload: ActionResultRequest, db: Session = Depends(get_db)) -> ActionResultResponse:
    session = get_session_or_404(db, payload.session_id)
    run = db.get(AgentRun, payload.run_id)
    if run is None or run.session_id != session.id:
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": "Agent run not found"})
    action = db.get(AgentAction, payload.action_id)
    if action is None or action.run_id != run.id:
        raise HTTPException(status_code=404, detail={"code": "action_not_found", "message": "Action not found"})
    try:
        status, run_status = apply_action_result(
            db,
            run=run,
            action=action,
            session=session,
            status=payload.status,
            detail=payload.detail,
            executed_value_hash=payload.executed_value_hash,
            actor="user",
        )
    except ActionProtocolError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "action_rejected", "message": str(exc)}) from exc
    db.commit()
    return ActionResultResponse(
        ok=status == "applied",
        run_status=run_status,
        action_status=status,
        message="Action recorded." if status == "applied" else f"Action {status}.",
    )


@router.get("/actions/{action_id}/value", response_model=ActionValueResponse)
def action_value(action_id: str, session_id: str, db: Session = Depends(get_db)) -> ActionValueResponse:
    get_session_or_404(db, session_id)
    action = db.get(AgentAction, action_id)
    if action is None or action.session_id != session_id:
        raise HTTPException(status_code=404, detail={"code": "action_not_found", "message": "Action not found"})
    if action.action_type != "fill":
        raise HTTPException(status_code=400, detail={"code": "no_value", "message": "Action has no value payload"})
    if action.status not in {"dispatched", "applied", "pending_confirmation"}:
        raise HTTPException(status_code=409, detail={"code": "action_not_active", "message": "Action is no longer active"})
    try:
        value = decrypt_action_value(action)
    except Exception as exc:  # pragma: no cover - corrupted payload
        raise HTTPException(status_code=500, detail={"code": "decrypt_failed", "message": "Value unavailable"}) from exc
    incr("agent.action.value_served")
    return ActionValueResponse(
        action_id=action.id,
        field=action.field,
        value=value,
        value_hash=action.value_hash,
    )


@router.get("/runs/{run_id}")
def read_run(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": "Agent run not found"})
    actions = db.scalars(
        select(AgentAction).where(AgentAction.run_id == run.id).order_by(AgentAction.created_at)
    ).all()
    audits = db.scalars(
        select(AuditLog).where(AuditLog.run_id == run.id).order_by(AuditLog.created_at)
    ).all()
    return {
        "run": {
            "id": run.id,
            "session_id": run.session_id,
            "screen_id": run.screen_id,
            "status": run.status,
            "prompt_version": run.prompt_version,
            "provider": run.provider,
            "model": run.model,
            "user_message_masked": run.user_message_masked,
            "plan_masked": run.plan_masked,
            "say_masked": run.say_masked,
            "confidence": run.confidence,
            "requires_confirmation": run.requires_confirmation,
            "confirmation_prompt_masked": run.confirmation_prompt_masked,
            "cited_policy_refs": run.cited_policy_refs,
            "retrieved_policies": run.retrieved_policies,
            "context_masked": run.context_masked,
            "events": run.events,
            "hallucination_flags": run.hallucination_flags,
            "latency_ms": run.latency_ms,
            "llm_latency_ms": run.llm_latency_ms,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "cost_usd": run.cost_usd,
            "error": run.error,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        },
        "actions": [
            {
                "id": a.id,
                "type": a.action_type,
                "field": a.field,
                "target": a.target,
                "status": a.status,
                "requires_confirmation": a.requires_confirmation,
                "sensitive": a.is_sensitive,
                "value_preview": a.value_masked,
                "reason": a.reason,
                "result": a.result,
                "error": a.error,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "executed_at": a.executed_at.isoformat() if a.executed_at else None,
            }
            for a in actions
        ],
        "audit": [
            {
                "id": a.id,
                "type": a.event_type,
                "actor": a.actor,
                "severity": a.severity,
                "summary": a.summary,
                "detail": a.detail,
                "created_at": a.created_at.isoformat(),
            }
            for a in audits
        ],
    }
