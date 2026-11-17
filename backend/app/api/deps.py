from __future__ import annotations

import hmac

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.entities import UserSession


def get_session_or_404(db: Session, session_id: str) -> UserSession:
    session = db.get(UserSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": "Session not found"})
    return session


def require_admin(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    admin_token: str | None = None,
) -> None:
    token = x_admin_token or admin_token
    if not token or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Admin token required"})


__all__ = ["get_db", "get_session_or_404", "require_admin"]
