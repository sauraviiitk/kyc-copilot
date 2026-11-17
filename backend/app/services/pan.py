"""PAN domain rules (format validation + deterministic mock verification)."""

from __future__ import annotations

import re
from dataclasses import dataclass

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")

# Synthetic PANs used by the mock verifier. No real taxpayer data is involved.
_NOT_FOUND_PANS = {"AAAAA0000A", "ZZZZZ9999Z", "ABCDE0000A"}

ERROR_MESSAGES = {
    "pan_format": "PAN could not be verified (E-PAN-01).",
    "pan_lowercase": "PAN could not be verified (E-PAN-01).",
    "pan_length": "PAN could not be verified (E-PAN-02).",
    "pan_invalid_chars": "PAN could not be verified (E-PAN-03).",
    "pan_not_found": "PAN not found in records (E-PAN-11).",
    "pan_already_verified": "PAN is locked for this application (E-PAN-20).",
    "pan_attempts_exhausted": "Verification is unavailable for this application.",
}


@dataclass(frozen=True)
class PANCheck:
    ok: bool
    normalized: str
    code: str | None
    message: str
    fixable_by_agent: bool
    reason: str


def normalize_pan(raw: str) -> str:
    """Remove separators and whitespace only. Case is preserved for diagnosis."""
    return NON_ALNUM_RE.sub("", raw or "").strip()


def check_pan(raw: str) -> PANCheck:
    compact = normalize_pan(raw)
    if not compact:
        return PANCheck(False, "", "pan_format", ERROR_MESSAGES["pan_format"], False, "PAN is empty")
    if len(compact) != 10:
        return PANCheck(
            False,
            compact,
            "pan_length",
            ERROR_MESSAGES["pan_length"],
            False,
            f"PAN has {len(compact)} characters; 10 are required",
        )
    if not all(ch.isalnum() for ch in compact):
        return PANCheck(False, compact, "pan_invalid_chars", ERROR_MESSAGES["pan_invalid_chars"], False, "PAN has invalid characters")
    uppercase = compact.upper()
    if uppercase != compact:
        if PAN_RE.match(uppercase):
            return PANCheck(
                False,
                uppercase,
                "pan_lowercase",
                ERROR_MESSAGES["pan_lowercase"],
                True,
                "PAN contains lowercase letters; only letter case is wrong",
            )
        return PANCheck(False, compact, "pan_format", ERROR_MESSAGES["pan_format"], False, "PAN character positions are invalid")
    if not PAN_RE.match(compact):
        return PANCheck(False, compact, "pan_format", ERROR_MESSAGES["pan_format"], False, "PAN character positions are invalid")
    return PANCheck(True, compact, None, "", False, "PAN format is valid")


def mock_verify_pan(normalized: str) -> tuple[bool, str | None, str]:
    """Deterministic stand-in for an income-tax PAN lookup."""
    if normalized in _NOT_FOUND_PANS:
        return False, "pan_not_found", ERROR_MESSAGES["pan_not_found"]
    return True, None, "PAN verified against records."
