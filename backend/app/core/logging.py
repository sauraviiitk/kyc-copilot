"""Structured JSON logging with a PII-scrubbing filter.

Rules enforced here:
* every log line is JSON with a level, logger, message and optional fields;
* PAN / Aadhaar / account / phone / e-mail shapes are redacted defensively even
  if a caller accidentally passes raw text;
* request id / run id are attached through the `extra` dict.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
run_id_ctx: ContextVar[str | None] = ContextVar("run_id", default=None)

PAN_RE = re.compile(r"\b[A-Za-z]{5}[0-9]{4}[A-Za-z]\b")
AADHAAR_RE = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
PHONE_RE = re.compile(r"\b(?:\+91[-\s]?)?[6-9]\d{9}\b")
ACCOUNT_RE = re.compile(r"\b\d{9,18}\b")
TOKEN_RE = re.compile(r"⟦[A-Z_]+_[a-f0-9]{4}⟧")

_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (PAN_RE, "[REDACTED_PAN]"),
    (AADHAAR_RE, "[REDACTED_AADHAAR]"),
    (EMAIL_RE, "[REDACTED_EMAIL]"),
    (PHONE_RE, "[REDACTED_PHONE]"),
    (ACCOUNT_RE, "[REDACTED_NUMBER]"),
)


def redact_pii(text: str) -> str:
    """Best-effort redaction used by logs and error envelopes."""
    if not text:
        return text
    out = text
    for pattern, replacement in _PII_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value


class PIIFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_pii(record.msg)
        if record.args:
            try:
                record.args = tuple(
                    redact_obj(a) if isinstance(a, (str, dict, list)) else a for a in record.args
                )
            except Exception:  # pragma: no cover - defensive
                pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if (rid := request_id_ctx.get()) is not None:
            payload["request_id"] = rid
        if (rid := run_id_ctx.get()) is not None:
            payload["run_id"] = rid
        for key in ("event", "session_id", "run_id", "status", "duration_ms", "extra"):
            if hasattr(record, key):
                payload[key] = redact_obj(getattr(record, key))
        if record.exc_info:
            payload["exception"] = redact_pii(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_kyc_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(PIIFilter())
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "chromadb", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._kyc_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
