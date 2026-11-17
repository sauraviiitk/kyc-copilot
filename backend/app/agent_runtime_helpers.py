"""Helpers for turning persisted (masked) agent events into user-facing events."""

from __future__ import annotations

from typing import Any

from app.services.pii import PIIVault

REHYDRATE_EVENTS = {"agent_message", "agent_complete", "agent_plan"}
REHYDRATE_KEYS = {"say", "confirmation_prompt", "detail", "plan", "message"}


def rehydrate_event(event: dict[str, Any], vault: PIIVault) -> dict[str, Any]:
    if event.get("event") not in REHYDRATE_EVENTS:
        return event
    data = _walk(event.get("data") or {}, vault)
    return {**event, "data": data}


def _walk(value: Any, vault: PIIVault) -> Any:
    if isinstance(value, str):
        return vault.rehydrate_text(value)
    if isinstance(value, dict):
        return {k: _walk(v, vault) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, vault) for v in value]
    return value
