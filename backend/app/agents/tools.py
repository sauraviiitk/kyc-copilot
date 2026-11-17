"""Agent tools: deterministic permissions, value resolution and side effects.

This is the layer that makes "the model cannot do arbitrary things" true:

* every tool call is validated against the live screen context;
* `set_field` values must be derivable from the user's input or a suggestion list;
* rejected values are recorded as hallucination flags, never executed;
* financial retries are only *proposed* and stay pending until the user confirms;
* escalations are durable rows; policy search is read-only;
* every call produces an audit record and a bounded action row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.entities import AgentAction, AgentRun, HumanEscalation, UserSession
from app.schemas import (
    MUTABLE_FIELDS,
    SENSITIVE_FIELDS,
    ActionType,
    ToolCall,
    ToolName,
)
from app.services.audit import record_audit
from app.services.mandate import latest_mandate
from app.services.pii import PIIVault, fernet, value_hash
from app.services.policies import search_policies

logger = get_logger(__name__)

UI_TARGETS = {"upload_area", "mandate_panel", "address_form", "pan_panel", "document_preview"}
NAVIGABLE_SCREENS = {
    "kyc.pan_entry",
    "kyc.document_upload",
    "kyc.bank_mandate",
    "kyc.address",
    "kyc.review",
}


@dataclass
class ToolContext:
    db: Session
    session: UserSession
    run: AgentRun
    screen_context_masked: dict[str, Any]
    raw_context: dict[str, Any]
    vault: PIIVault
    raw_message: str
    prompt_version: str = "v2"
    user_confirmed: bool = False


@dataclass
class ToolResult:
    tool: str
    ok: bool
    action: AgentAction | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    hallucination: bool = False
    escalation_id: str | None = None
    policy_hits: list[dict[str, Any]] = field(default_factory=list)


def _field_ctx(ctx: ToolContext, name: str) -> dict[str, Any] | None:
    return next((f for f in ctx.screen_context_masked.get("fields", []) if f.get("name") == name), None)


def _raw_field_value(ctx: ToolContext, name: str) -> str | None:
    for raw_field in ctx.raw_context.get("fields", []):
        if raw_field.get("name") == name:
            return raw_field.get("value")
    return None


def _make_action(
    ctx: ToolContext,
    *,
    action_type: ActionType,
    reason: str,
    field_name: str | None = None,
    target: str | None = None,
    value: str | None = None,
    value_preview: str | None = None,
    sensitive: bool = False,
    requires_confirmation: bool = False,
    idempotency_key: str | None = None,
    step_id: str | None = None,
    status: str = "dispatched",
    detail: dict[str, Any] | None = None,
) -> AgentAction:
    ciphertext = fernet().encrypt(value.encode()).decode() if value is not None else None
    action = AgentAction(
        run_id=ctx.run.id,
        session_id=ctx.session.id,
        action_type=action_type.value,
        field=field_name,
        target=target,
        step_id=step_id,
        idempotency_key=idempotency_key,
        value_masked=value_preview,
        value_ciphertext=ciphertext,
        value_hash=value_hash(value, field_name) if value is not None else None,
        is_sensitive=sensitive,
        requires_confirmation=requires_confirmation,
        reason=reason,
        status=status,
        result=detail or {},
    )
    ctx.db.add(action)
    ctx.db.flush()
    record_audit(
        ctx.db,
        event_type="agent.action.proposed",
        summary=f"{action_type.value} {field_name or target or ''} ({action.status})".strip(),
        session_id=ctx.session.id,
        run_id=ctx.run.id,
        actor="agent",
        detail={
            "action_id": action.id,
            "type": action_type.value,
            "field": field_name,
            "target": target,
            "sensitive": sensitive,
            "requires_confirmation": requires_confirmation,
            "value_preview": value_preview,
            "reason": reason,
        },
    )
    return action


def execute_tool_calls(calls: list[ToolCall], ctx: ToolContext) -> list[ToolResult]:
    results: list[ToolResult] = []
    limit = max(0, settings.max_agent_tool_calls)
    for call in calls[:limit]:
        results.append(execute_tool_call(call, ctx))
    return results


def execute_tool_call(call: ToolCall, ctx: ToolContext) -> ToolResult:
    tool = call.tool
    args = dict(call.args or {})
    try:
        if tool == ToolName.set_field:
            return _set_field(ctx, args)
        if tool == ToolName.highlight:
            return _highlight(ctx, args)
        if tool == ToolName.upload_doc:
            return _upload_doc(ctx, args)
        if tool == ToolName.retry_step:
            return _retry_step(ctx, args)
        if tool == ToolName.navigate:
            return _navigate(ctx, args)
        if tool == ToolName.escalate_to_human:
            return _escalate(ctx, args)
        if tool == ToolName.search_policy:
            return _search_policy(ctx, args)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("tool.failed tool=%s error=%s", tool, exc)
        record_audit(
            ctx.db,
            event_type="agent.tool.failed",
            summary=f"Tool {getattr(tool, 'value', tool)} failed",
            session_id=ctx.session.id,
            run_id=ctx.run.id,
            actor="agent",
            severity="warning",
            detail={"tool": str(getattr(tool, "value", tool)), "error": str(exc)[:200]},
        )
        return ToolResult(tool=str(getattr(tool, "value", tool)), ok=False, error=str(exc)[:200])
    return ToolResult(tool=str(getattr(tool, "value", tool)), ok=False, error="unknown tool")


def _set_field(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    field_name = str(args.get("field") or "").strip()
    proposed = args.get("value")
    proposed_s = None if proposed is None else str(proposed)
    screen_id = ctx.screen_context_masked.get("screen_id", "")

    allowed = MUTABLE_FIELDS.get(screen_id, ())
    if field_name not in allowed:
        return _reject(ctx, "set_field", field_name, f"field '{field_name}' is not editable on {screen_id}")

    field = _field_ctx(ctx, field_name)
    if field is None:
        return _reject(ctx, "set_field", field_name, f"field '{field_name}' does not exist on the current screen")

    current_raw = _raw_field_value(ctx, field_name)
    options = field.get("options") or []
    resolution = ctx.vault.resolve_fill(
        field_name,
        proposed_s,
        current_value=current_raw,
        raw_user_message=ctx.raw_message,
        allowed_values=options,
    )
    if not resolution.ok:
        ctx.run.hallucination_flags = list(ctx.run.hallucination_flags or []) + [
            {
                "tool": "set_field",
                "field": field_name,
                "proposed_preview": (proposed_s or "")[:3] + "…" if proposed_s else None,
                "reason": resolution.reason,
            }
        ]
        record_audit(
            ctx.db,
            event_type="agent.action.rejected",
            summary=f"Rejected fill for {field_name}: {resolution.reason}",
            session_id=ctx.session.id,
            run_id=ctx.run.id,
            actor="agent",
            severity="warning",
            detail={"field": field_name, "reason": resolution.reason, "source": resolution.source},
        )
        return ToolResult(
            tool="set_field",
            ok=False,
            error=resolution.reason,
            hallucination=True,
            detail={"field": field_name, "reason": resolution.reason},
        )

    sensitive = field_name in SENSITIVE_FIELDS
    entry = ctx.vault.entry_for_field(field_name) if sensitive else None
    preview = entry.token if entry else (resolution.value or "")
    action = _make_action(
        ctx,
        action_type=ActionType.fill,
        field_name=field_name,
        value=resolution.value,
        value_preview=preview,
        sensitive=sensitive,
        reason=f"{resolution.reason} ({resolution.source})",
        detail={"source": resolution.source, "transform": resolution.transform},
    )
    return ToolResult(
        tool="set_field",
        ok=True,
        action=action,
        detail={
            "field": field_name,
            "source": resolution.source,
            "preview": preview,
            "transform": resolution.transform,
        },
    )


def _highlight(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    field_name = str(args.get("field") or "").strip()
    field = _field_ctx(ctx, field_name)
    if field is None and field_name not in UI_TARGETS:
        return _reject(ctx, "highlight", field_name, "unknown highlight target")
    action = _make_action(
        ctx,
        action_type=ActionType.highlight,
        field_name=field_name if field else None,
        target=None if field else field_name,
        reason=f"Highlight {field_name}",
    )
    return ToolResult(tool="highlight", ok=True, action=action, detail={"field": field_name})


def _upload_doc(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    document_id = str(args.get("document_id") or "").strip()
    from app.models.entities import DocumentRecord

    record = ctx.db.get(DocumentRecord, document_id) if document_id else None
    if record is None or record.session_id != ctx.session.id:
        return _reject(ctx, "upload_doc", document_id, "document reference not found for this session")
    action = _make_action(
        ctx,
        action_type=ActionType.upload,
        target=document_id,
        value=None,
        value_preview=f"document:{record.doc_type}",
        reason=f"Authorised re-upload flow for document {document_id}",
        detail={"document_id": document_id, "doc_type": record.doc_type},
    )
    return ToolResult(tool="upload_doc", ok=True, action=action, detail={"document_id": document_id})


def _retry_step(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    step_id = str(args.get("step_id") or "").strip()
    key = str(args.get("idempotency_key") or "").strip()
    if step_id != "kyc.bank_mandate":
        return _reject(ctx, "retry_step", step_id, "only the bank mandate step is retryable")
    mandate = latest_mandate(ctx.db, ctx.session.id)
    if mandate is None:
        return _reject(ctx, "retry_step", step_id, "no mandate exists for this session")
    if not key or key != mandate.idempotency_key:
        return _reject(ctx, "retry_step", step_id, "idempotency key does not match the active mandate")
    if mandate.status == "verified":
        return _reject(ctx, "retry_step", step_id, "mandate is already verified")
    if mandate.status == "unknown":
        return _reject(ctx, "retry_step", step_id, "unknown transaction status must not be retried")
    if mandate.attempts >= 3:
        return _reject(ctx, "retry_step", step_id, "retry limit reached")
    if mandate.status != "failed":
        return _reject(ctx, "retry_step", step_id, f"mandate status '{mandate.status}' is not retryable")

    # Financial operation: never executed by the agent. The client confirms and
    # calls POST /api/kyc/mandate/retry with this exact key.
    action = _make_action(
        ctx,
        action_type=ActionType.retry,
        target="kyc.bank_mandate",
        step_id=step_id,
        idempotency_key=key,
        requires_confirmation=True,
        status="pending_confirmation",
        reason="Safe retry of a hard-failed mandate; requires user confirmation",
        detail={"mandate_ref": mandate.mandate_ref, "attempts": mandate.attempts},
        value_preview=f"mandate:{mandate.mandate_ref}",
    )
    return ToolResult(
        tool="retry_step",
        ok=True,
        action=action,
        detail={"mandate_ref": mandate.mandate_ref, "idempotency_key": key, "attempts": mandate.attempts},
    )


def _navigate(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    screen_id = str(args.get("screen_id") or "").strip()
    if screen_id not in NAVIGABLE_SCREENS:
        return _reject(ctx, "navigate", screen_id, "unknown navigation target")
    action = _make_action(
        ctx,
        action_type=ActionType.navigate,
        target=screen_id,
        reason=f"Navigate to {screen_id}",
    )
    return ToolResult(tool="navigate", ok=True, action=action, detail={"screen_id": screen_id})


def _escalate(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return ToolResult(tool="escalate_to_human", ok=False, error="reason is required")

    existing = ctx.db.scalar(
        select(HumanEscalation).where(
            HumanEscalation.run_id == ctx.run.id,
            HumanEscalation.reason == reason,
        )
    )
    escalation = existing
    if escalation is None:
        escalation = HumanEscalation(
            session_id=ctx.session.id,
            run_id=ctx.run.id,
            reason=reason,
            priority="high" if "financial" in reason.lower() or "unknown" in reason.lower() else "normal",
            detail={"screen_id": ctx.screen_context_masked.get("screen_id")},
        )
        ctx.db.add(escalation)
        ctx.db.flush()
        ctx.session.status = "escalated"
        record_audit(
            ctx.db,
            event_type="escalation.created",
            summary=f"Escalated to human: {reason}",
            session_id=ctx.session.id,
            run_id=ctx.run.id,
            actor="agent",
            severity="warning",
            detail={"escalation_id": escalation.id, "reason": reason},
        )

    action = _make_action(
        ctx,
        action_type=ActionType.escalate,
        target="human_support",
        reason=reason,
        status="applied",
        detail={"escalation_id": escalation.id, "reason": reason},
    )
    action.executed_at = action.created_at
    return ToolResult(tool="escalate_to_human", ok=True, action=action, escalation_id=escalation.id, detail={"reason": reason})


def _search_policy(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(tool="search_policy", ok=False, error="query is required")
    hits = [hit.to_dict() for hit in search_policies(query, k=4)]
    record_audit(
        ctx.db,
        event_type="agent.policy.search",
        summary=f"Policy search: {query[:80]}",
        session_id=ctx.session.id,
        run_id=ctx.run.id,
        actor="agent",
        detail={"query": query[:120], "refs": [h["ref"] for h in hits]},
    )
    return ToolResult(tool="search_policy", ok=True, policy_hits=hits, detail={"refs": [h["ref"] for h in hits]})


def _reject(ctx: ToolContext, tool: str, target: str, reason: str) -> ToolResult:
    record_audit(
        ctx.db,
        event_type="agent.tool.rejected",
        summary=f"Rejected {tool} on {target}: {reason}",
        session_id=ctx.session.id,
        run_id=ctx.run.id,
        actor="agent",
        severity="warning",
        detail={"tool": tool, "target": target, "reason": reason},
    )
    return ToolResult(tool=tool, ok=False, error=reason, detail={"target": target, "reason": reason})


def tool_catalog(screen_id: str) -> dict[str, Any]:
    """Compact tool + field description injected into prompts."""
    return {
        "editable_fields": list(MUTABLE_FIELDS.get(screen_id, ())),
        "ui_targets": sorted(UI_TARGETS),
        "tools": [
            {"name": "set_field", "args": ["field", "value"]},
            {"name": "highlight", "args": ["field"]},
            {"name": "upload_doc", "args": ["document_id"]},
            {"name": "retry_step", "args": ["step_id", "idempotency_key"]},
            {"name": "navigate", "args": ["screen_id"]},
            {"name": "escalate_to_human", "args": ["reason"]},
            {"name": "search_policy", "args": ["query"]},
        ],
        "max_tool_calls_per_run": settings.max_agent_tool_calls,
    }


def serialise_action(action: AgentAction) -> dict[str, Any]:
    return {
        "action_id": action.id,
        "type": action.action_type,
        "field": action.field,
        "target": action.target,
        "step_id": action.step_id,
        "requires_confirmation": action.requires_confirmation,
        "sensitive": action.is_sensitive,
        "preview": action.value_masked,
        "reason": action.reason,
        "status": action.status,
        "idempotency_key": action.idempotency_key,
        "result": action.result,
    }


def action_summary(action: AgentAction) -> str:
    if action.field:
        return f"{action.action_type}:{action.field}"
    if action.target:
        return f"{action.action_type}:{action.target}"
    return action.action_type


def debug_actions(actions: list[AgentAction]) -> str:
    return json.dumps([serialise_action(a) for a in actions], default=str)
