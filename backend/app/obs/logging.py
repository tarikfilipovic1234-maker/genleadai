"""Structured logging.

We use structlog rather than the stdlib logger because every later milestone
emits *events with fields* - a tool call with its name, duration and outcome; a
lead with its score and provenance counts - and those fields need to survive
into the log rather than being flattened into an f-string.

Local runs get colourised key/value output. Production emits one JSON object
per line, which is what log aggregators expect.
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    """Install the structlog + stdlib pipeline. Idempotent."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Route stdlib logging (uvicorn, sqlalchemy, httpx) through the same sink
    # so we do not end up with two competing log formats on one stream.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "httpx"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if settings.log_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
        shared.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Use ``get_logger(__name__)``."""
    return structlog.get_logger(name)
