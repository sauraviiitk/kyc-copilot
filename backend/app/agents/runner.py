"""Agent run orchestration: creation, background execution, outcomes."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.graph import AgentRuntime, build_graph
from app.core.logging import get_logger, run_id_ctx
from app.db.session import session_scope
from app.models.entities import AgentAction, AgentRun, UserSession
from app.observability.metrics import incr
from app.services.audit import record_audit
from app.services.events import hub

logger = get_logger(__name__)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-run")


@dataclass
class RunOutcome:
    run_id: str
    status: str
    actions: list[AgentAction] = field(default_factory=list)
    hallucination_flags: list[dict[str, Any]] = field(default_factory=list)
    cited_policy_refs: list[str] = field(default_factory=list)
    retrieved_policies: list[dict[str, Any]] = field(default_factory=list)
    provider: str = "mock"
    model: str = ""
    error: str | None = None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    degraded: bool = False
    say_masked: str = ""
    confirmation_prompt_masked: str | None = None

    @property
    def action_names(self) -> list[str]:
        names: list[str] = []
        for action in self.actions:
            if action.field:
                names.append(f"{action.action_type}:{action.field}")
            elif action.target:
                names.append(f"{action.action_type}:{action.target}")
            else:
                names.append(action.action_type)
        return names


def create_run(
    db: Session,
    *,
    session_id: str,
    message: str,
    prompt_version: str = "v2",
) -> AgentRun:
    run = AgentRun(
        session_id=session_id,
        prompt_version=prompt_version,
        status="queued",
        user_message_masked="",
    )
    db.add(run)
    db.flush()
    return run


def execute_run(
    db: Session,
    *,
    session: UserSession,
    run: AgentRun,
    message: str,
    prompt_version: str = "v2",
) -> RunOutcome:
    run_id_ctx.set(run.id)
    runtime = AgentRuntime(db, session, run, message=message, prompt_version=prompt_version)
    record_audit(
        db,
        event_type="agent.run.started",
        summary="Agent run started",
        session_id=session.id,
        run_id=run.id,
        actor="agent",
        detail={"prompt_version": prompt_version, "screen_id": session.current_screen_id},
    )
    runtime.status("analyzing")
    graph = build_graph(runtime)
    try:
        graph.invoke({"status": "queued", "plan_attempts": 0})
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("agent.run.failed")
        run.status = "failed"
        run.error = str(exc)[:500]
        runtime.emit("error", {"message": "The copilot hit an internal error. Nothing was changed.", "run_id": run.id})
        runtime.emit("agent_complete", {"status": "failed", "run_id": run.id})
        runtime.finish(status="failed", error=run.error)
        record_audit(
            db,
            event_type="agent.run.failed",
            summary="Agent run failed",
            session_id=session.id,
            run_id=run.id,
            actor="agent",
            severity="error",
            detail={"error": str(exc)[:300]},
        )
        hub.close(run.id)

    db.flush()
    actions = list(
        db.scalars(select(AgentAction).where(AgentAction.run_id == run.id).order_by(AgentAction.created_at)).all()
    )
    outcome = RunOutcome(
        run_id=run.id,
        status=run.status,
        actions=actions,
        hallucination_flags=list(run.hallucination_flags or []),
        cited_policy_refs=list(run.cited_policy_refs or []),
        retrieved_policies=list(run.retrieved_policies or []),
        provider=run.provider,
        model=run.model,
        error=run.error,
        latency_ms=run.latency_ms,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cost_usd=run.cost_usd,
        say_masked=run.say_masked,
        confirmation_prompt_masked=run.confirmation_prompt_masked,
    )
    return outcome


def start_agent_run(
    db: Session,
    *,
    session_id: str,
    message: str,
    prompt_version: str = "v2",
    loop: asyncio.AbstractEventLoop | None = None,
) -> AgentRun:
    session = db.get(UserSession, session_id)
    if session is None:
        raise LookupError("session not found")
    run = create_run(db, session_id=session_id, message=message, prompt_version=prompt_version)
    db.commit()
    hub.register(run.id, loop)
    incr("agent.runs.started", version=prompt_version)
    _executor.submit(_run_worker, run.id, message, prompt_version)
    return run


def _run_worker(run_id: str, message: str, prompt_version: str) -> None:
    try:
        with session_scope() as db:
            run = db.get(AgentRun, run_id)
            if run is None:
                return
            session = db.get(UserSession, run.session_id)
            if session is None:
                return
            execute_run(db, session=session, run=run, message=message, prompt_version=prompt_version)
    except Exception:  # pragma: no cover - last-resort guard
        logger.exception("agent.worker.crashed run_id=%s", run_id)
        hub.publish(run_id, {"event": "error", "data": {"message": "Agent worker crashed"}})
        hub.close(run_id)
    finally:
        run_id_ctx.set(None)
