from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_session_or_404
from app.core.config import settings
from app.core.logging import get_logger
from app.models.entities import DocumentRecord, HumanEscalation, MandateRecord, UserSession
from app.schemas import (
    AddressVerifyRequest,
    AddressVerifyResponse,
    DocumentAcceptRequest,
    DocumentQuality,
    DocumentUploadResponse,
    MandateResponse,
    MandateRetryRequest,
    MandateSubmitRequest,
    PANVerifyRequest,
    PANVerifyResponse,
)
from app.services.actions import finalize_pending_runs_for_session, resolve_mandate_confirmation
from app.services.address import city_suggestions, validate_address
from app.services.audit import record_audit
from app.services.documents import analyse_document
from app.services.idempotency import IdempotencyStore
from app.services.journey import update_session_meta
from app.services.mandate import retry_mandate, submit_mandate
from app.services.pan import check_pan, mock_verify_pan
from app.services.pii import value_hash

logger = get_logger(__name__)
router = APIRouter(prefix="/api/kyc", tags=["kyc"])


# ---------------------------------------------------------------------------
# PAN
# ---------------------------------------------------------------------------


@router.post("/pan/verify", response_model=PANVerifyResponse)
def verify_pan(payload: PANVerifyRequest, db: Session = Depends(get_db)) -> PANVerifyResponse:
    session = get_session_or_404(db, payload.session_id)
    meta = dict(session.meta or {})

    if meta.get("pan_verified"):
        return PANVerifyResponse(
            status="verified",
            message="This PAN is already verified.",
            attempts=int(meta.get("pan_attempts") or 0),
            remaining_attempts=max(0, 3 - int(meta.get("pan_attempts") or 0)),
        )

    idem = IdempotencyStore(namespace="pan")
    fingerprint_payload = {"session": session.id, "pan": value_hash(payload.pan, "pan")}
    key = payload.idempotency_key or f"pan:{session.id}:{value_hash(payload.pan, 'pan')}"
    state, record = idem.begin(key, fingerprint_payload)
    if state == "replay" and record.result:
        return PANVerifyResponse(**record.result)

    check = check_pan(payload.pan)
    attempts = int(meta.get("pan_attempts") or 0)
    if meta.get("pan_attempts_exhausted"):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "pan_attempts_exhausted",
                "message": "Verification attempts are exhausted. Please use the human support option.",
            },
        )

    attempt_number = attempts + 1
    update_session_meta(db, session, pan_attempts=attempt_number)

    if not check.ok:
        feasible = _register_pan_failure(db, session, check.code or "pan_format", check.reason)
        response = PANVerifyResponse(
            status="failed",
            message=check.message,
            error_code=check.code,
            attempts=attempt_number,
            remaining_attempts=max(0, 3 - attempt_number),
        )
        idem.fail(key, fingerprint_payload, check.message)
        db.commit()
        if feasible:
            _maybe_exhaust(db, session, attempt_number)
        return response

    verified, error_code, message = mock_verify_pan(check.normalized)
    if not verified:
        _register_pan_failure(db, session, error_code or "pan_not_found", message)
        response = PANVerifyResponse(
            status="failed",
            message=message,
            error_code=error_code,
            attempts=attempt_number,
            remaining_attempts=max(0, 3 - attempt_number),
        )
        idem.fail(key, fingerprint_payload, message)
        db.commit()
        _maybe_exhaust(db, session, attempt_number)
        return response

    update_session_meta(db, session, pan_verified=True)
    record_audit(
        db,
        event_type="pan.verified",
        summary="PAN verified (format + mock registry check)",
        session_id=session.id,
        actor="user",
        detail={"attempt": attempt_number, "pan_hash": value_hash(check.normalized, "pan")[:12]},
    )
    finalize_pending_runs_for_session(
        db, session, reason="pan_verified", detail={"screen_id": "kyc.pan_entry"}
    )
    response = PANVerifyResponse(
        status="verified",
        message="PAN verified successfully.",
        attempts=attempt_number,
        remaining_attempts=max(0, 3 - attempt_number),
        next_screen="kyc.document_upload",
    )
    idem.complete(key, fingerprint_payload, response.model_dump())
    db.commit()
    return response


def _register_pan_failure(db: Session, session: UserSession, code: str, reason: str) -> bool:
    record_audit(
        db,
        event_type="pan.verification_failed",
        summary="PAN verification failed",
        session_id=session.id,
        actor="user",
        severity="warning",
        detail={"code": code, "reason": reason},
    )
    # Cryptic messages are intentional in the UI; the real reason is in the audit
    # trail so the copilot can explain it.
    return True


