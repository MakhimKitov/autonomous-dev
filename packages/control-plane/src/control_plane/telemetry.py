"""Tracing per ARCHITECTURE.md §9: trace = one burst, task is an attribute.

The control plane opens the burst span and injects its W3C ``traceparent`` into
the container env; Claude Code (SDK mode) adopts it, nesting its own span tree
under ours. Export is OTLP when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, console
when ``CONTROL_PLANE_TRACE_CONSOLE=1``, otherwise spans exist only to mint
trace context (no exporter) — correlation IDs cost nothing either way.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

SERVICE_NAME = "control-plane"
_initialized = False


def init_telemetry() -> None:
    global _initialized
    if _initialized:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    elif os.environ.get("CONTROL_PLANE_TRACE_CONSOLE") == "1":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _initialized = True


def traceparent_of(span: trace.Span) -> str:
    ctx = span.get_span_context()
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{ctx.trace_flags:02x}"


@contextmanager
def burst_span(
    task_id: str, burst_id: str, role: str, kind: str
) -> Iterator[tuple[trace.Span, str]]:
    """Open the burst's root span; yields (span, traceparent-for-the-container)."""
    tracer = trace.get_tracer(SERVICE_NAME)
    with tracer.start_as_current_span(
        "burst",
        attributes={
            "task.id": task_id,
            "burst.id": burst_id,
            "burst.role": role,
            "burst.kind": kind,
        },
    ) as span:
        yield span, traceparent_of(span)
