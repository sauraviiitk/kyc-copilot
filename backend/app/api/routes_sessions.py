from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_session_or_404
from app.models.entities import JourneyEvent, UserSession
from app.schemas import SCREEN_STEPS
from app.services.journey import create_session, get_screen_context

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionCreateRequest(BaseModel):
    external_ref: str | None = None
    consent_given: bool = True


class SessionResponse(BaseModel):
    session_id: str
    status: str
    current_screen_id: str
    current_step: int
    consent_given: bool
    created_at: str
    screen_context: dict[str, Any] | None = None
    flags: dict[str, Any] = {}


@router.post("", response_model=SessionResponse)
def create(payload: SessionCreateRequest | None = None, db: Session = Depends(get_db)) -> SessionResponse:
    payload = payload or SessionCreateRequest()
    session = create_session(db, external_ref=payload.external_ref)
    session.consent_given = payload.consent_given
    db.commit()
    return _serialise(session, db)


@router.get("/{session_id}", response_model=SessionResponse)
def read(session_id: str, db: Session = Depends(get_db)) -> SessionResponse:
    session = get_session_or_404(db, session_id)
    return _serialise(session, db)


@router.get("/{session_id}/events")
def events(session_id: str, limit: int = 50, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    get_session_or_404(db, session_id)
    rows = db.scalars(
        select(JourneyEvent)
        .where(JourneyEvent.session_id == session_id)
        .order_by(JourneyEvent.created_at.desc())
        .limit(min(limit, 200))
    ).all()
    return [
        {
            "id": row.id,
            "type": row.event_type,
            "screen_id": row.screen_id,
            "step": row.step,
            "payload": row.payload,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def _serialise(session: UserSession, db: Session) -> SessionResponse:
    context = get_screen_context(db, session.id)
    meta = session.meta or {}
    return SessionResponse(
        session_id=session.id,
        status=session.status,
        current_screen_id=session.current_screen_id,
        current_step=SCREEN_STEPS.get(session.current_screen_id, session.current_step),
        consent_given=session.consent_given,
        created_at=session.created_at.isoformat(),
        screen_context=context,
        flags={
            "pan_verified": bool(meta.get("pan_verified")),
            "pan_attempts": int(meta.get("pan_attempts") or 0),
            "document_accepted": meta.get("document_accepted"),
            "document_uploads": int(meta.get("document_uploads") or 0),
            "address_verified": bool(meta.get("address_verified")),
        },
    )


@router.patch("/{session_id}")
def update_session(session_id: str, payload: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    session = get_session_or_404(db, session_id)
    if "status" in payload:
        if payload["status"] not in {"active", "completed", "escalated", "abandoned"}:
            raise HTTPException(status_code=422, detail="invalid status")
        session.status = payload["status"]
    if "consent_given" in payload:
        session.consent_given = bool(payload["consent_given"])
    db.commit()
    return {"ok": True, "status": session.status}
