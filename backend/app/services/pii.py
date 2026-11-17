"""PII tokenization with format-preserving surrogates.

Why surrogates?
---------------
Pure tokenization (replace PAN ``ABCDE1234F`` with ``⟦PAN_7f3a⟧``) prevents the
model from reasoning about *format defects* — the exact thing the copilot must
fix. So sensitive values are replaced with **surrogates**: same length, same
character classes, same casing, same separators, but fake characters.

    real      abcde1234f      (lowercase -> fails ^[A-Z]{5}[0-9]{4}[A-Z]$)
    surrogate qwert1234a      (also lowercase -> model sees the real defect)

The model can only ever return one of:
  * the token itself                      -> rehydrated from the encrypted vault
  * an unchanged surrogate                -> mapped back to the real value
  * a case/separator transform of it      -> the SAME transform applied to the real value
  * a value literally present in the user's message -> accepted as user-provided

Anything else is rejected as a hallucinated value. That makes "never invent PAN
characters" a mechanical guarantee rather than a prompt instruction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import string
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.entities import PIIRecord
from app.schemas import SENSITIVE_FIELDS

logger = get_logger(__name__)

TOKEN_RE = re.compile(r"⟦([A-Z_]+)_([a-f0-9]{4})⟧")
SEPARATORS_RE = re.compile(r"[\s\-./,]")

LETTERS = string.ascii_lowercase
DIGITS = string.digits

# Free-text detectors applied to the user's message before it reaches the LLM.
_TEXT_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PAN", re.compile(r"\b[A-Za-z]{5}[0-9]{4}[A-Za-z]\b")),
    ("AADHAAR", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
    ("PHONE", re.compile(r"\b(?:\+91[-\s]?)?[6-9]\d{9}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("ACCOUNT", re.compile(r"\b\d{9,18}\b")),
)


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def fernet() -> Fernet:
    """Public accessor (same key derivation as the vault)."""
    return _fernet()


def _h(secret: str, *parts: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), "|".join(parts).encode("utf-8"), hashlib.sha256).digest()


def value_hash(value: str, field: str | None = None) -> str:
    """Non-reversible fingerprint used for audit + client execution checks."""
    suffix = f":{field}" if field else ""
    return hmac.new(
        settings.secret_key.encode("utf-8"), (value + suffix).encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def preview_mask(value: str | None, keep: int = 0) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "•" * len(value)
    return value[:keep] + "•" * (len(value) - keep * 2) + value[-keep:] if keep else "•" * len(value)


def _surrogate_char(ch: str, digest: bytes, index: int) -> str:
    if ch.isalpha():
        alphabet = LETTERS if ch.islower() else LETTERS.upper()
        return alphabet[digest[index % len(digest)] % 26]
    if ch.isdigit():
        return DIGITS[digest[index % len(digest)] % 10]
    return ch


def make_surrogate(field: str, value: str, token_suffix: str) -> str:
    """Deterministic, shape-preserving stand-in for a sensitive value."""
    digest = _h(settings.secret_key, "surrogate", field, token_suffix, value)
    # Expand digest deterministically if the value is longer than 32 chars.
    stream = digest * (len(value) // len(digest) + 2)
    surrogate = "".join(_surrogate_char(ch, stream, i) for i, ch in enumerate(value))
    # A PIN's leading zero is a validation defect, so it must be preserved.
    # Conversely a valid non-zero leading digit must not become a false defect.
    if field == "pin" and value and value[0].isdigit():
        chars = list(surrogate)
        if value[0] == "0":
            chars[0] = "0"
        elif chars[0] == "0":
            chars[0] = "1"
        surrogate = "".join(chars)
    return surrogate


@dataclass(frozen=True)
class VaultEntry:
    token: str
    field: str
    surrogate: str
    real: str


@dataclass(frozen=True)
class FillResolution:
    ok: bool
    value: str | None
    source: str
    reason: str
    transform: str = "none"


class PIIVault:
    """Per-session vault. Values are Fernet-encrypted in the database."""

    def __init__(self, db: Session, session_id: str, *, create_if_missing: bool = True):
        self.db = db
        self.session_id = session_id
        self.create_if_missing = create_if_missing
        self._by_token: dict[str, VaultEntry] = {}
        self._by_field: dict[str, VaultEntry] = {}
        self._fernet = _fernet()
        self._loaded = False

    # -- internals ---------------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        rows = self.db.scalars(
            select(PIIRecord).where(PIIRecord.session_id == self.session_id).order_by(PIIRecord.created_at)
        ).all()
        for row in rows:
            try:
                real = self._fernet.decrypt(row.ciphertext.encode()).decode()
            except InvalidToken:
                logger.warning("pii.vault.decrypt_failed", extra={"extra": {"field": row.field}})
                continue
            entry = VaultEntry(token=row.token, field=row.field, surrogate=row.surrogate, real=real)
            self._by_token[row.token] = entry
            self._by_field[row.field] = entry  # later rows win (latest value)
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()

    # -- tokenize ----------------------------------------------------------
    def tokenize(self, field: str, value: str | None) -> VaultEntry | None:
        if value is None or value == "":
            return None
        self._ensure_loaded()
        suffix = _h(settings.secret_key, "token", self.session_id, field, value).hex()[:4]
        label = SENSITIVE_FIELDS.get(field, "PII")
        token = f"⟦{label}_{suffix}⟧"

        existing = self._by_token.get(token)
        if existing and existing.real == value:
            self._by_field[field] = existing
            return existing

        surrogate = make_surrogate(field, value, suffix)
        entry = VaultEntry(token=token, field=field, surrogate=surrogate, real=value)
        if self.create_if_missing:
            ciphertext = self._fernet.encrypt(value.encode()).decode()
            row = self.db.scalar(
                select(PIIRecord).where(
                    PIIRecord.session_id == self.session_id,
                    PIIRecord.field == field,
                    PIIRecord.ciphertext == ciphertext,
                )
            )
            if row is None:
                self.db.add(
                    PIIRecord(
                        token=token,
                        session_id=self.session_id,
                        field=field,
                        ciphertext=ciphertext,
                        surrogate=surrogate,
                    )
                )
                self.db.flush()
            else:
                token = row.token
                entry = VaultEntry(token=row.token, field=field, surrogate=row.surrogate, real=value)
        self._by_token[entry.token] = entry
        self._by_field[field] = entry
        return entry

    # -- reads -------------------------------------------------------------
    def entry_for_field(self, field: str) -> VaultEntry | None:
        self._ensure_loaded()
        return self._by_field.get(field)

    def resolve_token(self, token: str) -> str | None:
        self._ensure_loaded()
        entry = self._by_token.get(token)
        return entry.real if entry else None

    def rehydrate_text(self, text: str) -> str:
        """Replace tokens and surrogates in user-facing copy with real values.

        The planner may echo a surrogate verbatim, or apply one of the two
        transforms it is allowed to ask for (case normalisation, separator
        removal). All three forms map back to the same real value.
        """
        if not text:
            return text
        self._ensure_loaded()
        out = text
        for entry in self._by_token.values():
            out = out.replace(entry.token, entry.real)

        pairs: list[tuple[str, str]] = []
        for entry in self._by_token.values():
            if len(entry.surrogate) < 4:
                continue
            pairs.append((entry.surrogate, entry.real))
            pairs.append((entry.surrogate.upper(), entry.real.upper()))
            pairs.append((entry.surrogate.lower(), entry.real.lower()))
            compact_surrogate = _strip_separators(entry.surrogate)
            if compact_surrogate != entry.surrogate:
                pairs.append((compact_surrogate, _strip_separators(entry.real)))

        # Longest form first so a compact variant cannot clobber a spaced one.
        seen: set[str] = set()
        for variant, replacement in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
            if not variant or variant in seen:
                continue
            seen.add(variant)
            out = out.replace(variant, replacement)
        return out

    def mask_value(self, field: str, value: str | None) -> str | None:
        entry = self.tokenize(field, value) if value else None
        return entry.token if entry else value

    def mask_message(self, message: str) -> str:
        """Mask identifier shapes in free text before it is sent to an LLM."""
        if not message:
            return message
        self._ensure_loaded()

        def repl(label: str):
            def _inner(match: re.Match[str]) -> str:
                raw = match.group(0)
                field = {
                    "PAN": "pan",
                    "AADHAAR": "aadhaar",
                    "PHONE": "phone",
                    "EMAIL": "email",
                    "ACCOUNT": "account_number",
                }[label]
                # Reuse the vault entry of the same field when it is literally
                # the same value, otherwise create a message-scoped token.
                entry = self._by_field.get(field)
                if entry and entry.real == raw:
                    return entry.token
                suffix = _h(settings.secret_key, "msg", self.session_id, field, raw).hex()[:4]
                token = f"⟦{label}_{suffix}⟧"
                if token not in self._by_token:
                    surrogate = make_surrogate(field, raw, suffix)
                    self._by_token[token] = VaultEntry(token=token, field=field, surrogate=surrogate, real=raw)
                return token

            return _inner

        out = message
        for label, pattern in _TEXT_DETECTORS:
            out = pattern.sub(repl(label), out)
        return out

    # -- resolution --------------------------------------------------------
    def resolve_fill(
        self,
        field: str,
        proposed: str | None,
        *,
        current_value: str | None,
        raw_user_message: str,
        allowed_values: list[str] | None = None,
    ) -> FillResolution:
        """Authorize a fill value. This is the anti-hallucination gate."""
        if proposed is None or proposed.strip() == "":
            return FillResolution(False, None, "rejected", "empty value")

        proposed_s = proposed.strip()
        current = current_value or ""

        if field in SENSITIVE_FIELDS:
            entry = self.tokenize(field, current) if current else self._by_field.get(field)
            self._ensure_loaded()
            if entry is None:
                entry = self._by_field.get(field)

            # 1. token echo
            token_value = self.resolve_token(proposed_s)
            if token_value is not None:
                return FillResolution(True, token_value, "token", "rehydrated vault token")

            # 2. unchanged surrogate
            if entry and proposed_s == entry.surrogate:
                return FillResolution(True, entry.real, "surrogate", "unchanged surrogate")

            # 3. case / separator transform of the surrogate
            if entry:
                derived = _derive_transform(entry.real, entry.surrogate, proposed_s)
                if derived is not None:
                    return FillResolution(True, derived, "surrogate_transform", "structural transform", "case/separator")

            # 4. user-provided value (case-insensitive containment)
            if _appears_in_message(proposed_s, raw_user_message):
                return FillResolution(True, proposed_s, "user_message", "value provided by the user")

            return FillResolution(
                False,
                None,
                "rejected",
                "value is not derivable from the user's input or the current field value",
            )

        # Non-sensitive field (city/state/street text): allow only authorised
        # suggestions or values the user actually typed.
        if allowed_values:
            canonical = {opt.lower(): opt for opt in allowed_values}
            if proposed_s.lower() in canonical:
                return FillResolution(True, canonical[proposed_s.lower()], "suggestion", "matched suggestion")
        if _appears_in_message(proposed_s, raw_user_message):
            return FillResolution(True, proposed_s, "user_message", "value provided by the user")
        return FillResolution(False, None, "rejected", "value not in authorised suggestions or user message")


def _strip_separators(value: str) -> str:
    return SEPARATORS_RE.sub("", value)


def _derive_transform(real: str, surrogate: str, proposed: str) -> str | None:
    """Apply the surrogate->proposed edit to `real`, only for case/separator edits.

    Supports the two corrections that are safe and useful for KYC formats:
    case normalisation (lowercase PAN) and separator removal ("560 001").
    Any character substitution is refused so the model cannot invent digits.
    """
    s = _strip_separators(surrogate)
    p = _strip_separators(proposed)
    r = _strip_separators(real)
    if not (len(s) == len(p) == len(r)):
        return None
    out: list[str] = []
    changed = False
    for sc, pc, rc in zip(s, p, r, strict=True):
        if sc == pc:
            out.append(rc)
            continue
        if sc.lower() == pc.lower() and sc.isalpha() and pc.isalpha():
            out.append(rc.upper() if pc.isupper() else rc.lower())
            changed = True
            continue
        return None
    if not changed and not (s != surrogate or p != proposed):
        return None
    # Re-add the separators from the original real value when the proposal kept
    # them; for separator removal we return the compact form on purpose.
    if _strip_separators(proposed) != proposed:
        return "".join(out)
    if len(proposed) == len(surrogate):
        rebuilt: list[str] = []
        compact_index = 0
        for ch in real:
            if SEPARATORS_RE.match(ch):
                rebuilt.append(ch)
            else:
                rebuilt.append(out[compact_index])
                compact_index += 1
        return "".join(rebuilt)
    return "".join(out)


def _appears_in_message(value: str, message: str) -> bool:
    if not message:
        return False
    needle = _strip_separators(value).lower()
    haystack = _strip_separators(message).lower()
    return bool(needle) and needle in haystack


def mask_context(context: dict[str, Any], vault: PIIVault) -> dict[str, Any]:
    """Return a deep-ish copy of the screen context safe for an LLM prompt."""
    masked = {k: v for k, v in context.items() if k not in {"fields"}}
    fields: list[dict[str, Any]] = []
    for raw_field in context.get("fields", []):
        field = dict(raw_field)
        name = field.get("name", "")
        if field.get("sensitive") or name in SENSITIVE_FIELDS:
            value = field.get("value")
            entry = vault.tokenize(name, value) if value else None
            field["value"] = entry.surrogate if entry else None
            field["token"] = entry.token if entry else None
            field["value_is_surrogate"] = bool(entry)
            field["sensitive"] = True
        fields.append(field)
    masked["fields"] = fields
    return masked
