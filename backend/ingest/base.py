"""Parser contract and shared normalisation helpers.

Every framework reports the same events in a different shape. The job here is
to erase that difference *without erasing information*, because the value of
the whole system depends on a Cypress failure and a PyTest failure being
comparable objects.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.models.enums import TestFramework, TestStatus
from backend.models.test_result import TestResultCreate

logger = logging.getLogger(__name__)


class ParseError(ValueError):
    """The uploaded report could not be understood.

    Carries the framework and a hint about what was expected, because "invalid
    JSON" in a CI log is unactionable — the person reading it needs to know
    which reporter to configure.
    """

    def __init__(self, message: str, framework: TestFramework | None = None) -> None:
        self.framework = framework
        super().__init__(message)


@dataclass
class RunMetadata:
    """Execution context supplied alongside the report, not inside it.

    Test reporters do not know the git SHA or the CI run id — that information
    lives in the pipeline's environment. It arrives as form fields and is
    stamped onto every result in the file. Without it, history is unqueryable
    and the pass→fail commit boundary cannot be computed.
    """

    ci_run_id: str = "local"
    environment: str = "staging"
    git_commit: str | None = None
    git_branch: str | None = None
    ci_provider: str | None = None
    ci_job_url: str | None = None


@dataclass
class ParsedReport:
    """The outcome of parsing one uploaded report."""

    framework: TestFramework
    results: list[TestResultCreate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    """Non-fatal problems: entries skipped, fields missing, shape guessed.

    Surfaced in the HTTP response rather than only logged. A CI step that
    uploads 400 results and is told "success" while 380 were dropped has
    negative value — it manufactures confidence in a number that is wrong.
    """

    @property
    def failure_count(self) -> int:
        return sum(1 for r in self.results if r.status in _FAILING)


_FAILING = frozenset({TestStatus.FAILED, TestStatus.FLAKY, TestStatus.ERROR})


class ReportParser(ABC):
    """Base class for framework report parsers."""

    framework: TestFramework

    @abstractmethod
    def parse(self, payload: bytes, metadata: RunMetadata) -> ParsedReport:
        """Turn raw report bytes into normalised results.

        Implementations raise ``ParseError`` when the file is not a report of
        the expected shape at all, and accumulate ``warnings`` for individual
        entries they had to skip. That distinction matters: a malformed file is
        a configuration problem the pipeline owner must fix, whereas one weird
        test entry should not discard the other 399.
        """

    # ------------------------------------------------------------- helpers

    @staticmethod
    def normalize_status(raw: str | None, *, framework: TestFramework) -> TestStatus:
        """Map a framework's outcome vocabulary onto ours.

        Unknown values become ``ERROR`` rather than being dropped or silently
        coerced to ``PASSED``. A status we do not recognise is a parser gap, and
        the safe direction to fail is "visible", not "green".
        """
        value = (raw or "").strip().lower()
        if value in _PASSED_WORDS:
            return TestStatus.PASSED
        if value in _FAILED_WORDS:
            return TestStatus.FAILED
        if value in _SKIPPED_WORDS:
            return TestStatus.SKIPPED
        if value in _FLAKY_WORDS:
            return TestStatus.FLAKY
        if value in _ERROR_WORDS:
            return TestStatus.ERROR
        logger.warning(
            "unrecognised test status", extra={"status": raw, "framework": framework.value}
        )
        return TestStatus.ERROR

    @staticmethod
    def resolve_retry_statuses(attempts: Sequence[TestResultCreate]) -> None:
        """Derive FLAKY across a test's retry attempts, in place.

        This is the point of storing one row per attempt. A test that failed and
        then passed on retry is reported by most runners as a pass — the suite
        goes green and nobody looks again. That is exactly the failure worth
        investigating, so the final passing attempt is recorded as ``FLAKY``
        rather than ``PASSED``, which keeps it in every failure view.

        Also stamps ``retry_count`` on every attempt, so "the authoritative
        result for this test in this run" is expressible as
        ``attempt == retry_count`` and the dashboard can avoid counting one
        logical failure three times.
        """
        if not attempts:
            return

        ordered = sorted(attempts, key=lambda a: a.attempt)
        retries = len(ordered) - 1
        for attempt in ordered:
            attempt.retry_count = retries

        final = ordered[-1]
        earlier_failed = any(a.status in _FAILING for a in ordered[:-1])
        if final.status == TestStatus.PASSED and earlier_failed:
            final.status = TestStatus.FLAKY

    @staticmethod
    def parse_timestamp(raw: Any) -> datetime | None:
        """Best-effort timestamp parsing across the formats reporters emit."""
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
        if isinstance(raw, (int, float)):
            # Reporters emit both seconds and milliseconds since epoch. The
            # threshold distinguishes them: 1e11 seconds is the year 5138.
            seconds = raw / 1000 if raw > 1e11 else raw
            try:
                return datetime.fromtimestamp(seconds, tz=UTC)
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(raw, str):
            text = raw.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return None

    @staticmethod
    def to_millis(value: Any, *, unit: str = "ms") -> int:
        """Coerce a duration to non-negative integer milliseconds.

        Frameworks disagree: Playwright and Cypress report milliseconds, PyTest
        and JUnit report fractional seconds. Getting this wrong by 1000x makes
        every duration-versus-baseline comparison meaningless, which silently
        removes one of the agent's better signals.
        """
        if value is None:
            return 0
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0
        if unit == "s":
            number *= 1000
        return max(0, round(number))

    @staticmethod
    def join_output(chunks: Iterable[Any]) -> str | None:
        """Flatten a reporter's stdout/stderr array into one string.

        Playwright emits ``[{"text": "..."}, {"buffer": "..."}]``; others emit
        plain strings. Both collapse to the same text.
        """
        lines: list[str] = []
        for chunk in chunks or []:
            if isinstance(chunk, str):
                lines.append(chunk)
            elif isinstance(chunk, dict):
                text = chunk.get("text") or chunk.get("buffer")
                if isinstance(text, str):
                    lines.append(text)
        joined = "".join(lines).strip()
        return joined or None


# Status vocabularies, unioned across every framework we ingest. Kept as one
# table rather than per-parser mappings because the overlap is near-total and
# duplicating it is how a status ends up handled in three parsers and missed in
# the fourth.
_PASSED_WORDS = frozenset({"passed", "pass", "ok", "success", "successful", "expected"})
_FAILED_WORDS = frozenset({"failed", "fail", "failure", "unexpected", "broken"})
_SKIPPED_WORDS = frozenset(
    {"skipped", "skip", "pending", "disabled", "ignored", "notrun", "xfailed", "xpassed"}
)
_FLAKY_WORDS = frozenset({"flaky", "retried"})
_ERROR_WORDS = frozenset({"error", "errored", "crashed", "interrupted", "timedout", "timeout"})


_REGISTRY: dict[TestFramework, ReportParser] = {}


def register(parser: ReportParser) -> ReportParser:
    """Register a parser for its framework."""
    _REGISTRY[parser.framework] = parser
    return parser


def get_parser(framework: TestFramework) -> ReportParser:
    """Look up the parser for a framework."""
    # Imported here rather than at module top so that registration happens on
    # first use without base.py importing its own subclasses (a cycle).
    from backend.ingest import (  # noqa: F401
        cypress,
        junit,
        playwright,
        pytest_report,
        selenium,
    )

    try:
        return _REGISTRY[framework]
    except KeyError:  # pragma: no cover - unreachable while the enum is closed
        raise ParseError(f"no parser registered for {framework}", framework) from None


def parse_report(
    framework: TestFramework, payload: bytes, metadata: RunMetadata
) -> ParsedReport:
    """Parse an uploaded report for the given framework."""
    if not payload.strip():
        raise ParseError("uploaded file is empty", framework)
    return get_parser(framework).parse(payload, metadata)
