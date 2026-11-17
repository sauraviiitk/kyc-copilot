"""In-process observability: counters, histograms, cost accounting.

Deliberately dependency-free so `/metrics` works in every deployment. Optional
OpenTelemetry tracing is wired in `app.observability.tracing`.
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings

_lock = threading.Lock()
_counters: Counter[str] = Counter()
_histograms: dict[str, list[float]] = defaultdict(list)
_gauges: dict[str, float] = {}
_MAX_SAMPLES = 2000


def incr(name: str, value: float = 1.0, **labels: Any) -> None:
    key = _label(name, labels)
    with _lock:
        _counters[key] += value


def gauge(name: str, value: float, **labels: Any) -> None:
    with _lock:
        _gauges[_label(name, labels)] = value


def observe(name: str, value_ms: float, **labels: Any) -> None:
    key = _label(name, labels)
    with _lock:
        samples = _histograms[key]
        samples.append(float(value_ms))
        if len(samples) > _MAX_SAMPLES:
            del samples[: len(samples) - _MAX_SAMPLES]


@contextmanager
def timer(name: str, **labels: Any):
    start = time.perf_counter()
    try:
        yield
    finally:
        observe(name, (time.perf_counter() - start) * 1000, **labels)


def _label(name: str, labels: dict[str, Any]) -> str:
    if not labels:
        return name
    suffix = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{suffix}}}"


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    rank = max(0, min(len(sorted_values) - 1, math.ceil((pct / 100) * len(sorted_values)) - 1))
    return sorted_values[int(rank)]


def histogram_stats(name: str) -> dict[str, float]:
    with _lock:
        samples = sorted(_histograms.get(name, []))
    if not samples:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "avg": 0.0, "max": 0.0}
    return {
        "count": len(samples),
        "p50": round(_percentile(samples, 50), 2),
        "p95": round(_percentile(samples, 95), 2),
        "avg": round(sum(samples) / len(samples), 2),
        "max": round(samples[-1], 2),
    }


def snapshot() -> dict[str, Any]:
    with _lock:
        counters = dict(_counters)
        hist_names = list(_histograms.keys())
        gauges = dict(_gauges)
    return {
        "counters": counters,
        "histograms": {name: histogram_stats(name) for name in hist_names},
        "gauges": gauges,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000) * settings.price_input_per_mtok
        + (output_tokens / 1_000_000) * settings.price_output_per_mtok,
        6,
    )


def reset_metrics() -> None:
    with _lock:
        _counters.clear()
        _histograms.clear()
        _gauges.clear()
