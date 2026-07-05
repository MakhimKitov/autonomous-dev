"""Structured logging for both processes — slog-shaped: an event phrase plus
``key=value`` pairs.

Lives in dev-agent because the seam only allows imports this way (control-plane
imports dev-agent, never the reverse — see ``dev_agent.env``). The renderer is
picked by where stdout goes, no knobs: a TTY gets the colorized console view
(an operator watching the loop), anything else gets logfmt (burst containers,
whose stdout is teed to ``data/logs/``, and CI). Agent prose and container
output stay raw ``print`` — they are payload, not log records.
"""

from __future__ import annotations

import sys

import structlog


def configure_logging() -> None:
    pretty = sys.stdout.isatty()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S" if pretty else "iso", utc=not pretty),
            structlog.dev.ConsoleRenderer()
            if pretty
            else structlog.processors.LogfmtRenderer(key_order=["timestamp", "level", "event"]),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
