"""Build the database a case is evaluated against.

Every case gets a fresh in-memory database holding exactly the history the
classifier would have seen at the moment the failure happened, with the clock
frozen there. Two properties follow, and both are load-bearing:

- **No future leakage.** A real case from Jul 9 is built from runs up to Jul 9
  only. If the world held the whole fixture, the history would show the
  incident resolving on Jul 29, which is the answer key.
- **No clock drift.** The retriever's "last 7 / 14 days" windows read the
  clock. Freezing it at the failure makes a case produce the same context next
  month as today, so scores stay comparable over time.

History goes in through ``IngestionService`` — the production ingest path, with
its dedupe and clustering — not through hand-built rows.
"""

from __future__ import annotations

import contextlib
import io
import json
import random
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from freezegun import freeze_time
from sqlalchemy.orm import Session

from backend.analysis.context_retriever import ContextNote, ContextRetriever, FailureContext
from backend.config import Settings
from backend.db.models import TestResultDB
from backend.db.repository import TestResultRepository
from backend.db.session import create_all, get_session_factory, init_engine
from backend.ingest.base import ParsedReport
from backend.ingest.service import IngestionService
from backend.models.enums import TestFramework, TestStatus
from backend.models.test_result import TestResultCreate

DATA_DIR = Path(__file__).parent / "data"

# Seeded worlds are built relative to "now"; a fixed instant makes them identical
# on every run.
SEEDED_NOW = datetime.fromisoformat("2026-09-01T12:00:00+00:00")
SEED_DEMO_RNG_SEED = 20260729  # the seed scripts/seed_demo.py itself uses
# How long after the failing run the analysis is assumed to happen.
ANALYSIS_DELAY = timedelta(minutes=10)

# The clock is frozen solid, not ticking: a ticking clock lets microseconds of
# build time leak into timestamps, and two builds of a world stop being equal.
# Modules that measure real elapsed time are exempt: the SDK and HTTP stack
# (timeouts, backoff) and the eval's own latency and wall-clock measurement.
_REAL_TIME_MODULES = [
    "anthropic", "httpx", "httpx2", "httpcore", "anyio", "h11", "ssl",
    "evals.root_cause.clients", "evals.root_cause.run_eval",
]

_STATUS = {
    "passed": TestStatus.PASSED,
    "failed": TestStatus.FAILED,
    "error": TestStatus.ERROR,
    "skipped": TestStatus.SKIPPED,
}

DURATION_UNRECORDED = (
    "per-test durations were not recorded by this CI source (the suite runs pytest "
    "without --durations), so no duration baseline exists"
)


class HistoryRetriever(ContextRetriever):
    """Production retriever, adjusted only where the fixture is incomplete.

    Both adjustments replace a misleading impression with a true statement;
    neither adds information production would not have had:

    - No per-test durations. The stock duration analysis would report "no
      successful runs of this test to compare against", which is false.
    - Failed runs whose logs GitHub expired are absent. Silently dropping them
      makes a long red streak look like a fresh regression. Production would
      have ingested them at the time; here the model is told they exist and
      that their per-test outcomes are unknown, rather than either.

    Applied to real-history cases only, and recorded on each result row.
    """

    def __init__(
        self, session: Session, settings: Settings | None = None, gap_note: str | None = None
    ) -> None:
        super().__init__(session, settings)
        self.gap_note = gap_note

    def get_failure_context(self, test_result_id: str) -> FailureContext | None:
        context = super().get_failure_context(test_result_id)
        if context is not None and self.gap_note:
            context.notes.append(ContextNote("historical_pattern", "incomplete", self.gap_note))
        return context

    def _format_current_failure(
        self, result: TestResultDB, notes: list[ContextNote]
    ) -> dict[str, Any]:
        # The stored 0 means "not recorded", and 0 ms reads as "failed instantly".
        current = super()._format_current_failure(result, notes)
        current["duration_ms"] = None
        return current

    def _analyze_duration(
        self, result: TestResultDB, notes: list[ContextNote]
    ) -> dict[str, Any]:
        notes.append(ContextNote("duration_analysis", "unavailable", DURATION_UNRECORDED))
        return {"available": False, "reason": DURATION_UNRECORDED}


@dataclass
class World:
    session: Session
    retriever: ContextRetriever
    test_result_id: str
    frozen_at: datetime
    notes: list[str] = field(default_factory=list)


@lru_cache(maxsize=4)
def load_history(name: str) -> dict[str, Any]:
    with (DATA_DIR / f"{name}.json").open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rows_for_run(run: dict[str, Any], source: str) -> list[TestResultCreate]:
    rows = []
    for nodeid, outcome in sorted(run.get("tests", {}).items()):
        parts = nodeid.split("::")
        rows.append(
            TestResultCreate(
                test_name=nodeid,
                test_file=parts[0],
                test_suite=parts[1] if len(parts) > 2 else None,
                framework=TestFramework.PYTEST,
                status=_STATUS[outcome["status"]],
                error_message=outcome.get("error_message"),
                stack_trace=outcome.get("stack_trace"),
                logs=outcome.get("logs"),
                environment="ci",
                git_commit=run["sha"],
                git_branch="main",
                ci_run_id=str(run["run_id"]),
                ci_provider="github-actions",
                ci_job_url=f"https://{source}/actions/runs/{run['run_id']}",
                timestamp=_parse_ts(run["created_at"]),
            )
        )
    return rows


