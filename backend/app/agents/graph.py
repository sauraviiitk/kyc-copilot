"""LangGraph agent workflow.

    guard -> load_context -> retrieve_policy -> plan -> validate -> dispatch

`validate` can loop back to `plan` at most `max_planning_attempts - 1` times
(used only when the agent explicitly asks for another policy search). There is
no unbounded tool loop: tool calls per run are capped and every value passes the
deterministic anti-hallucination gate.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.llm import LLMError, PlanRequest, get_provider
from app.agents.tools import (
    ToolContext,
    action_summary,
    execute_tool_calls,
    serialise_action,
    tool_catalog,
)
from app.core.config import settings
from app.core.logging import get_logger
from app.models.entities import AgentAction, AgentRun, UserSession
from app.observability.metrics import estimate_cost, gauge, incr, observe
from app.schemas import AgentDecision
from app.services.audit import record_audit
from app.services.events import hub
from app.services.kill_switch import is_killed
from app.services.pii import PIIVault
from app.services.policies import search_policies

logger = get_logger(__name__)

# Per-screen vocabulary added to the retrieval query. Without it, short user
# messages ("help me finish this") retrieve unrelated policy documents, which
# makes grounding flaky for both the real model and the offline planner.
POLICY_QUERY_HINTS: dict[str, str] = {
    "kyc.pan_entry": "pan format uppercase verification consent attempts",
    "kyc.document_upload": "document quality ocr confidence blur rotation retry escalation",
    "kyc.bank_mandate": "mandate retry timeout idempotency unknown attempts escalation",
    "kyc.address": "address pin code city state validation suggestion consent",
    "kyc.review": "consent verification escalation",
}

# Intent vocabulary. A short message like "talk to a human" or "just skip KYC"
# carries almost no lexical overlap with the policy corpus, so the matched
# concept words are appended explicitly. This keeps retrieval deterministic for
# both the real model and the offline planner.
MESSAGE_QUERY_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("skip", "bypass", "fake", "shortcut", "without kyc", "approve my loan"),
        "bypass skip refusal prohibited out-of-scope",
    ),
    (
        ("human", "representative", "support agent", "talk to", "escalate"),
        "escalation human support handoff",
    ),
    (
        ("interest", "rate", "loan", "emi", "tax", "legal", "advice", "invest", "insurance"),
        "out-of-scope financial legal tax advice refusal",
    ),
    (
        ("make one up", "make up", "invent", "guess", "don't know"),
        "never invent address values user input validation",
    ),
)


def message_query_hints(message: str) -> str:
    lowered = (message or "").lower()
    matched: list[str] = []
    for keywords, expansion in MESSAGE_QUERY_HINTS:
        if any(keyword in lowered for keyword in keywords):
            matched.append(expansion)
    return " ".join(matched)


class AgentState(TypedDict, total=False):
    status: str
    error: str | None
    message_masked: str
    context_masked: dict[str, Any]
    context_raw: dict[str, Any]
    policies: list[dict[str, Any]]
    policy_refs: list[str]
    decision: dict[str, Any] | None
    plan_attempts: int
    replan: bool
    tool_results: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    hallucination_flags: list[dict[str, Any]]
    degraded: bool
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    llm_latency_ms: int
    requires_confirmation: bool
    confirmation_prompt: str | None
    stopped: bool


class AgentRuntime:
    """Mutable per-run context shared by the graph nodes."""

    def __init__(
        self,
        db: Session,
        session: UserSession,
        run: AgentRun,
        *,
        message: str,
        prompt_version: str,
    ):
        self.db = db
        self.session = session
        self.run = run
        self.message = message
        self.prompt_version = prompt_version
        self.vault = PIIVault(db, session.id)
        self.started = time.perf_counter()

    # -- events ------------------------------------------------------------
    def emit(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data}
        events = list(self.run.events or [])
        events.append({**payload, "ts": datetime.now(UTC).isoformat()})
        self.run.events = events
        hub.publish(self.run.id, payload)

    def status(self, status: str, detail: str | None = None) -> None:
        self.emit("agent_status", {"status": status, "detail": detail} if detail else {"status": status})

    # -- helpers -----------------------------------------------------------
    def add_hallucination(self, entry: dict[str, Any]) -> None:
        self.run.hallucination_flags = list(self.run.hallucination_flags or []) + [entry]

    def finish(self, *, status: str, error: str | None = None) -> None:
        self.run.status = status
        self.run.error = error
        self.run.latency_ms = int((time.perf_counter() - self.started) * 1000)
        self.run.output_tokens = self.run.output_tokens or 0
        self.run.cost_usd = estimate_cost(self.run.input_tokens or 0, self.run.output_tokens or 0)
        if status in {"completed", "completed_with_errors", "failed", "stopped"}:
            self.run.completed_at = datetime.now(UTC)
        observe("agent.run.latency_ms", self.run.latency_ms, version=self.prompt_version)
        incr("agent.runs", status=status, version=self.prompt_version)
        gauge("agent.last_latency_ms", self.run.latency_ms)


def build_graph(runtime: AgentRuntime):
    def guard(state: AgentState) -> AgentState:
        runtime.status("analyzing")
        if is_killed(runtime.db):
            runtime.status("stopped", "kill switch enabled")
            runtime.emit(
                "agent_complete",
                {"status": "stopped", "run_id": runtime.run.id, "reason": "kill_switch"},
            )
            return {"status": "stopped", "stopped": True, "error": "Agent actions are paused by an operator."}
        if runtime.session.status == "completed":
            payload = {
                "run_id": runtime.run.id,
                "status": "completed",
                "confidence": 0.99,
                "requires_confirmation": False,
                "confirmation_prompt": None,
                "cited_policy_refs": [],
                "actions": [],
                "degraded": False,
            }
            runtime.run.plan_masked = "Journey already complete."
            runtime.run.say_masked = "Your KYC journey is already complete. Nothing further is needed."
            runtime.run.status = "completed"
            runtime.emit("agent_message", {"say": runtime.run.say_masked, "masked": True})
            runtime.emit("agent_complete", payload)
            runtime.finish(status="completed")
            return {"status": "completed", "stopped": True, "error": "This KYC journey is already complete."}
        if runtime.session.status == "escalated":
            runtime.status("escalated", "session already escalated")
        return {"status": "running", "plan_attempts": 0}

    def load_context(state: AgentState) -> AgentState:
        from app.services.journey import get_agent_screen_context, get_screen_context

        masked = get_agent_screen_context(runtime.db, runtime.session.id)
        if masked is None:
            runtime.status("failed", "no screen context")
            runtime.emit(
                "agent_complete",
                {"status": "failed", "run_id": runtime.run.id, "reason": "no_screen_context"},
            )
            runtime.finish(status="failed", error="No screen context has been published for this session.")
            return {"status": "failed", "error": "no_screen_context"}

        raw = get_screen_context(runtime.db, runtime.session.id) or masked
        runtime.run.context_masked = masked
        runtime.run.screen_id = masked.get("screen_id", "")
        state["context_masked"] = masked
        state["context_raw"] = raw
        state["message_masked"] = runtime.vault.mask_message(runtime.message)
        runtime.run.user_message_masked = state["message_masked"]
        runtime.emit(
            "context",
            {
                "screen_id": masked.get("screen_id"),
                "step": masked.get("step"),
                "field_count": len(masked.get("fields", [])),
                "errors": masked.get("errors", []),
                "context": masked,
            },
        )
        return state

    def retrieve(state: AgentState) -> AgentState:
        runtime.status("retrieving_policy")
        context = state["context_masked"]
        errors = " ".join(str(e) for e in context.get("errors", []))
        field_names = " ".join(str(f.get("name")) for f in context.get("fields", []))
        document = context.get("document") or {}
        doc_issues = " ".join(str(i) for i in document.get("issues", []))
        screen_id = context.get("screen_id", "")
        screen_hint = POLICY_QUERY_HINTS.get(screen_id, "")
        intent_hint = message_query_hints(state["message_masked"])
        query = (
            f"{state['message_masked']} {intent_hint} {screen_id} {screen_hint} {errors} {field_names} "
            f"{doc_issues} {json_safe(context.get('policy_refs'))}"
        )
        hits = [hit.to_dict() for hit in search_policies(query, k=4)]
        state["policies"] = hits
        state["policy_refs"] = [hit["ref"] for hit in hits]
        runtime.run.retrieved_policies = hits
        runtime.emit(
            "policy",
            {
                "refs": state["policy_refs"],
                "hits": [{"ref": h["ref"], "title": h["title"], "score": h.get("score")} for h in hits],
            },
        )
        if not hits:
            runtime.emit("agent_status", {"status": "policy_missing"})
        return state

    def plan(state: AgentState) -> AgentState:
        runtime.status("planning")
        version = runtime.prompt_version
        attempt = int(state.get("plan_attempts", 0)) + 1
        provider = get_provider(version)
        request = PlanRequest(
            session_id=runtime.session.id,
            run_id=runtime.run.id,
            message_masked=state["message_masked"],
            raw_message=runtime.message,
            screen_context=state["context_masked"],
            policies=state.get("policies", []),
            prompt_version=version,
            limits=tool_catalog(state["context_masked"].get("screen_id", "")),
            raw_context=state.get("context_raw", {}),
        )
        degraded = bool(state.get("degraded"))
        try:
            result = provider.plan(request)
        except LLMError as exc:
            logger.warning("agent.llm_fallback: %s", exc)
            from app.agents.llm import MockProvider

            result = MockProvider(version).plan(request)
            degraded = True
            runtime.emit("agent_status", {"status": "degraded", "detail": "LLM unavailable; used offline planner"})

        decision = result.decision
        state.update(
            {
                "decision": decision.model_dump(mode="json"),
                "plan_attempts": attempt,
                "degraded": degraded,
                "provider": result.provider,
                "model": result.model,
                "input_tokens": int(state.get("input_tokens", 0)) + result.input_tokens,
                "output_tokens": int(state.get("output_tokens", 0)) + result.output_tokens,
                "llm_latency_ms": int(state.get("llm_latency_ms", 0)) + result.latency_ms,
            }
        )
        runtime.run.provider = result.provider
        runtime.run.model = result.model
        runtime.run.llm_attempts = attempt
        runtime.run.llm_latency_ms = int(state["llm_latency_ms"])
        runtime.run.input_tokens = int(state["input_tokens"])
        runtime.run.output_tokens = int(state["output_tokens"])
        runtime.run.plan_masked = decision.plan
        runtime.run.confidence = decision.confidence
        observe("agent.llm_latency_ms", result.latency_ms, provider=result.provider)

        ungrounded = [ref for ref in decision.cited_policy_refs if ref not in state.get("policy_refs", [])]
        runtime.emit(
            "agent_plan",
            {
                "attempt": attempt,
                "plan": decision.plan,
                "confidence": decision.confidence,
                "cited_policy_refs": decision.cited_policy_refs,
                "ungrounded_refs": ungrounded,
                "provider": result.provider,
                "model": result.model,
                "degraded": degraded,
            },
        )
        return state

    def validate(state: AgentState) -> AgentState:
        runtime.status("validating")
        decision = AgentDecision.model_validate(state["decision"] or {})
        if is_killed(runtime.db):
            runtime.status("stopped", "kill switch enabled during run")
            runtime.emit("agent_complete", {"status": "stopped", "run_id": runtime.run.id, "reason": "kill_switch"})
            runtime.finish(status="stopped", error="Stopped by kill switch.")
            return {**state, "status": "stopped", "stopped": True}

        tool_ctx = ToolContext(
            db=runtime.db,
            session=runtime.session,
            run=runtime.run,
            screen_context_masked=state["context_masked"],
            raw_context=state.get("context_raw", {}),
            vault=runtime.vault,
            raw_message=runtime.message,
            prompt_version=runtime.prompt_version,
        )
        results = execute_tool_calls(decision.tool_calls, tool_ctx)

        actions = [r.action for r in results if r.action is not None]
        policy_hits = [hit for r in results for hit in r.policy_hits]
        hallucination = [r.detail for r in results if r.hallucination]
        for flag in hallucination:
            runtime.add_hallucination(flag)

        # Model-level escalation without an explicit tool call still creates a record.
        if decision.escalate and not any(a.action_type == "escalate" for a in actions):
            reason = decision.escalation_reason or "Agent requested human review"
            from app.agents.tools import ToolCall as _ToolCall  # local import avoids cycle

            fallback = execute_tool_calls(
                [_ToolCall(tool="escalate_to_human", args={"reason": reason})], tool_ctx
            )
            actions.extend(r.action for r in fallback if r.action is not None)

        state["tool_results"] = [
            {
                "tool": r.tool,
                "ok": r.ok,
                "error": r.error,
                "detail": r.detail,
                "action_id": r.action.id if r.action else None,
            }
            for r in results
        ]
        state["actions"] = [serialise_action(a) for a in actions]
        state["requires_confirmation"] = bool(decision.requires_confirmation)
        state["confirmation_prompt"] = decision.confirmation_prompt

        # Bounded re-plan: only when the agent asked for extra policy context.
        only_policy_search = bool(policy_hits) and not actions
        if only_policy_search and int(state.get("plan_attempts", 1)) < settings.max_planning_attempts:
            merged = {p["ref"]: p for p in state.get("policies", [])}
            for hit in policy_hits:
                merged[hit["ref"]] = hit
            state["policies"] = list(merged.values())
            state["policy_refs"] = list(merged.keys())
            runtime.run.retrieved_policies = state["policies"]
            state["replan"] = True
            return state

        state["replan"] = False
        return state

    def dispatch(state: AgentState) -> AgentState:
        runtime.status("executing")
        decision = AgentDecision.model_validate(state["decision"] or {})
        actions = list(
            runtime.db.scalars(
                select(AgentAction)
                .where(AgentAction.run_id == runtime.run.id)
                .order_by(AgentAction.created_at)
            ).all()
        )

        pending_confirmation = any(a.status == "pending_confirmation" for a in actions)
        dispatched = any(a.status == "dispatched" for a in actions)
        if pending_confirmation:
            run_status = "awaiting_confirmation"
        elif dispatched:
            run_status = "awaiting_result"
        else:
            run_status = "completed"

        for action in actions:
            runtime.emit("action", serialise_action(action))

        say = runtime.vault.rehydrate_text(decision.say)
        # Only the masked confirmation prompt is persisted/emitted; rehydrating
        # it would leak PII, so deliberately no plaintext copy is created here.
        runtime.run.say_masked = decision.say
        runtime.run.requires_confirmation = bool(decision.requires_confirmation or pending_confirmation)
        runtime.run.confirmation_prompt_masked = decision.confirmation_prompt
        runtime.run.cited_policy_refs = decision.cited_policy_refs
        runtime.run.status = run_status
        runtime.run.tool_latency_ms = max(0, runtime.run.latency_ms)

        runtime.emit("agent_message", {"say": decision.say, "masked": True})
        runtime.emit(
            "agent_complete",
            {
                "run_id": runtime.run.id,
                "status": run_status,
                "confidence": decision.confidence,
                "requires_confirmation": runtime.run.requires_confirmation,
                "confirmation_prompt": decision.confirmation_prompt,
                "cited_policy_refs": decision.cited_policy_refs,
                "actions": [action_summary(a) for a in actions],
                "degraded": bool(state.get("degraded")),
            },
        )
        runtime.finish(status="completed" if run_status == "completed" else run_status)
        runtime.run.status = run_status

        record_audit(
            runtime.db,
            event_type="agent.run.completed",
            summary=f"Agent run {run_status} with {len(actions)} action(s)",
            session_id=runtime.session.id,
            run_id=runtime.run.id,
            actor="agent",
            detail={
                "status": run_status,
                "actions": [action_summary(a) for a in actions],
                "cited_policy_refs": decision.cited_policy_refs,
                "confidence": decision.confidence,
                "degraded": bool(state.get("degraded")),
            },
        )
        if not actions:
            hub.close(runtime.run.id)
        if say:
            logger.info(
                "agent.run.finished",
                extra={"run_id": runtime.run.id, "session_id": runtime.session.id, "status": run_status},
            )
        return state

    builder = StateGraph(AgentState)
    builder.add_node("guard", guard)
    builder.add_node("load_context", load_context)
    builder.add_node("retrieve", retrieve)
    builder.add_node("plan", plan)
    builder.add_node("validate", validate)
    builder.add_node("dispatch", dispatch)

    builder.add_edge(START, "guard")
    builder.add_conditional_edges(
        "guard", lambda s: "end" if s.get("stopped") else "load", {"load": "load_context", "end": END}
    )
    builder.add_edge("load_context", "retrieve")
    builder.add_conditional_edges(
        "retrieve", lambda s: "end" if s.get("status") == "failed" else "plan", {"plan": "plan", "end": END}
    )
    builder.add_edge("plan", "validate")
    builder.add_conditional_edges(
        "validate",
        lambda s: "plan" if s.get("replan") and not s.get("stopped") else "dispatch",
        {"plan": "plan", "dispatch": "dispatch"},
    )
    builder.add_edge("dispatch", END)
    return builder.compile()


def json_safe(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except Exception:  # pragma: no cover
        return str(value)
