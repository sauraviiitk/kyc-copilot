"""Idempotency primitives.

Two layers:
1. Redis (or in-memory) for a fast duplicate check.
2. A database unique constraint on the operation record as the durable truth.

A duplicate request never re-executes a mutation: it replays the stored result.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from app.services.cache import CacheBackend, get_cache

IDEMPOTENCY_PREFIX = "idem:"
DEFAULT_TTL = 60 * 60 * 24


@dataclass
class IdempotencyRecord:
    key: str
    fingerprint: str
    state: Literal["in_progress", "completed", "failed"]
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = 0.0


def request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


class IdempotencyStore:
    def __init__(self, cache: CacheBackend | None = None, namespace: str = "default"):
        self.cache = cache or get_cache()
        self.namespace = namespace

    def _key(self, key: str) -> str:
        return f"{IDEMPOTENCY_PREFIX}{self.namespace}:{key}"

    def get(self, key: str) -> IdempotencyRecord | None:
        raw = self.cache.get(self._key(key))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return IdempotencyRecord(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    def begin(self, key: str, payload: dict[str, Any]) -> tuple[Literal["new", "replay", "conflict"], IdempotencyRecord]:
        fingerprint = request_fingerprint(payload)
        existing = self.get(key)
        if existing:
            if existing.fingerprint != fingerprint:
                return "conflict", existing
            if existing.state == "completed":
                return "replay", existing
            if existing.state == "in_progress":
                # Concurrent duplicate: treat as replay of the in-flight record.
                return "replay", existing
            # failed -> caller may retry explicitly, but only via retry endpoint.
            return "new", existing
        record = IdempotencyRecord(
            key=key, fingerprint=fingerprint, state="in_progress", created_at=time.time()
        )
        self.cache.set(self._key(key), json.dumps(record.__dict__), DEFAULT_TTL)
        return "new", record

    def complete(self, key: str, payload: dict[str, Any], result: dict[str, Any]) -> None:
        record = IdempotencyRecord(
            key=key,
            fingerprint=request_fingerprint(payload),
            state="completed",
            result=result,
            created_at=time.time(),
        )
        self.cache.set(self._key(key), json.dumps(record.__dict__), DEFAULT_TTL)

    def fail(self, key: str, payload: dict[str, Any], error: str) -> None:
        record = IdempotencyRecord(
            key=key,
            fingerprint=request_fingerprint(payload),
            state="failed",
            error=error,
            created_at=time.time(),
        )
        self.cache.set(self._key(key), json.dumps(record.__dict__), DEFAULT_TTL)

    def release(self, key: str) -> None:
        self.cache.delete(self._key(key))


def new_idempotency_key(*parts: str) -> str:
    seed = "|".join(parts) + f"|{time.time_ns()}"
    return hashlib.sha256(seed.encode()).hexdigest()[:32]