def _maybe_exhaust(db: Session, session: UserSession, attempt_number: int) -> None:
    if attempt_number < 3:
        return
    meta = dict(session.meta or {})
    if meta.get("pan_attempts_exhausted"):
        return
    update_session_meta(db, session, pan_attempts_exhausted=True)
    escalation = HumanEscalation(
        session_id=session.id,
        reason="PAN verification attempt limit reached (3)",
        priority="normal",
        detail={"attempts": attempt_number},
    )
    db.add(escalation)
    record_audit(
        db,
        event_type="escalation.created",
        summary="PAN attempt limit reached; escalated to human support",
        session_id=session.id,
        actor="system",
        severity="warning",
        detail={"escalation_id": escalation.id},
    )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


@router.post("/document/upload", response_model=DocumentUploadResponse)
async def upload_document(
    session_id: str = Form(...),
    doc_type: str = Form("pan"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> DocumentUploadResponse:
    session = get_session_or_404(db, session_id)
    meta = dict(session.meta or {})
    uploads = int(meta.get("document_uploads") or 0)
    if uploads >= 3 and not meta.get("document_accepted"):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "upload_limit_reached",
                "message": "The upload limit (3) is reached. Please use the human support option.",
            },
        )

    content = await file.read(8 * 1024 * 1024 + 1)
    filename = file.filename or "upload.bin"
    content_type = file.content_type or "application/octet-stream"

    quality, ocr, status = analyse_document(filename, content_type, len(content))

    duplicate = db.scalar(
        select(DocumentRecord).where(
            DocumentRecord.session_id == session.id,
            DocumentRecord.filename == filename,
            DocumentRecord.status == "accepted",
        )
    )
    if duplicate is not None and quality.acceptable:
        status = "needs_review"
        quality.acceptable = False
        quality.issues.append("This document matches an accepted upload from this session (possible duplicate).")
        ocr["issues"] = quality.issues

    storage_key = ""
    if content and content_type in {"image/jpeg", "image/jpg", "image/png", "application/pdf"}:
        storage_key = f"{session.id}/{uuid.uuid4().hex}_{filename}"
        target = settings.upload_dir / f"{uuid.uuid4().hex}_{filename}"
        try:
            target.write_bytes(content[: 8 * 1024 * 1024])
            storage_key = target.name
        except OSError as exc:  # pragma: no cover - disk issues
            logger.warning("document.store_failed: %s", exc)

    record = DocumentRecord(
        session_id=session.id,
        doc_type=doc_type,
        filename=filename[:200],
        content_type=content_type,
        size_bytes=len(content),
        storage_key=storage_key,
        quality=quality.model_dump(),
        ocr_masked=ocr,
        status=status,
    )
    db.add(record)
    update_session_meta(db, session, document_uploads=uploads + 1)
    if status == "accepted":
        update_session_meta(db, session, document_accepted=record.id)
    db.flush()

    record_audit(
        db,
        event_type="document.uploaded",
        summary=f"Document uploaded -> {status}",
        session_id=session.id,
        actor="user",
        severity="info" if status == "accepted" else "warning",
        detail={
            "document_id": record.id,
            "doc_type": doc_type,
            "content_type": content_type,
            "size_bytes": len(content),
            "quality": quality.model_dump(),
            "simulated": True,
        },
    )
    db.commit()

    message = {
        "accepted": "Document meets all quality checks. Confirm to use this extraction.",
        "needs_review": "Document needs review. " + " ".join(quality.issues),
        "rejected": "Document rejected. " + " ".join(quality.issues),
    }[status]

    return DocumentUploadResponse(
        document_id=record.id,
        doc_type=doc_type,
        filename=filename,
        content_type=content_type,
        size_bytes=len(content),
        quality=quality,
        ocr=ocr,
        status=status,
        message=message,
        requires_user_confirmation=status == "accepted",
    )


@router.post("/document/accept")
def accept_document(payload: DocumentAcceptRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    session = get_session_or_404(db, payload.session_id)
    record = db.get(DocumentRecord, payload.document_id)
    if record is None or record.session_id != session.id:
        raise HTTPException(status_code=404, detail={"code": "document_not_found", "message": "Document not found"})
    quality = DocumentQuality.model_validate(record.quality or {})
    if not quality.acceptable:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "document_not_acceptable",
                "message": "This document does not meet the quality requirements and cannot be accepted.",
            },
        )
    if not payload.confirm:
        raise HTTPException(status_code=409, detail={"code": "confirmation_required", "message": "Explicit confirmation is required."})
    record.status = "accepted"
    update_session_meta(db, session, document_accepted=record.id)
    record_audit(
        db,
        event_type="document.accepted",
        summary="User accepted document extraction",
        session_id=session.id,
        actor="user",
        detail={"document_id": record.id, "doc_type": record.doc_type},
    )
    finalize_pending_runs_for_session(db, session, reason="document_accepted", detail={"screen_id": "kyc.document_upload"})
    db.commit()
    return {"ok": True, "document_id": record.id, "status": "accepted", "next_screen": "kyc.bank_mandate"}


# ---------------------------------------------------------------------------
# Mandate
# ---------------------------------------------------------------------------


