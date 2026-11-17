"""Journey state: screen snapshots, session lifecycle, journey events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.entities import JourneyEvent, PIIRecord, ScreenState, UserSession
from app.schemas import SCREEN_STEPS, ScreenContext
from app.services.audit import record_audit
from app.services.cache import get_cache
from app.services.mandate import mandate_context
from app.services.pii import PIIVault, mask_context

logger = get_logger(__name__)

SCREEN_CACHE_PREFIX = "screen:"


def create_session(db: Session, *, external_ref: str | None = None, is_evaluation: bool = False) -> UserSession:
    session = UserSession(
        external_ref=external_ref,
        is_evaluation=is_evaluation,
        current_screen_id="kyc.pan_entry",
        current_step=1,
        meta={"pan_attempts": 0, "document_uploads": 0, "mandate_force_failures": 0},
    )
    db.add(session)
    db.flush()
    record_audit(
        db,
        event_type="session.created",
        summary="KYC session created",
        session_id=session.id,
        actor="user",
        detail={"evaluation": is_evaluation},
    )
    return session


def update_session_meta(db: Session, session: UserSession, **values: Any) -> None:
    meta = dict(session.meta or {})
    meta.update(values)
    session.meta = meta
    db.add(session)
    db.flush()


def upsert_screen_state(db: Session, session: UserSession, context: ScreenContext) -> ScreenState:
    vault = PIIVault(db, session.id)
    masked = mask_context(context.model_dump(mode="json"), vault)

    latest = db.scalar(
        select(ScreenState)
        .where(ScreenState.session_id == session.id)
        .order_by(ScreenState.revision.desc())
    )
    revision = (latest.revision + 1) if latest else 1

    state = ScreenState(
        session_id=session.id,
        screen_id=context.screen_id,
        step=SCREEN_STEPS.get(context.screen_id, context.step),
        revision=revision,
        context=masked,
    )
    db.add(state)
    session.current_screen_id = context.screen_id
    session.current_step = SCREEN_STEPS.get(context.screen_id, context.step)
    session.updated_at = datetime.now(UTC)

    db.add(
        JourneyEvent(
            session_id=session.id,
            screen_id=context.screen_id,
            step=state.step,
            event_type="screen.snapshot",
            payload={
                "revision": revision,
                "errors": masked.get("errors", []),
                "field_names": [f.get("name") for f in masked.get("fields", [])],
                "status": masked.get("status", {}),
            },
        )
    )
    db.flush()
    get_cache().set_json(f"{SCREEN_CACHE_PREFIX}{session.id}", masked, ttl_seconds=1800)
    return state


def latest_screen_state(db: Session, session_id: str) -> ScreenState | None:
    return db.scalar(
        select(ScreenState)
        .where(ScreenState.session_id == session_id)
        .order_by(ScreenState.revision.desc())
    )


def rehydrate_context(db: Session, masked: dict[str, Any], vault: PIIVault) -> dict[str, Any]:
    """Restore real values for the owning client (same-user read path)."""
    out = {k: v for k, v in masked.items() if k != "fields"}
    fields: list[dict[str, Any]] = []
    for raw in masked.get("fields", []):
        field = dict(raw)
        token = field.pop("token", None)
        if token:
            record = db.get(PIIRecord, token)
            if record and record.session_id == vault.session_id:
                try:
                    field["value"] = vault._fernet.decrypt(record.ciphertext.encode()).decode()  # noqa: SLF001
                except Exception:
                    field["value"] = None
        field.pop("value_is_surrogate", None)
        fields.append(field)
    out["fields"] = fields
    return out


def get_screen_context(db: Session, session_id: str) -> dict[str, Any] | None:
    state = latest_screen_state(db, session_id)
    if state is None:
        return None
    vault = PIIVault(db, session_id)
    raw = rehydrate_context(db, state.context, vault)
    raw["session_id"] = session_id
    raw["revision"] = state.revision
    raw["updated_at"] = state.updated_at.isoformat() if state.updated_at else None
    return raw


def get_agent_screen_context(db: Session, session_id: str) -> dict[str, Any] | None:
    """Masked context for LLM prompts (surrogates, never raw values)."""
    state = latest_screen_state(db, session_id)
    if state is None:
        return None
    return dict(state.context)


def build_context_from_seed(
    db: Session,
    session: UserSession,
    screen_id: str,
    seed: dict[str, Any],
) -> ScreenContext:
    """Create a screen context for evaluation scenarios / demos.

    Mirrors what the frontend would post, using the same validation hints.
    """
    from app.schemas import MUTABLE_FIELDS

    fields: list[dict[str, Any]] = []
    errors: list[str] = []
    option_map = seed.get("field_options") or {}
    for name, value in seed.get("fields", {}).items():
        if name in {"document", "mandate", "status"}:
            continue
        field_seed = {
            "name": name,
            "label": name.replace("_", " ").title(),
            "value": value,
            "sensitive": False,
        }
        if option_map.get(name):
            field_seed["options"] = list(option_map[name])
        fields.append(field_seed)
    # Include known screen fields even when empty so the agent sees the form.
    defaults = {
        "kyc.pan_entry": ["full_name", "pan"],
        "kyc.document_upload": [],
        "kyc.bank_mandate": ["bank", "ifsc", "account_number"],
        "kyc.address": ["house_number", "street", "city", "state", "pin"],
    }[screen_id]
    present = {f["name"] for f in fields}
    for name in defaults:
        if name not in present:
            fields.append({"name": name, "label": name.replace("_", " ").title(), "value": "", "sensitive": False})
    if seed.get("errors"):
        if isinstance(seed["errors"], dict):
            for fname, msg in seed["errors"].items():
                for field in fields:
                    if field["name"] == fname:
                        field["error"] = msg
                errors.append(msg)
        else:
            errors.extend(seed["errors"])
    document = seed.get("document")
    mandate = seed.get("mandate") or (mandate_context(db, session.id) if screen_id == "kyc.bank_mandate" else None)
    context = ScreenContext(
        session_id=session.id,
        screen_id=screen_id,
        fields=fields,
        errors=errors,
        document=document,
        mandate=mandate,
        status=seed.get("status", {}),
        actions=list(MUTABLE_FIELDS.get(screen_id, ())),
    )
    return context
