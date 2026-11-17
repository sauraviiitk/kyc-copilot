from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_session_or_404
from app.schemas import ScreenContextRead, ScreenContextUpdate
from app.services.journey import (
    get_screen_context,
    latest_screen_state,
    update_session_meta,
    upsert_screen_state,
)

router = APIRouter(prefix="/api", tags=["screen-context"])


@router.get("/screen-context", response_model=ScreenContextRead)
def read_context(session_id: str, db: Session = Depends(get_db)) -> ScreenContextRead:
    get_session_or_404(db, session_id)
    state = latest_screen_state(db, session_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "no_screen_context", "message": "The client has not published screen context yet."},
        )
    context = get_screen_context(db, session_id) or {}
    return ScreenContextRead(
        session_id=session_id,
        screen_id=state.screen_id,
        step=state.step,
        revision=state.revision,
        updated_at=state.updated_at,
        context=context,
    )


@router.post("/screen-context/update")
def update_context(payload: ScreenContextUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    session = get_session_or_404(db, payload.session_id)
    if payload.context.session_id and payload.context.session_id != session.id:
        raise HTTPException(status_code=409, detail="context session mismatch")
    payload.context.session_id = session.id
    state = upsert_screen_state(db, session, payload.context)

    if payload.context.status:
        meta_updates: dict[str, Any] = {}
        if payload.context.status.get("pan") == "verified":
            meta_updates["pan_verified"] = True
        if payload.context.status.get("address") == "verified":
            meta_updates["address_verified"] = True
        if meta_updates:
            update_session_meta(db, session, **meta_updates)
    db.commit()
    return {
        "ok": True,
        "revision": state.revision,
        "screen_id": state.screen_id,
        "step": state.step,
        "reason": payload.reason,
    }
