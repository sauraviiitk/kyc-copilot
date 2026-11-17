"""SSE event hub.

Events are appended to the run in two places:
* AgentRun.events (durable, for replay and the developer dashboard)
* this hub (live, so an open EventSource gets them immediately)

The hub is thread-safe because agent execution happens in a worker thread while
FastAPI's async SSE endpoint reads from it.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

MAX_EVENTS_PER_RUN = 200


@dataclass
class RunStream:
    history: list[dict[str, Any]] = field(default_factory=list)
    queues: list[asyncio.Queue] = field(default_factory=list)
    closed: bool = False
    loop: asyncio.AbstractEventLoop | None = None


class EventHub:
    def __init__(self) -> None:
        self._streams: dict[str, RunStream] = {}
        self._lock = threading.RLock()

    def register(self, run_id: str, loop: asyncio.AbstractEventLoop | None = None) -> None:
        with self._lock:
            stream = self._streams.setdefault(run_id, RunStream())
            if loop is not None:
                stream.loop = loop

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        event = {**event, "ts": event.get("ts") or time.time()}
        with self._lock:
            stream = self._streams.setdefault(run_id, RunStream())
            stream.history.append(event)
            if len(stream.history) > MAX_EVENTS_PER_RUN:
                stream.history = stream.history[-MAX_EVENTS_PER_RUN:]
            queues = list(stream.queues)
            loop = stream.loop
        for queue in queues:
            self._deliver(queue, event, loop)

    @staticmethod
    def _deliver(queue: asyncio.Queue, event: dict[str, Any], loop: asyncio.AbstractEventLoop | None) -> None:
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
                return
            except RuntimeError:  # pragma: no cover - loop closed
                pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover
            pass

    def subscribe(self, run_id: str, loop: asyncio.AbstractEventLoop | None = None) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            stream = self._streams.setdefault(run_id, RunStream())
            if loop is not None:
                stream.loop = loop
            stream.queues.append(queue)
            history = list(stream.history) if stream.closed else list(stream.history)
            closed = stream.closed
        if closed:
            queue.put_nowait({"event": "stream_end", "data": {"reason": "already_finished"}})
        return queue, history

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            stream = self._streams.get(run_id)
            if stream and queue in stream.queues:
                stream.queues.remove(queue)

    def close(self, run_id: str) -> None:
        with self._lock:
            stream = self._streams.setdefault(run_id, RunStream())
            stream.closed = True
            queues = list(stream.queues)
            loop = stream.loop
        for queue in queues:
            self._deliver(queue, {"event": "stream_end", "data": {"reason": "complete"}, "ts": time.time()}, loop)

    def is_closed(self, run_id: str) -> bool:
        with self._lock:
            stream = self._streams.get(run_id)
            return bool(stream and stream.closed)

    def history(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            stream = self._streams.get(run_id)
            return list(stream.history) if stream else []

    def clear(self) -> None:
        with self._lock:
            self._streams.clear()


hub = EventHub()


def sse_format(event: dict[str, Any]) -> str:
    """Serialise one event hub entry into SSE wire format."""
    import json

    name = event.get("event", "message")
    data = event.get("data", {})
    return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"
