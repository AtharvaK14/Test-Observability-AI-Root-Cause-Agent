"""Small, dependency-free helpers shared across the backend.

Kept deliberately tiny so that importing it never drags in the database,
HTTP, or Anthropic layers — which keeps unit tests fast.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Timezone-aware UTC 'now'.

    ``datetime.utcnow()`` is deprecated from Python 3.12 and — worse — returns a
    *naive* datetime. Mixing naive and aware datetimes raises ``TypeError`` at
    comparison time, which in a test-observability system surfaces as a
    mysterious 500 on a trend endpoint months after ingestion. Every timestamp
    in this codebase is UTC-aware; there is exactly one place to get one.
    """
    return datetime.now(UTC)


def new_id() -> str:
    """Generate a primary key.

    UUID4 strings rather than DB sequences: test results are produced by many
    concurrent CI runners, and client-generated ids let a runner build the whole
    payload (including foreign key references) before it ever talks to the API.
    """
    return str(uuid.uuid4())


def truncate(text: str | None, limit: int, suffix: str = "... [truncated]") -> str | None:
    """Clamp free-form text to ``limit`` characters, flagging that it was cut.

    Stack traces and CI logs are routinely megabytes. Silently slicing them
    (``text[:500]``) makes the LLM believe it saw the whole trace and reason
    confidently off a fragment, so the truncation is always made explicit.
    """
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + suffix
