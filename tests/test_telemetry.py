"""Tracing: burst span attributes + W3C traceparent minting."""

import re

from control_plane.telemetry import burst_span, init_telemetry

TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def test_burst_span_mints_valid_traceparent():
    init_telemetry()
    with burst_span("42", "b1", "dev", "work") as (span, traceparent):
        assert TRACEPARENT_RE.match(traceparent)
        ctx = span.get_span_context()
        assert f"{ctx.trace_id:032x}" in traceparent
        assert f"{ctx.span_id:016x}" in traceparent


def test_each_burst_gets_its_own_trace():
    init_telemetry()
    with burst_span("42", "b1", "dev", "work") as (_, tp1):
        pass
    with burst_span("42", "b2", "dev", "review") as (_, tp2):
        pass
    assert tp1.split("-")[1] != tp2.split("-")[1]  # trace ids differ (trace = burst)
