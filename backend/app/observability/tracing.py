"""Optional OpenTelemetry tracing.

Enabled with OTEL_ENABLED=true. When the SDK or collector is absent this module
is a no-op so tracing can never break a request.
"""

from __future__ import annotations

from contextlib import contextmanager

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)
_tracer = None


def init_tracing(app=None) -> bool:
    global _tracer
    if not settings.otel_enabled:
        return False
    try:  # pragma: no cover - optional dependency
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "kyc-copilot"}))
        if settings.otel_exporter_otlp_endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
            )
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("kyc-copilot")
        if app is not None:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                FastAPIInstrumentor.instrument_app(app)
            except Exception:
                pass
        logger.info("otel.enabled")
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("otel.unavailable: %s", exc)
        return False


@contextmanager
def span(name: str, **attributes):
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as current:  # pragma: no cover
        for key, value in attributes.items():
            try:
                current.set_attribute(key, value)
            except Exception:
                pass
        yield current