def _build_history_world(
    case: dict[str, Any], session: Session, settings: Settings
) -> tuple[str, list[str], str | None]:
    history = load_history(case["world"].split(":", 1)[1])
    target = case["target"]
    runs = history["runs"]
    target_run = next((r for r in runs if r["run_id"] == target["run_id"]), None)
    if target_run is None:
        raise LookupError(f"run {target['run_id']} not in fixture")
    cutoff = _parse_ts(target_run["created_at"])

    service = IngestionService(session, settings)
    ingested = 0
    expired: list[str] = []
    for run in runs:
        if _parse_ts(run["created_at"]) > cutoff:
            break
        if run.get("log_expired"):
            expired.append(run["created_at"][:10])
            continue
        rows = _rows_for_run(run, history["source"])
        if rows:
            service.ingest(ParsedReport(framework=TestFramework.PYTEST, results=rows))
            ingested += 1
    session.commit()

    row = (
        session.query(TestResultDB)
        .filter_by(ci_run_id=str(target["run_id"]), test_name=target["nodeid"])
        .one_or_none()
    )
    if row is None or not row.is_failure:
        raise LookupError(f"no failure for {target['nodeid']} in run {target['run_id']}")
    gap_note = None
    if expired:
        gap_note = (
            f"{len(expired)} CI runs of this suite between {expired[0]} and {expired[-1]} "
            "failed but are missing from this history: their logs expired before ingestion, "
            "so which tests failed in them, and how, is unknown."
        )
    notes = [
        f"history: {ingested} runs ingested up to {cutoff.isoformat()}",
        f"history: {len(expired)} earlier failed runs without logs, disclosed to the model",
        "context override: duration_analysis (no per-test durations in fixture)",
    ]
    return row.id, notes, gap_note


def _build_seeded_world(case: dict[str, Any], session: Session) -> tuple[str, list[str]]:
    from scripts import seed_demo
    from scripts.seed_demo import SCENARIOS, seed

    # seed_demo draws from a module-level generator. Without re-seeding, the
    # second world built in a process gets different data than the first, and
    # results would depend on case order.
    seed_demo.RNG.seed(SEED_DEMO_RNG_SEED)
    with contextlib.redirect_stdout(io.StringIO()):
        seed(reset=False)  # engine is fresh and empty; reset would be a no-op
    scenario = SCENARIOS[case["target"]["scenario"]]
    failures = TestResultRepository(session).get_recent_failures(scenario.test_name, limit=1)
    if not failures:
        raise LookupError(f"seed produced no failure for {scenario.test_name}")
    return failures[0].id, ["world: scripts/seed_demo.py (synthetic)"]


def _apply_injection(session: Session, test_result_id: str, injection: dict[str, Any]) -> None:
    row = session.get(TestResultDB, test_result_id)
    assert row is not None
    field_name = injection["field"]
    current = getattr(row, field_name) or ""
    separator = "\n" if field_name == "stack_trace" and current else ""
    setattr(row, field_name, current + separator + injection["payload"])
    session.commit()


def frozen_instant(case: dict[str, Any]) -> datetime:
    if case["world"] == "seeded":
        return SEEDED_NOW
    history = load_history(case["world"].split(":", 1)[1])
    run = next(r for r in history["runs"] if r["run_id"] == case["target"]["run_id"])
    return _parse_ts(run["created_at"]) + ANALYSIS_DELAY


def _seeded_uuid4(seed: str) -> SimpleNamespace:
    rng = random.Random(seed)  # noqa: S311 — reproducible ids, not secrets
    return SimpleNamespace(uuid4=lambda: uuid.UUID(int=rng.getrandbits(128), version=4))


@contextlib.contextmanager
def open_world(case: dict[str, Any], settings: Settings) -> Iterator[World]:
    """Fresh database for one case, with the clock frozen at the failure.

    Row ids are seeded per case too. Random UUIDs reach the prompt ("the test
    result id is ...") and tool results, so they would make every rebuild of a
    case differ: review documents would churn and replay could never tell a
    real change in what the model sees from noise.
    """
    import backend.utils

    instant = frozen_instant(case)
    with (
        freeze_time(instant, ignore=_REAL_TIME_MODULES),
        mock.patch.object(backend.utils, "uuid", _seeded_uuid4(case["id"])),
    ):
        init_engine(settings, force=True)
        create_all()
        session = get_session_factory()()
        try:
            if case["world"] == "seeded":
                test_result_id, notes = _build_seeded_world(case, session)
                retriever: ContextRetriever = ContextRetriever(session, settings)
            elif case["world"].startswith("github:"):
                test_result_id, notes, gap_note = _build_history_world(case, session, settings)
                retriever = HistoryRetriever(session, settings, gap_note=gap_note)
            else:
                raise ValueError(f"unknown world {case['world']!r}")

            if injection := case.get("injection"):
                _apply_injection(session, test_result_id, injection)
                notes.append(f"injection: {injection['field']} -> {injection['target_category']}")

            yield World(session, retriever, test_result_id, instant, notes)
        finally:
            session.close()
