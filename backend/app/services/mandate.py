"""Simulated bank-mandate gateway with idempotency.

Design goals (all enforced here, not in the prompt):
* the same idempotency key never creates a second mandate;
* a verified mandate is terminal;
* an *unknown* gateway status is never retried automatically;
* retries are bounded (3 attempts total), and every attempt reuses the key.

The gateway deliberately fails ~20% of the time (configurable), split between
hard failures (retryable) and unknown states (not retryable).
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, attributes

from app.core.config import settings
from app.core.logging import get_logger
from app.models.entities import MandateRecord, UserSession
from app.schemas import IFSC_RE
from app.services.audit import record_audit
from app.services.cache import get_cache
from app.services.pii import PIIVault, _fernet

logger = get_logger(__name__)

MAX_ATTEMPTS = 3
ACCOUNT_RE = re.compile(r"^\d{9,18}$")
MANDATE_CACHE_PREFIX = "mandate:"


@dataclass
class MandateOutcome:
    mandate_ref: str
    status: str
    attempts: int
    max_attempts: int
    message: str
    error_code: str | None
    idempotency_key: str
    idempotent_replay: bool
    retryable: bool
    requires_confirmation: bool = False
    record_id: str | None = None


def derive_mandate_ref(session_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{session_id}:{key}".encode()).hexdigest()[:10].upper()
    return f"MND-{digest}"


def _account_masked(account: str) -> str:
    return "•" * max(0, len(account) - 4) + account[-4:]


def _gateway(session: UserSession, key: str, attempt: int) -> tuple[str, str | None, str]:
    """Return (status, error_code, message) for one gateway attempt."""
    meta = dict(session.meta or {})
    forced = int(meta.get("mandate_force_failures") or 0)
    force_mode = meta.get("mandate_force_mode") or "timeout"

    if forced > 0:
        meta["mandate_force_failures"] = forced - 1
        session.meta = meta
        attributes.flag_modified(session, "meta")
        if force_mode == "unknown":
            return "unknown", "GATEWAY_STATUS_UNKNOWN", "The bank did not confirm the transaction status."
        return "failed", "GATEWAY_TIMEOUT", "The bank gateway timed out before confirming the mandate."

    if not settings.mock_seed:
        roll = random.random()
    else:
        rng = random.Random(f"{settings.mock_seed}:{key}:{attempt}")
        roll = rng.random()
    rate = max(0.0, min(1.0, settings.mock_mandate_failure_rate))
    if roll < rate:
        # Half of injected failures are hard failures, half are unknown states.
        if roll < rate / 2:
            return "failed", "GATEWAY_TIMEOUT", "The bank gateway timed out before confirming the mandate."
        return "unknown", "GATEWAY_STATUS_UNKNOWN", "The bank did not confirm the transaction status."

    return "verified", None, "Mandate verified by the bank."


def _to_outcome(record: MandateRecord, *, replay: bool, message: str | None = None) -> MandateOutcome:
    retryable = record.status == "failed" and record.attempts < MAX_ATTEMPTS
    if record.status == "verified":
        default_message = "Mandate already verified."
    elif record.status == "unknown":
        default_message = "Transaction status is unknown. A human operator must confirm before retrying."
    elif record.status == "failed":
        default_message = record.last_error or "Mandate submission failed."
    else:
        default_message = "Mandate is pending."
    return MandateOutcome(
        mandate_ref=record.mandate_ref,
        status=record.status,
        attempts=record.attempts,
        max_attempts=MAX_ATTEMPTS,
        message=message or default_message,
        error_code=(record.detail or {}).get("error_code"),
        idempotency_key=record.idempotency_key,
        idempotent_replay=replay,
        retryable=retryable,
        requires_confirmation=record.status in {"failed", "pending"},
        record_id=record.id,
    )


def _find_by_key(db: Session, key: str) -> MandateRecord | None:
    return db.scalar(select(MandateRecord).where(MandateRecord.idempotency_key == key))


def submit_mandate(
    db: Session,
    *,
    session: UserSession,
    bank: str,
    ifsc: str,
    account_number: str,
    idempotency_key: str,
) -> MandateOutcome:
    key = (idempotency_key or "").strip()
    if not key:
        raise ValueError("idempotency_key is required")

    existing = _find_by_key(db, key)
    if existing:
        if existing.session_id != session.id:
            raise ValueError("idempotency key belongs to another session")
        return _to_outcome(existing, replay=True)

    active = db.scalar(
        select(MandateRecord)
        .where(MandateRecord.session_id == session.id, MandateRecord.status == "verified")
        .order_by(MandateRecord.created_at.desc())
    )
    if active:
        return _to_outcome(active, replay=True, message="A verified mandate already exists for this journey.")

    cleaned_ifsc = (ifsc or "").strip().upper()
    if not IFSC_RE.match(cleaned_ifsc):
        raise ValueError("IFSC must match ^[A-Z]{4}0[A-Z0-9]{6}$")
    if not ACCOUNT_RE.match(account_number or ""):
        raise ValueError("Account number must be 9 to 18 digits")

    vault = PIIVault(db, session.id)
    vault.tokenize("account_number", account_number)

    record = MandateRecord(
        session_id=session.id,
        mandate_ref=derive_mandate_ref(session.id, key),
        bank=bank.strip(),
        ifsc=cleaned_ifsc,
        account_masked=_account_masked(account_number),
        account_ciphertext=_fernet().encrypt(account_number.encode()).decode(),
        idempotency_key=key,
        status="pending",
        attempts=1,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        # Concurrent duplicate submission: return the winner, never a second mandate.
        db.rollback()
        winner = _find_by_key(db, key)
        if winner is None:
            raise
        return _to_outcome(winner, replay=True)

    status, error_code, message = _gateway(session, key, 1)
    record.status = status
    record.last_error = None if status == "verified" else message
    record.detail = {"error_code": error_code, "simulated": True}
    db.flush()

    record_audit(
        db,
        event_type="mandate.submitted",
        summary=f"Mandate {record.mandate_ref} -> {status} (attempt 1)",
        session_id=session.id,
        actor="user",
        detail={
            "mandate_ref": record.mandate_ref,
            "status": status,
            "attempts": record.attempts,
            "account_masked": record.account_masked,
            "error_code": error_code,
        },
    )
    get_cache().set_json(
        f"{MANDATE_CACHE_PREFIX}{key}",
        {"status": status, "mandate_ref": record.mandate_ref, "attempts": 1},
        ttl_seconds=3600,
    )
    return _to_outcome(record, replay=False, message=message)


def retry_mandate(db: Session, *, session: UserSession, idempotency_key: str) -> MandateOutcome:
    record = _find_by_key(db, idempotency_key)
    if record is None or record.session_id != session.id:
        raise LookupError("No mandate found for this idempotency key")
    if record.status == "verified":
        return _to_outcome(record, replay=True, message="Mandate is already verified; nothing to retry.")
    if record.status == "unknown":
        return _to_outcome(record, replay=True)
    if record.attempts >= MAX_ATTEMPTS:
        return _to_outcome(record, replay=True, message="Retry limit reached. Escalate to a human reviewer.")

    record.attempts += 1
    status, error_code, message = _gateway(session, record.idempotency_key, record.attempts)
    record.status = status
    record.last_error = None if status == "verified" else message
    record.detail = {"error_code": error_code, "simulated": True}
    db.flush()

    record_audit(
        db,
        event_type="mandate.retried",
        summary=f"Mandate {record.mandate_ref} retry attempt {record.attempts} -> {status}",
        session_id=session.id,
        actor="agent",
        detail={
            "mandate_ref": record.mandate_ref,
            "status": status,
            "attempts": record.attempts,
            "account_masked": record.account_masked,
            "error_code": error_code,
        },
    )
    return _to_outcome(record, replay=False, message=message)


def latest_mandate(db: Session, session_id: str) -> MandateRecord | None:
    return db.scalar(
        select(MandateRecord)
        .where(MandateRecord.session_id == session_id)
        .order_by(MandateRecord.created_at.desc())
    )


def mandate_context(db: Session, session_id: str) -> dict:
    record = latest_mandate(db, session_id)
    if record is None:
        return {"status": "not_started", "attempts": 0, "max_attempts": MAX_ATTEMPTS}
    return {
        "mandate_ref": record.mandate_ref,
        "status": record.status,
        "attempts": record.attempts,
        "max_attempts": MAX_ATTEMPTS,
        "last_error": record.last_error,
        "idempotency_key": record.idempotency_key,
        "masked_account": record.account_masked,
    }
