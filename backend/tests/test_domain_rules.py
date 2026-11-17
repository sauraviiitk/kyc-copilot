"""Deterministic KYC domain rules and financial safety invariants."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import MandateRecord, UserSession
from app.services.address import canonical_state, city_suggestions, normalize_pin, validate_address
from app.services.journey import update_session_meta
from app.services.mandate import (
    MAX_ATTEMPTS,
    retry_mandate,
    submit_mandate,
)
from app.services.pan import check_pan, mock_verify_pan, normalize_pan

# --- PAN -------------------------------------------------------------------


def test_pan_format_rules() -> None:
    valid = check_pan("ABCDE1234F")
    assert valid.ok and valid.normalized == "ABCDE1234F" and valid.code is None

    assert check_pan("").code == "pan_format"
    assert check_pan("ABCDE123").code == "pan_length"
    lowercase = check_pan("abcde1234f")
    assert lowercase.code == "pan_lowercase" and lowercase.fixable_by_agent is True
    assert lowercase.normalized == "ABCDE1234F"
    assert check_pan("A1CDE1234F").code == "pan_format"
    assert not check_pan("ABCDE1234F").fixable_by_agent

    assert normalize_pan(" ABCDE 1234 F ") == "ABCDE1234F"
    assert normalize_pan("ABCDE1234F").isupper()


def test_pan_mock_registry_is_synthetic_and_deterministic() -> None:
    assert mock_verify_pan("AAAAA0000A")[1] == "pan_not_found"
    ok, code, _message = mock_verify_pan("ABCDE1234F")
    assert ok and code is None
    assert mock_verify_pan("ABCDE1234F") == (ok, code, mock_verify_pan("ABCDE1234F")[2])


# --- Address ----------------------------------------------------------------


def test_address_validation_and_suggestions() -> None:
    errors, suggestions = validate_address("12", "MG Road", "Bengaluru", "Karnataka", "560001")
    assert errors == {}
    assert "Bengaluru" in suggestions["city"]
    assert canonical_state("karnataka") == "Karnataka"

    errors, suggestions = validate_address("12", "MG Road", "Pune", "Karnataka", "560001")
    assert errors["city"] == "City does not match Karnataka (E-ADDR-11)."
    assert "Bengaluru" in suggestions["city"]

    errors, _ = validate_address("12", "MG Road", "Bengaluru", "Karnataka", "056001")
    assert errors["pin"] == "PIN code cannot start with zero (E-ADDR-21)."
    assert normalize_pin("560 001") == "560001"
    assert validate_address("12", "MG Road", "Bengaluru", "Karnataka", "560 001")[0] == {}

    errors, suggestions = validate_address("Flat 4", "asdf road", "Bengaluru", "Karnatka", "560001")
    assert "street" in errors and "state" in errors
    assert "Karnataka" in suggestions["state"]
    assert city_suggestions("Karnataka") == ["Bengaluru", "Mysuru", "Mangaluru"]


# --- Mandate ----------------------------------------------------------------


def _submit(db: Session, session: UserSession, key: str, account: str = "12345678901234"):
    return submit_mandate(
        db,
        session=session,
        bank="HDFC Bank",
        ifsc="HDFC0001234",
        account_number=account,
        idempotency_key=key,
    )


def test_mandate_idempotency_never_creates_a_second_record(
    db: Session, session: UserSession
) -> None:
    first = _submit(db, session, "key-once")
    assert first.status == "verified"
    assert first.idempotent_replay is False

    replay = _submit(db, session, "key-once", account="99999999999999")
    assert replay.idempotent_replay is True
    assert replay.mandate_ref == first.mandate_ref
    assert replay.attempts == first.attempts == 1
    assert db.scalar(select(func.count()).select_from(MandateRecord)) == 1


def test_mandate_retry_is_bounded_and_confirmation_gated(
    db: Session, session: UserSession
) -> None:
    update_session_meta(
        db,
        session,
        mandate_force_failures=MAX_ATTEMPTS + 2,
        mandate_force_mode="timeout",
    )
    first = _submit(db, session, "key-retry")
    assert first.status == "failed"
    assert first.retryable is True
    assert first.requires_confirmation is True
    assert first.error_code == "GATEWAY_TIMEOUT"

    second = retry_mandate(db, session=session, idempotency_key=first.idempotency_key)
    assert second.attempts == 2 and second.status == "failed"
    third = retry_mandate(db, session=session, idempotency_key=first.idempotency_key)
    assert third.attempts == MAX_ATTEMPTS and third.retryable is False

    # The limit is terminal: an extra retry is a replay, never a fourth attempt.
    fourth = retry_mandate(db, session=session, idempotency_key=first.idempotency_key)
    assert fourth.attempts == MAX_ATTEMPTS and fourth.idempotent_replay is True
    assert db.scalar(select(func.count()).select_from(MandateRecord)) == 1


def test_unknown_mandate_status_is_never_retried(db: Session, session: UserSession) -> None:
    update_session_meta(
        db,
        session,
        mandate_force_failures=2,
        mandate_force_mode="unknown",
    )
    outcome = _submit(db, session, "key-unknown")
    assert outcome.status == "unknown"
    assert outcome.retryable is False
    assert outcome.requires_confirmation is False
    assert outcome.error_code == "GATEWAY_STATUS_UNKNOWN"

    replay = retry_mandate(db, session=session, idempotency_key=outcome.idempotency_key)
    assert replay.attempts == 1 and replay.idempotent_replay is True


def test_mandate_validation_rejects_bad_ifsc_and_foreign_keys(
    db: Session, session: UserSession
) -> None:
    with pytest.raises(ValueError, match="IFSC"):
        submit_mandate(
            db,
            session=session,
            bank="HDFC",
            ifsc="BADIFSC",
            account_number="123456789",
            idempotency_key="bad-ifsc",
        )
    with pytest.raises(ValueError, match="idempotency_key is required"):
        _submit(db, session, "   ")
