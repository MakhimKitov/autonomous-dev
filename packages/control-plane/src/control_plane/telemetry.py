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
from urllib.parse import quote

import structlog
from dev_agent import env as agentenv
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from control_plane.models import BurstRecord

SERVICE_NAME = "control-plane"
_initialized = False

# Where a *burst* pushes its telemetry — deliberately not the control plane's own
# `OTEL_EXPORTER_OTLP_ENDPOINT`. The two processes are in different network
# namespaces, so the same string rarely names the same collector: `localhost` inside
# the box is the box. Unset means the burst gets no telemetry configuration at all,
# which is the same presence-toggle shape as `LEDGER_DSN` and `ARCHIVE_URL`.
AGENT_OTLP_ENDPOINT_ENV = "DEV_AGENT_OTLP_ENDPOINT"

# The CLI's tracing is beta: "span names and attributes may change between
# releases". This is the version the span nesting was verified against.
VERIFIED_CLI_VERSION = "2.1.235"


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


def _resource_attributes(pairs: dict[str, str]) -> str:
    """W3C-Baggage-shaped `key=value,key=value`, values percent-encoded.

    Comma, space and `=` are the separators, so a value containing one would split
    the list — the CLI's docs say to encode them. `/` and `#` are left alone
    deliberately: they are in every task id, they are not separators, and a
    dashboard filter on `task.id` should read the id and not a mangling of it.
    """
    return ",".join(f"{key}={quote(value, safe='/#')}" for key, value in pairs.items())


def agent_telemetry_env(task_id: str, burst_id: str, role: str, kind: str) -> dict[str, str]:
    """Claude Code's own OTel, configured for one burst (B2, #20).

    Empty unless `DEV_AGENT_OTLP_ENDPOINT` names a collector the *container* can
    reach: no endpoint, no telemetry variables, and the box is exactly what it was.

    Two signals, and the third one's absence is the interesting part:

    * **traces** — the point of this. The CLI adopts the `TRACEPARENT` the runner
      already injects, so `claude_code.interaction` and its children land inside the
      burst span instead of in a trace of their own.
    * **log events** — `tool_result`, `api_request`, `user_prompt`: tool visibility
      *is* the experiment's dataset (ARCHITECTURE §9). The violations ledger is still
      derived control-plane-side from the volume, never from these — an alerting
      path that depends on an adopter's log pipeline isn't a wall.
    * **metrics** — not enabled, and not an omission. TO-BE §12: the ledger *is* the
      metrics store, and "no emitted-metrics pipeline — that would mirror ledger
      facts and drift". The CLI's counters are cost and tokens, which `bursts` already
      records as the authoritative copy; B3's reporting views are how they're read.
      An adopter who wants them adds `OTEL_METRICS_EXPORTER` and owns the divergence.

    Content: arguments yes, bodies no. `OTEL_LOG_TOOL_DETAILS` puts tool inputs —
    paths, commands, patterns — on the events, which is the dataset. Tool *bodies*
    (`OTEL_LOG_TOOL_CONTENT`) and raw API bodies stay off: §9 says spans stay thin
    because the session transcript in the archive is the deep record, and raw bodies
    carry the whole conversation into a second store with its own retention.

    Nothing here may be `console`: the SDK uses the CLI's stdout as its message
    channel, so a console exporter corrupts the burst rather than exporting it.
    """
    endpoint = os.environ.get(AGENT_OTLP_ENDPOINT_ENV, "").strip()
    if not endpoint:
        return {}
    return {
        agentenv.CLAUDE_CODE_ENABLE_TELEMETRY: "1",
        agentenv.CLAUDE_CODE_ENHANCED_TELEMETRY_BETA: "1",  # traces are beta-gated
        agentenv.OTEL_TRACES_EXPORTER: "otlp",
        agentenv.OTEL_LOGS_EXPORTER: "otlp",
        agentenv.OTEL_EXPORTER_OTLP_PROTOCOL: "http/protobuf",
        agentenv.OTEL_EXPORTER_OTLP_ENDPOINT: endpoint,
        # A burst is short and can be killed mid-run by the budget rail, which loses
        # whatever is still in the batch buffer. The CLI's own advice for short-lived
        # calls is to shorten the intervals; the default 5s would lose most bursts.
        agentenv.OTEL_TRACES_EXPORT_INTERVAL: "1000",
        agentenv.OTEL_LOGS_EXPORT_INTERVAL: "1000",
        agentenv.OTEL_SERVICE_NAME: agentenv.AGENT_SERVICE_NAME,
        # The same four attributes the burst span carries, so a query written against
        # one signal works on the other.
        agentenv.OTEL_RESOURCE_ATTRIBUTES: _resource_attributes(
            {"task.id": task_id, "burst.id": burst_id, "burst.role": role, "burst.kind": kind}
        ),
        agentenv.OTEL_LOG_USER_PROMPTS: "1",
        agentenv.OTEL_LOG_TOOL_DETAILS: "1",
    }


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
