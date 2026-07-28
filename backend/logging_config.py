"""Structured logging setup.

JSON in production, human-readable colour locally — the same log line, rendered
for two different readers. Structured output matters here specifically because
the interesting questions are aggregate ones ("which framework's ingests are
failing to parse?", "what is the p95 agent latency?"), and those are only
answerable if fields are fields rather than substrings of a message.
"""

from __future__ import annotations

import logging
import sys

import structlog

from backend.config import Settings


def configure_logging(settings: Settings) -> None:
    """Route stdlib logging through structlog.

    Everything in this codebase uses ``logging.getLogger(__name__)`` rather than
    a structlog logger directly, so that third-party libraries (SQLAlchemy,
    uvicorn, httpx) end up in the same pipeline instead of a second, differently
    formatted stream.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Quieten the libraries that log a line per operation. SQLAlchemy's engine
    # logger in particular emits every statement at INFO, which buries our own
    # logs under query text within seconds of real traffic.
    for noisy, noisy_level in (
        ("sqlalchemy.engine", logging.WARNING),
        ("uvicorn.access", logging.WARNING),
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(noisy_level)