@router.post("/mandate/submit", response_model=MandateResponse)
def submit_mandate_route(payload: MandateSubmitRequest, db: Session = Depends(get_db)) -> MandateResponse:
    session = get_session_or_404(db, payload.session_id)
    key = payload.idempotency_key or f"mnd:{session.id}:{value_hash(payload.account_number, 'account_number')}"
    try:
        outcome = submit_mandate(
            db,
            session=session,
            bank=payload.bank,
            ifsc=payload.ifsc,
            account_number=payload.account_number,
            idempotency_key=key,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail={"code": "invalid_mandate", "message": str(exc)}) from exc
    update_session_meta(db, session, mandate_key=outcome.idempotency_key, mandate_status=outcome.status)
    record_audit(
        db,
        event_type="mandate.status",
        summary=f"Mandate {outcome.status} (attempts={outcome.attempts})",
        session_id=session.id,
        actor="user",
        severity="info" if outcome.status == "verified" else "warning",
        detail={
            "mandate_ref": outcome.mandate_ref,
            "status": outcome.status,
            "attempts": outcome.attempts,
            "idempotent_replay": outcome.idempotent_replay,
            "simulated": True,
        },
    )
    if outcome.status == "verified":
        finalize_pending_runs_for_session(db, session, reason="mandate_verified", detail={"screen_id": "kyc.bank_mandate"})
    db.commit()
    return MandateResponse(**outcome.__dict__)


@router.post("/mandate/retry", response_model=MandateResponse)
def retry_mandate_route(payload: MandateRetryRequest, db: Session = Depends(get_db)) -> MandateResponse:
    session = get_session_or_404(db, payload.session_id)
    try:
        outcome = retry_mandate(db, session=session, idempotency_key=payload.idempotency_key)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail={"code": "mandate_not_found", "message": str(exc)}) from exc

    record = db.scalar(select(MandateRecord).where(MandateRecord.idempotency_key == payload.idempotency_key))
    if record is not None and outcome.status != "verified" and outcome.status != "unknown":
        # The retry executed but failed; the confirmation is still satisfied.
        resolve_mandate_confirmation(db, session, record)
    if record is not None and outcome.status == "verified":
        resolve_mandate_confirmation(db, session, record)
        update_session_meta(db, session, mandate_status="verified")
        finalize_pending_runs_for_session(db, session, reason="mandate_verified", detail={"screen_id": "kyc.bank_mandate"})

    db.commit()
    return MandateResponse(**outcome.__dict__)


@router.get("/mandate/{session_id}")
def mandate_status(session_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    session = get_session_or_404(db, session_id)
    record = db.scalar(
        select(MandateRecord)
        .where(MandateRecord.session_id == session.id)
        .order_by(MandateRecord.created_at.desc())
    )
    if record is None:
        return {"status": "not_started", "attempts": 0, "max_attempts": 3, "session_id": session.id}
    return {
        "status": record.status,
        "mandate_ref": record.mandate_ref,
        "bank": record.bank,
        "masked_account": record.account_masked,
        "attempts": record.attempts,
        "max_attempts": 3,
        "last_error": record.last_error,
        "idempotency_key": record.idempotency_key,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "simulated": True,
    }


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------


@router.post("/address/verify", response_model=AddressVerifyResponse)
def verify_address(payload: AddressVerifyRequest, db: Session = Depends(get_db)) -> AddressVerifyResponse:
    session = get_session_or_404(db, payload.session_id)
    errors, suggestions = validate_address(
        payload.house_number, payload.street, payload.city, payload.state, payload.pin
    )
    if errors:
        record_audit(
            db,
            event_type="address.verification_failed",
            summary="Address validation failed",
            session_id=session.id,
            actor="user",
            severity="warning",
            detail={"fields": list(errors.keys()), "codes": list(errors.values())},
        )
        db.commit()
        return AddressVerifyResponse(
            status="failed",
            message="We couldn't verify this address. Check the highlighted fields.",
            field_errors=errors,
            suggestions=suggestions or {"city": city_suggestions(payload.state)},
        )

    update_session_meta(db, session, address_verified=True)
    record_audit(
        db,
        event_type="address.verified",
        summary="Address verified (simulated geocoder)",
        session_id=session.id,
        actor="user",
        detail={"state": payload.state.strip(), "city": payload.city.strip(), "pin_hash": value_hash(payload.pin, "pin")[:10]},
    )
    finalize_pending_runs_for_session(db, session, reason="address_verified", detail={"screen_id": "kyc.address"})
    db.commit()
    return AddressVerifyResponse(
        status="verified",
        message="Address verified successfully.",
        next_screen="kyc.review",
    )


@router.get("/suggestions")
def suggestions(state: str | None = None) -> dict[str, Any]:
    from app.services.address import state_suggestions

    return {
        "states": state_suggestions(state or ""),
        "cities": city_suggestions(state),
        "simulated": True,
    }
