"""Redis-backed cache with a graceful in-memory fallback.

Redis is used for idempotency fast paths, mandate status caching and session
snapshots. The app must keep working (single-process) when Redis is absent, so
every operation degrades instead of raising.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Protocol

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class CacheBackend(Protocol):
    mode: str

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...

    def delete(self, key: str) -> None: ...

    def incr(self, key: str, ttl_seconds: int | None = None) -> int: ...

    def ping(self) -> bool: ...

    def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...

    def get_json(self, key: str) -> Any | None: ...


class MemoryCache:
    mode = "in-memory"

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.RLock()

    def _expired(self, expires_at: float | None) -> bool:
        return expires_at is not None and expires_at < time.time()

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            value, expires_at = item
            if self._expired(expires_at):
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        with self._lock:
            expires = time.time() + ttl_seconds if ttl_seconds else None
            self._store[key] = (value, expires)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def incr(self, key: str, ttl_seconds: int | None = None) -> int:
        with self._lock:
            current = self.get(key)
            next_value = int(current or "0") + 1
            self.set(key, str(next_value), ttl_seconds)
            return next_value

    def ping(self) -> bool:
        return True

    def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        self.set(key, json.dumps(value, default=str), ttl_seconds)

    def get_json(self, key: str) -> Any | None:
        raw = self.get(key)
        return json.loads(raw) if raw else None

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._store)


class RedisCache:
    mode = "redis"

    def __init__(self, client) -> None:
        self.client = client

    def get(self, key: str) -> str | None:
        try:
            return self.client.get(key)
        except Exception as exc:  # pragma: no cover - network
            logger.warning("cache.redis.get_failed: %s", exc)
            return None

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        try:
            self.client.set(key, value, ex=ttl_seconds)
        except Exception as exc:  # pragma: no cover
            logger.warning("cache.redis.set_failed: %s", exc)

    def delete(self, key: str) -> None:
        try:
            self.client.delete(key)
        except Exception:  # pragma: no cover
            pass

    def incr(self, key: str, ttl_seconds: int | None = None) -> int:
        try:
            value = int(self.client.incr(key))
            if value == 1 and ttl_seconds:
                self.client.expire(key, ttl_seconds)
            return value
        except Exception:  # pragma: no cover
            return 1

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:  # pragma: no cover
            return False

    def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        self.set(key, json.dumps(value, default=str), ttl_seconds)

    def get_json(self, key: str) -> Any | None:
        raw = self.get(key)
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:  # pragma: no cover
            return None


_lock = threading.Lock()
_cache: CacheBackend | None = None


def get_cache(*, force_memory: bool = False) -> CacheBackend:
    global _cache
    with _lock:
        if _cache is not None and not force_memory:
            return _cache
        if force_memory or not settings.redis_url:
            _cache = MemoryCache()
            return _cache
        try:
            import redis

            client = redis.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.75,
                socket_timeout=0.75,
                health_check_interval=30,
            )
            client.ping()
            _cache = RedisCache(client)
            logger.info("cache.redis.connected")
        except Exception as exc:  # pragma: no cover - depends on env
            logger.warning("cache.redis.unavailable: %s; using in-memory cache", exc)
            _cache = MemoryCache()
        return _cache


def reset_cache() -> None:
    """Test helper."""
    global _cache
    with _lock:
        _cache = None


def cache_health() -> dict[str, Any]:
    cache = get_cache()
    return {"mode": cache.mode, "ok": cache.ping()}
