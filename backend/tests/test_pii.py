"""PII vault, masking and anti-hallucination gate tests."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import PIIRecord, UserSession
from app.schemas import ScreenContext
from app.services.pii import PIIVault, fernet, mask_context, preview_mask, value_hash
from tests.conftest import dump

REAL_PAN = "ABCDE1234F"


def test_mask_context_never_leaks_raw_values(db: Session, session: UserSession) -> None:
    context = ScreenContext(
        session_id=session.id,
        screen_id="kyc.pan_entry",
        fields=[
            {"name": "full_name", "value": "Aarav Sharma"},
            {"name": "pan", "value": REAL_PAN},
            {"name": "city", "value": "Bengaluru"},
        ],
    )
    masked = mask_context(context.model_dump(mode="json"), PIIVault(db, session.id))

    assert REAL_PAN not in dump(masked)
    assert "Aarav Sharma" not in dump(masked)
    pan = next(field for field in masked["fields"] if field["name"] == "pan")
    assert pan["sensitive"] is True
    assert pan["value_is_surrogate"] is True
    assert pan["value"] != REAL_PAN
    assert len(pan["value"]) == len(REAL_PAN)
    assert pan["token"].startswith("⟦PAN_")
    # Non-sensitive fields stay readable so the agent sees the live screen.
    city = next(field for field in masked["fields"] if field["name"] == "city")
    assert city["value"] == "Bengaluru"


def test_tokenize_stores_only_encrypted_values(db: Session, session: UserSession) -> None:
    vault = PIIVault(db, session.id)
    entry = vault.tokenize("pan", REAL_PAN)
    assert entry is not None
    db.commit()

    rows = list(db.scalars(select(PIIRecord).where(PIIRecord.session_id == session.id)).all())
    assert len(rows) == 1
    assert REAL_PAN not in rows[0].ciphertext
    assert fernet().decrypt(rows[0].ciphertext.encode()).decode() == REAL_PAN
    assert rows[0].surrogate == entry.surrogate
    assert vault.resolve_token(entry.token) == REAL_PAN


def test_resolve_fill_rejects_invented_identity_characters(db: Session, session: UserSession) -> None:
    vault = PIIVault(db, session.id)
    entry = vault.tokenize("pan", REAL_PAN)
    assert entry is not None

    invented = vault.resolve_fill(
        "pan",
        "ZXCVB9876Q",
        current_value=REAL_PAN,
        raw_user_message="please fix the error",
    )
    assert invented.ok is False
    assert invented.value is None
    assert invented.source == "rejected"

    # A real value pasted into the chat is allowed; an LLM-invented one is not.
    supplied = vault.resolve_fill(
        "pan",
        "ABCDE1234G",
        current_value=REAL_PAN,
        raw_user_message="the correct PAN is ABCDE1234G",
    )
    assert supplied.ok is True
    assert supplied.value == "ABCDE1234G"
    assert supplied.source == "user_message"


def test_resolve_fill_accepts_surrogate_token_and_case_transform(
    db: Session, session: UserSession
) -> None:
    vault = PIIVault(db, session.id)
    real = "abcde1234f"
    entry = vault.tokenize("pan", real)
    assert entry is not None

    surrogate = vault.resolve_fill("pan", entry.surrogate, current_value=real, raw_user_message="")
    assert surrogate.ok and surrogate.value == real and surrogate.source == "surrogate"

    token = vault.resolve_fill("pan", entry.token, current_value=real, raw_user_message="")
    assert token.ok and token.value == real and token.source == "token"

    upper = vault.resolve_fill(
        "pan", entry.surrogate.upper(), current_value=real, raw_user_message=""
    )
    assert upper.ok and upper.value == real.upper()
    assert upper.source == "surrogate_transform"

    # Substituting a digit is not a case transform and must be refused.
    changed = list(entry.surrogate)
    changed[5] = "7" if changed[5] != "7" else "8"
    refused = vault.resolve_fill(
        "pan", "".join(changed), current_value=real, raw_user_message=""
    )
    assert refused.ok is False


def test_mask_message_masks_common_identifier_shapes(db: Session, session: UserSession) -> None:
    vault = PIIVault(db, session.id)
    message = "PAN ABCDE1234F, phone 9876543210, mail me at test.user@example.com"
    masked = vault.mask_message(message)

    assert REAL_PAN not in masked
    assert "9876543210" not in masked
    assert "test.user@example.com" not in masked
    assert "⟦PAN_" in masked
    assert "⟦PHONE_" in masked
    assert "⟦EMAIL_" in masked


def test_rehydrate_text_handles_tokens_surrogates_and_compact_variants(
    db: Session, session: UserSession
) -> None:
    vault = PIIVault(db, session.id)
    entry = vault.tokenize("pin", "560 001")
    assert entry is not None
    compact = entry.surrogate.replace(" ", "")

    assert vault.rehydrate_text(f"use {entry.token}") == "use 560 001"
    assert vault.rehydrate_text(f"use {entry.surrogate}") == "use 560 001"
    assert vault.rehydrate_text(entry.surrogate.upper()) == "560 001".upper()
    assert vault.rehydrate_text(entry.surrogate.lower()) == "560 001".lower()
    assert vault.rehydrate_text(f"pin {compact}") == "pin 560001"


def test_value_hash_and_preview_mask_are_non_reversible() -> None:
    first = value_hash(REAL_PAN, "pan")
    assert first == value_hash(REAL_PAN, "pan")
    assert first != value_hash(REAL_PAN, "aadhaar")
    assert first != value_hash("AAAAA0000A", "pan")
    assert len(first) == 32
    assert REAL_PAN not in first

    masked = preview_mask(REAL_PAN, keep=2)
    assert masked.startswith("AB") and masked.endswith("F")
    assert REAL_PAN not in masked
