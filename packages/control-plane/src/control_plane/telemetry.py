"""Tracing per ARCHITECTURE.md §9: trace = one burst, task is an attribute.

The control plane opens the burst span and injects its W3C ``traceparent`` into
the container env; Claude Code (SDK mode) adopts it, nesting its own span tree
under ours. Export is OTLP when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, console
when ``CONTROL_PLANE_TRACE_CONSOLE=1``, otherwise spans exist only to mint
trace context (no exporter) — correlation IDs cost nothing either way.

A task can run for days, so it is deliberately *not* a trace. What holds the
bursts of one task together is a **span link** per burst to the burst it resumed
from (B1, #19), which is why `burst_span` takes the predecessor's row rather than
just its ids: a link target is a pair, `(trace_id, span_id)`, and the ledger
records both halves of its own root span for exactly this reason.

The same span is where log correlation begins: the trace, burst and task ids go
into structlog's context for the span's lifetime, so every line the control plane
writes during a burst carries them without any call site passing them (TO-BE §12,
"correlation in-band").
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from control_plane.models import BurstRecord

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


def link_to_burst(parent: BurstRecord) -> trace.Link | None:
    """A link to the burst this one resumed from, or ``None`` if it can't be made.

    A W3C link names a *span*, not a trace, so both columns have to be there. They
    aren't always: any burst recorded before B1 has a `trace_id` and no `span_id`,
    and a burst run with tracing off has neither. Returning ``None`` then is the
    honest answer — a link with a zero span id resolves to nothing in a viewer,
    which is worse than an absent link because it looks like a broken parent
    instead of an unrecorded one.

    The predecessor's trace is marked sampled because it is ours and it was: a
    trace we exported at all, we exported whole (no head sampler in the provider
    above). Overstating this would only mislead a backend into fetching a trace it
    already has.
    """
    if not parent.trace_id or not parent.span_id:
        return None
    try:
        context = trace.SpanContext(
            trace_id=int(parent.trace_id, 16),
            span_id=int(parent.span_id, 16),
            is_remote=True,
            trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
        )
    except ValueError:  # not hex — a hand-edited row, not a runtime state
        return None
    if not context.is_valid:  # all-zero ids parse fine and mean "no span"
        return None
    return trace.Link(
        context,
        {"burst.id": parent.burst_id, "burst.role": parent.role, "burst.link": "resumed_from"},
    )


@contextmanager
def burst_span(
    task_id: str,
    burst_id: str,
    role: str,
    kind: str,
    model: str | None = None,
    parent: BurstRecord | None = None,
) -> Iterator[tuple[trace.Span, str]]:
    """Open the burst's root span; yields (span, traceparent-for-the-container).

    ``parent`` is the burst this one resumed from — the leaf of the task's session
    tree, which the caller is already holding because it materialized the volume
    from it. It becomes a span link, and its id becomes an attribute too: not every
    viewer renders links, and the lineage is worth having in the ones that don't.
    """
    tracer = trace.get_tracer(SERVICE_NAME)
    attributes = {
        "task.id": task_id,
        "burst.id": burst_id,
        "burst.role": role,
        "burst.kind": kind,
    }
    if model:
        attributes["burst.model"] = model
    if parent is not None:
        attributes["burst.resumed_from"] = parent.burst_id
    link = None if parent is None else link_to_burst(parent)
    with tracer.start_as_current_span(
        "burst", attributes=attributes, links=[link] if link else None
    ) as span:
        ctx = span.get_span_context()
        # Bound for the span's lifetime, not the process's: a scheduler tick runs
        # bursts one after another, and a stale trace_id on a later line is a worse
        # lie than no trace_id at all. reset_contextvars restores what was there
        # before, so nesting can't leak either.
        tokens = structlog.contextvars.bind_contextvars(
            trace_id=f"{ctx.trace_id:032x}", burst_id=burst_id, task_id=task_id
        )
        try:
            yield span, traceparent_of(span)
        finally:
            structlog.contextvars.reset_contextvars(**tokens)
