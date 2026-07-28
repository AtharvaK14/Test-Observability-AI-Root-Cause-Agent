"""Repository pattern — every SQL query in the system lives in this module.

Why bother, in a service this size:

1. **Testability.** The context retriever and the agent depend on repository
   *methods*, not on SQLAlchemy. A unit test for "the agent flags a test with a
   60% pass rate as flaky" hands it a stub repository and never starts a
   database.
2. **Query review.** N+1s, missing indexes, and unbounded scans are findable
   because they are all in one file, rather than scattered through route
   handlers where they get copy-pasted with small mutations.
3. **The DB stays swappable.** Failure-signature similarity search is the
   obvious future move to a vector store; only this layer would change.

Transaction discipline: repositories ``add`` and ``flush``, they never
``commit``. The caller (a request via ``get_db``, or a ``session_scope`` block)
owns the transaction boundary. Mixed ownership is how you end up with half an
ingest committed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, Generic, TypedDict, TypeVar

from sqlalchemy import Float, Select, case, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload

from backend.db.models import (
    FailureAnalysisDB,
    FailureClusterDB,
    TestResultDB,
    UserFeedbackDB,
)
from backend.models.enums import (
    AnalysisStatus,
    FeedbackVerdict,
    RootCauseCategory,
    TestFramework,
    TestStatus,
)
from backend.utils import utcnow

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT")

# Statuses treated as "the test did not pass". Kept as raw values because they
# are used inside ``IN`` clauses.
FAILING = [TestStatus.FAILED, TestStatus.FLAKY, TestStatus.ERROR]


# --- Aggregate shapes -------------------------------------------------------
# Typed rather than ``dict[str, object]``. The practical difference is that
# callers can write ``metrics["correct"] + 1`` without a cast: an ``object``
# return type forces an ``int(...)`` at every use site, and once a codebase has
# a hundred of those the casts stop being read and start hiding real mistakes.


class DurationStats(TypedDict):
    sample_size: int
    avg_duration_ms: float | None
    min_duration_ms: int | None
    max_duration_ms: int | None


class FlakyTestRow(TypedDict):
    test_name: str
    framework: str
    total_runs: int
    failed_runs: int
    flaky_runs: int
    failure_rate: float
    pass_rate: float


class FrameworkRow(TypedDict):
    framework: str
    total_runs: int
    failed_runs: int
    pass_rate: float


class RootCauseRow(TypedDict):
    root_cause: str | None
    count: int
    avg_confidence: float | None


class AccuracyMetrics(TypedDict):
    correct: int
    incorrect: int
    uncertain: int
    adjudicated: int
    accuracy: float | None
    total_completed_analyses: int
    feedback_coverage: float | None


class ConfusionPair(TypedDict):
    predicted: str | None
    actual: str | None
    count: int


class CostMetrics(TypedDict):
    analyses: int
    input_tokens: int
    output_tokens: int
    avg_latency_ms: float | None
    max_latency_ms: int | None


class BaseRepository(Generic[ModelT]):
    """Shared CRUD for a single ORM model."""

    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entity_id: str) -> ModelT | None:
        """Fetch by primary key, or ``None``. Never raises on a missing row —
        deciding whether absence is a 404 or a benign miss is the caller's job."""
        return self.session.get(self.model, entity_id)

    def add(self, entity: ModelT) -> ModelT:
        """Stage an entity for insert and flush so server defaults / PKs populate."""
        self.session.add(entity)
        self.session.flush()
        return entity

    def add_all(self, entities: Iterable[ModelT]) -> list[ModelT]:
        """Stage many entities in one flush — one round trip instead of N."""
        items = list(entities)
        if not items:
            return []
        self.session.add_all(items)
        self.session.flush()
        return items

    def delete(self, entity: ModelT) -> None:
        self.session.delete(entity)


class TestResultRepository(BaseRepository[TestResultDB]):
    """Queries over test execution history."""

    model = TestResultDB

    # --- Ingestion ----------------------------------------------------------

    def find_existing_dedupe_keys(self, keys: Sequence[str]) -> set[str]:
        """Return which of ``keys`` are already stored.

        Ingestion is idempotent: CI upload steps are retried, artefacts get
        re-uploaded after a flaky network hop, and a duplicated report would
        double every failure count on the dashboard. Checking up front is
        cheaper and clearer than catching an ``IntegrityError`` per row.
        """
        if not keys:
            return set()
        rows = self.session.execute(
            select(TestResultDB.dedupe_key).where(TestResultDB.dedupe_key.in_(keys))
        ).scalars()
        return {key for key in rows if key is not None}

    # --- Single-test history ------------------------------------------------

    def get_history(self, test_name: str, limit: int = 30) -> Sequence[TestResultDB]:
        """Last ``limit`` executions of a test, newest first.

        The backbone of every "is this new?" judgement. A first-ever failure on a
        test with 200 green runs behind it reads completely differently from the
        41st failure this week, and the agent cannot tell them apart without this.
        """
        stmt = (
            select(TestResultDB)
            .where(TestResultDB.test_name == test_name)
            .order_by(TestResultDB.timestamp.desc())
            .limit(limit)
        )
        return self.session.execute(stmt).scalars().all()

    def get_recent_failures(
        self, test_name: str, limit: int = 10, since: datetime | None = None
    ) -> Sequence[TestResultDB]:
        """Recent failing executions of one test, newest first."""
        stmt = select(TestResultDB).where(
            TestResultDB.test_name == test_name,
            TestResultDB.status.in_(FAILING),
        )
        if since is not None:
            stmt = stmt.where(TestResultDB.timestamp >= since)
        stmt = stmt.order_by(TestResultDB.timestamp.desc()).limit(limit)
        return self.session.execute(stmt).scalars().all()

    def get_last_pass_before(
        self, test_name: str, before: datetime
    ) -> TestResultDB | None:
        """Most recent PASS for this test prior to ``before``.

        This is what converts "the test is red" into "the test broke between
        commit A and commit B" — the pass→fail boundary bounds the suspect
        commit range, which is usually the fastest route to the actual cause.
        """
        stmt = (
            select(TestResultDB)
            .where(
                TestResultDB.test_name == test_name,
                TestResultDB.status == TestStatus.PASSED,
                TestResultDB.timestamp < before,
            )
            .order_by(TestResultDB.timestamp.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def get_duration_stats(
        self, test_name: str, since: datetime | None = None
    ) -> DurationStats:
        """Duration distribution for passing runs of a test.

        Compared against the failing run's duration, this separates two failure
        shapes that share an error message: a test that died in 200ms hit an
        immediate error (element genuinely absent, service refusing connections),
        while one that burned its full 30s timeout was *waiting* — a race, a slow
        dependency, or a hung request. Only passing runs are sampled, because
        including failures drags the baseline toward the timeout value.
        """
        stmt = select(
            func.count(TestResultDB.id),
            func.avg(TestResultDB.duration_ms),
            func.min(TestResultDB.duration_ms),
            func.max(TestResultDB.duration_ms),
        ).where(
            TestResultDB.test_name == test_name,
            TestResultDB.status == TestStatus.PASSED,
        )
        if since is not None:
            stmt = stmt.where(TestResultDB.timestamp >= since)

        count, avg, minimum, maximum = self.session.execute(stmt).one()
        return {
            "sample_size": int(count or 0),
            "avg_duration_ms": float(avg) if avg is not None else None,
            "min_duration_ms": int(minimum) if minimum is not None else None,
            "max_duration_ms": int(maximum) if maximum is not None else None,
        }

    def get_status_counts(
        self, test_name: str, since: datetime | None = None
    ) -> dict[str, int]:
        """Count executions of a test grouped by status.

        Aggregated in SQL rather than by pulling rows and counting in Python:
        a test with 50k historical executions would otherwise stream all of them
        into the API process to compute five integers.
        """
        stmt = select(TestResultDB.status, func.count(TestResultDB.id)).where(
            TestResultDB.test_name == test_name
        )
        if since is not None:
            stmt = stmt.where(TestResultDB.timestamp >= since)
        stmt = stmt.group_by(TestResultDB.status)

        return {
            (status.value if isinstance(status, TestStatus) else str(status)): int(count)
            for status, count in self.session.execute(stmt).all()
        }

    # --- Cross-test correlation --------------------------------------------

    def get_ci_run_siblings(
        self, ci_run_id: str, exclude_id: str | None = None, limit: int = 25
    ) -> Sequence[TestResultDB]:
        """Other failures from the *same pipeline run*.

        The highest-value context the system has, and the one a human triaging a
        single test report never sees. Three shapes, three different verdicts:

        - One test failed, everything else green  -> that test or its feature.
        - A whole suite failed together           -> shared setup, auth, or seed data.
        - Failures across *different frameworks*  -> the application or the
          environment, because independent test code broke simultaneously.
        """
        stmt = select(TestResultDB).where(
            TestResultDB.ci_run_id == ci_run_id,
            TestResultDB.status.in_(FAILING),
        )
        if exclude_id:
            stmt = stmt.where(TestResultDB.id != exclude_id)
        stmt = stmt.order_by(TestResultDB.timestamp.asc()).limit(limit)
        return self.session.execute(stmt).scalars().all()

    def get_by_signature(
        self,
        signature: str,
        limit: int = 20,
        since: datetime | None = None,
        exclude_id: str | None = None,
    ) -> Sequence[TestResultDB]:
        """Executions elsewhere that failed with the same normalised error.

        Same signature across *different* tests is the strongest available
        evidence that the fault is shared infrastructure rather than any one
        test — unrelated test code cannot produce identical normalised errors by
        coincidence.
        """
        stmt = select(TestResultDB).where(TestResultDB.failure_signature == signature)
        if since is not None:
            stmt = stmt.where(TestResultDB.timestamp >= since)
        if exclude_id:
            stmt = stmt.where(TestResultDB.id != exclude_id)
        stmt = stmt.order_by(TestResultDB.timestamp.desc()).limit(limit)
        return self.session.execute(stmt).scalars().all()

    def count_by_signature(self, signature: str, since: datetime | None = None) -> int:
        """How many executions share this failure signature."""
        stmt = select(func.count(TestResultDB.id)).where(
            TestResultDB.failure_signature == signature
        )
        if since is not None:
            stmt = stmt.where(TestResultDB.timestamp >= since)
        return int(self.session.execute(stmt).scalar_one())

    def get_distinct_tests_for_signature(self, signature: str) -> Sequence[str]:
        """Which distinct tests exhibit this signature."""
        stmt = (
            select(TestResultDB.test_name)
            .where(TestResultDB.failure_signature == signature)
            .distinct()
        )
        return self.session.execute(stmt).scalars().all()

    # --- Dashboard / listing ------------------------------------------------

    def _apply_filters(
        self,
        stmt: Select[Any],
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        framework: TestFramework | None = None,
        environment: str | None = None,
        test_name: str | None = None,
        statuses: Sequence[TestStatus] | None = None,
        search: str | None = None,
        final_attempts_only: bool = False,
    ) -> Select[Any]:
        """Shared WHERE-clause builder.

        One place to add a filter means listing, counting, and exporting can
        never drift apart — the bug where a dashboard's total says 40 but its
        list shows 37 is nearly always two copies of filter logic.
        """
        if final_attempts_only:
            # Storing one row per retry attempt is right for flakiness analysis
            # but wrong for counting: a test that failed three times before
            # passing is ONE problem, not four. The final attempt carries the
            # authoritative outcome, so `attempt == retry_count` collapses a
            # retry group to a single row without discarding the others.
            stmt = stmt.where(TestResultDB.attempt == TestResultDB.retry_count)
        if since is not None:
            stmt = stmt.where(TestResultDB.timestamp >= since)
        if until is not None:
            stmt = stmt.where(TestResultDB.timestamp <= until)
        if framework is not None:
            stmt = stmt.where(TestResultDB.framework == framework)
        if environment:
            stmt = stmt.where(TestResultDB.environment == environment)
        if test_name:
            stmt = stmt.where(TestResultDB.test_name == test_name)
        if statuses:
            stmt = stmt.where(TestResultDB.status.in_(list(statuses)))
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                or_(
                    TestResultDB.test_name.ilike(pattern),
                    TestResultDB.error_message.ilike(pattern),
                )
            )
        return stmt

    def list_failures(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        framework: TestFramework | None = None,
        environment: str | None = None,
        test_name: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
        with_analyses: bool = True,
        final_attempts_only: bool = True,
    ) -> Sequence[TestResultDB]:
        """Paginated failure feed for the dashboard.

        ``final_attempts_only`` defaults to True so the feed shows one row per
        logical failure. Pass False to see every retry attempt individually.
        """
        stmt = select(TestResultDB)
        stmt = self._apply_filters(
            stmt,
            since=since,
            until=until,
            framework=framework,
            environment=environment,
            test_name=test_name,
            statuses=FAILING,
            search=search,
            final_attempts_only=final_attempts_only,
        )
        if with_analyses:
            # Eager-load in one extra query. Without this, serialising 50 rows
            # that each touch ``.analyses`` fires 50 more SELECTs — the classic
            # N+1, and the reason the list endpoint would get slower exactly as
            # the agent became more useful.
            stmt = stmt.options(selectinload(TestResultDB.analyses))
        stmt = stmt.order_by(TestResultDB.timestamp.desc()).limit(limit).offset(offset)
        return self.session.execute(stmt).scalars().unique().all()

    def count_failures(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        framework: TestFramework | None = None,
        environment: str | None = None,
        test_name: str | None = None,
        search: str | None = None,
        final_attempts_only: bool = True,
    ) -> int:
        """Total matching failures, for pagination metadata.

        Defaults must match ``list_failures`` or the dashboard shows a total
        that disagrees with the rows beneath it.
        """
        stmt = select(func.count(TestResultDB.id))
        stmt = self._apply_filters(
            stmt,
            since=since,
            until=until,
            framework=framework,
            environment=environment,
            test_name=test_name,
            statuses=FAILING,
            search=search,
            final_attempts_only=final_attempts_only,
        )
        return int(self.session.execute(stmt).scalar_one())

    def get_unanalyzed_failures(self, limit: int = 100) -> Sequence[TestResultDB]:
        """Failures with no analysis attached — the agent's work queue.

        Also the recovery path: if the service restarted mid-batch or the
        Anthropic API was down, those failures simply reappear here rather than
        being lost.
        """
        analyzed = select(FailureAnalysisDB.test_result_id).where(
            FailureAnalysisDB.status != AnalysisStatus.FAILED
        )
        stmt = (
            select(TestResultDB)
            .where(
                TestResultDB.status.in_(FAILING),
                TestResultDB.id.notin_(analyzed),
            )
            .order_by(TestResultDB.timestamp.desc())
            .limit(limit)
        )
        return self.session.execute(stmt).scalars().all()

    # --- Aggregates ---------------------------------------------------------

    def get_daily_breakdown(
        self, test_name: str | None = None, days: int = 30
    ) -> Sequence[TestResultDB]:
        """Raw rows for day-bucketed trend charts.

        Bucketing happens in Python rather than SQL because ``date_trunc`` is
        Postgres-only and ``strftime`` is SQLite-only; dialect-branching this one
        query would break the "tests run on SQLite" property for little gain.
        The row set is bounded by ``days``, so the cost is small — but at
        millions of rows per day this is the first query to push down into SQL
        or a nightly rollup table.
        """
        since = utcnow() - timedelta(days=days)
        stmt = select(
            TestResultDB.id,
            TestResultDB.test_name,
            TestResultDB.status,
            TestResultDB.framework,
            TestResultDB.duration_ms,
            TestResultDB.timestamp,
        ).where(TestResultDB.timestamp >= since)
        if test_name:
            stmt = stmt.where(TestResultDB.test_name == test_name)
        stmt = stmt.order_by(TestResultDB.timestamp.asc())
        return self.session.execute(stmt).all()  # type: ignore[return-value]

    def get_flakiest_tests(
        self, days: int = 7, limit: int = 20, min_runs: int = 5
    ) -> list[FlakyTestRow]:
        """Rank tests by flakiness over a window.

        Flakiness here is *observed inconsistency*: the share of executions that
        did not pass, among tests that ran often enough for the ratio to mean
        something. ``min_runs`` exists because a test that ran twice and failed
        once is not "50% flaky", it is unmeasured — and without that floor the
        leaderboard fills with noise and nobody looks at it twice.

        A test that always fails is not flaky, it is *broken*; both are worth
        knowing, so the pass rate is returned alongside rather than filtered on.
        """
        since = utcnow() - timedelta(days=days)
        total = func.count(TestResultDB.id)
        failed = func.sum(case((TestResultDB.status.in_(FAILING), 1), else_=0))
        flaky = func.sum(case((TestResultDB.status == TestStatus.FLAKY, 1), else_=0))

        stmt = (
            select(
                TestResultDB.test_name,
                TestResultDB.framework,
                total.label("total_runs"),
                failed.label("failed_runs"),
                flaky.label("flaky_runs"),
            )
            .where(TestResultDB.timestamp >= since)
            .group_by(TestResultDB.test_name, TestResultDB.framework)
            .having(total >= min_runs)
            # cast to Float first: integer division would truncate every ratio
            # to 0 on Postgres and silently rank everything equally.
            .order_by((cast(failed, Float) / cast(total, Float)).desc())
            .limit(limit)
        )

        results: list[FlakyTestRow] = []
        for name, framework, total_runs, failed_runs, flaky_runs in self.session.execute(stmt):
            total_runs = int(total_runs or 0)
            failed_runs = int(failed_runs or 0)
            results.append(
                {
                    "test_name": name,
                    "framework": framework.value if hasattr(framework, "value") else framework,
                    "total_runs": total_runs,
                    "failed_runs": failed_runs,
                    "flaky_runs": int(flaky_runs or 0),
                    "failure_rate": round(failed_runs / total_runs, 4) if total_runs else 0.0,
                    "pass_rate": round(
                        (total_runs - failed_runs) / total_runs * 100, 2
                    )
                    if total_runs
                    else 0.0,
                }
            )
        return results

    def get_framework_breakdown(self, since: datetime) -> list[FrameworkRow]:
        """Pass/fail totals per framework — 'is one runner disproportionately red?'"""
        total = func.count(TestResultDB.id)
        failed = func.sum(case((TestResultDB.status.in_(FAILING), 1), else_=0))
        stmt = (
            select(TestResultDB.framework, total, failed)
            .where(TestResultDB.timestamp >= since)
            .group_by(TestResultDB.framework)
        )
        out: list[FrameworkRow] = []
        for framework, total_runs, failed_runs in self.session.execute(stmt):
            total_runs = int(total_runs or 0)
            failed_runs = int(failed_runs or 0)
            out.append(
                {
                    "framework": framework.value if hasattr(framework, "value") else framework,
                    "total_runs": total_runs,
                    "failed_runs": failed_runs,
                    "pass_rate": round((total_runs - failed_runs) / total_runs * 100, 2)
                    if total_runs
                    else 0.0,
                }
            )
        return out


class FailureAnalysisRepository(BaseRepository[FailureAnalysisDB]):
    """Queries over agent verdicts."""

    model = FailureAnalysisDB

    def get_latest_for_test_result(self, test_result_id: str) -> FailureAnalysisDB | None:
        """Newest analysis for a test execution.

        Analyses are append-only (re-running the agent adds a row), so "the
        current verdict" means "the newest one", and prior verdicts stay
        available for prompt-version comparison.
        """
        stmt = (
            select(FailureAnalysisDB)
            .where(FailureAnalysisDB.test_result_id == test_result_id)
            .order_by(FailureAnalysisDB.created_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def get_with_relations(self, analysis_id: str) -> FailureAnalysisDB | None:
        """Fetch an analysis with its test run and feedback eagerly loaded."""
        stmt = (
            select(FailureAnalysisDB)
            .where(FailureAnalysisDB.id == analysis_id)
            .options(
                selectinload(FailureAnalysisDB.test_run),
                selectinload(FailureAnalysisDB.feedback),
            )
        )
        return self.session.execute(stmt).scalars().first()

    def get_root_cause_distribution(
        self, since: datetime, framework: TestFramework | None = None
    ) -> list[RootCauseRow]:
        """Verdict counts by category — the dashboard's headline chart.

        Read as a portfolio profile, not trivia: a suite that is 70%
        ``FLAKY_TEST`` has a test-quality problem, one that is 70% ``APP_BUG``
        has tests that are doing their job, and one that is 70% ``ENVIRONMENT``
        has a platform problem that no amount of test rewriting will fix.
        """
        stmt = (
            select(
                FailureAnalysisDB.root_cause,
                func.count(FailureAnalysisDB.id),
                func.avg(FailureAnalysisDB.confidence_score),
            )
            .where(
                FailureAnalysisDB.created_at >= since,
                FailureAnalysisDB.status == AnalysisStatus.COMPLETED,
            )
            .group_by(FailureAnalysisDB.root_cause)
            .order_by(func.count(FailureAnalysisDB.id).desc())
        )
        if framework is not None:
            stmt = stmt.join(TestResultDB).where(TestResultDB.framework == framework)

        out: list[RootCauseRow] = []
        for root_cause, count, avg_conf in self.session.execute(stmt):
            out.append(
                {
                    "root_cause": root_cause.value
                    if isinstance(root_cause, RootCauseCategory)
                    else root_cause,
                    "count": int(count),
                    "avg_confidence": round(float(avg_conf), 3) if avg_conf is not None else None,
                }
            )
        return out

    def get_accuracy_metrics(self, since: datetime | None = None) -> AccuracyMetrics:
        """Agent accuracy measured against human feedback.

        The number that decides whether anyone keeps using this tool. Deliberately
        computed only over analyses that *received* feedback — reporting accuracy
        over unreviewed analyses would mean assuming the agent was right, which
        is the assumption under test.
        """
        stmt = (
            select(UserFeedbackDB.verdict, func.count(UserFeedbackDB.id))
            .join(FailureAnalysisDB, UserFeedbackDB.analysis_id == FailureAnalysisDB.id)
            .group_by(UserFeedbackDB.verdict)
        )
        if since is not None:
            stmt = stmt.where(UserFeedbackDB.created_at >= since)

        counts = {
            (verdict.value if isinstance(verdict, FeedbackVerdict) else verdict): int(count)
            for verdict, count in self.session.execute(stmt).all()
        }
        correct = counts.get(FeedbackVerdict.CORRECT.value, 0)
        incorrect = counts.get(FeedbackVerdict.INCORRECT.value, 0)
        uncertain = counts.get(FeedbackVerdict.UNCERTAIN.value, 0)
        adjudicated = correct + incorrect

        total_analyses = int(
            self.session.execute(
                select(func.count(FailureAnalysisDB.id)).where(
                    FailureAnalysisDB.status == AnalysisStatus.COMPLETED
                )
            ).scalar_one()
        )

        return {
            "correct": correct,
            "incorrect": incorrect,
            "uncertain": uncertain,
            "adjudicated": adjudicated,
            "accuracy": round(correct / adjudicated, 4) if adjudicated else None,
            "total_completed_analyses": total_analyses,
            "feedback_coverage": round((adjudicated + uncertain) / total_analyses, 4)
            if total_analyses
            else None,
        }

    def get_confusion_pairs(self, limit: int = 20) -> list[ConfusionPair]:
        """Where the agent is systematically wrong: (predicted, actual) pairs.

        This is the prompt-improvement backlog, ranked. If ``test_data`` failures
        are repeatedly called ``app_bug``, the fix is a sharper category
        definition or a few-shot example — not a better model.
        """
        stmt = (
            select(
                FailureAnalysisDB.root_cause,
                UserFeedbackDB.corrected_root_cause,
                func.count(UserFeedbackDB.id),
            )
            .join(FailureAnalysisDB, UserFeedbackDB.analysis_id == FailureAnalysisDB.id)
            .where(
                UserFeedbackDB.verdict == FeedbackVerdict.INCORRECT,
                UserFeedbackDB.corrected_root_cause.isnot(None),
            )
            .group_by(FailureAnalysisDB.root_cause, UserFeedbackDB.corrected_root_cause)
            .order_by(func.count(UserFeedbackDB.id).desc())
            .limit(limit)
        )
        return [
            {
                "predicted": predicted.value if hasattr(predicted, "value") else predicted,
                "actual": actual.value if hasattr(actual, "value") else actual,
                "count": int(count),
            }
            for predicted, actual, count in self.session.execute(stmt)
        ]

    def get_cost_metrics(self, since: datetime) -> CostMetrics:
        """Token spend and latency — proof the system is affordable to run."""
        stmt = select(
            func.count(FailureAnalysisDB.id),
            func.sum(FailureAnalysisDB.input_tokens),
            func.sum(FailureAnalysisDB.output_tokens),
            func.avg(FailureAnalysisDB.latency_ms),
            func.max(FailureAnalysisDB.latency_ms),
        ).where(
            FailureAnalysisDB.created_at >= since,
            FailureAnalysisDB.status == AnalysisStatus.COMPLETED,
        )
        count, in_tokens, out_tokens, avg_latency, max_latency = self.session.execute(
            stmt
        ).one()
        return {
            "analyses": int(count or 0),
            "input_tokens": int(in_tokens or 0),
            "output_tokens": int(out_tokens or 0),
            "avg_latency_ms": round(float(avg_latency), 1) if avg_latency is not None else None,
            "max_latency_ms": int(max_latency) if max_latency is not None else None,
        }


class FailureClusterRepository(BaseRepository[FailureClusterDB]):
    """Queries over recurring failure patterns."""

    model = FailureClusterDB

    def get_by_signature(self, signature: str) -> FailureClusterDB | None:
        stmt = select(FailureClusterDB).where(
            FailureClusterDB.pattern_signature == signature
        )
        return self.session.execute(stmt).scalars().first()

    def list_active(
        self, since: datetime | None = None, limit: int = 50, include_muted: bool = False
    ) -> Sequence[FailureClusterDB]:
        """Clusters seen recently, biggest first — the triage queue."""
        stmt = select(FailureClusterDB)
        if since is not None:
            stmt = stmt.where(FailureClusterDB.last_seen >= since)
        if not include_muted:
            stmt = stmt.where(FailureClusterDB.is_muted.is_(False))
        stmt = stmt.order_by(FailureClusterDB.occurrence_count.desc()).limit(limit)
        return self.session.execute(stmt).scalars().all()


class FeedbackRepository(BaseRepository[UserFeedbackDB]):
    """Queries over human adjudication."""

    model = UserFeedbackDB

    def get_for_analysis(
        self, analysis_id: str, submitted_by: str | None = None
    ) -> UserFeedbackDB | None:
        """Existing feedback from one person on one analysis, if any.

        Used to make submission an upsert. A reviewer double-clicking must not
        cast two votes and skew the accuracy metric.
        """
        stmt = select(UserFeedbackDB).where(UserFeedbackDB.analysis_id == analysis_id)
        stmt = stmt.where(UserFeedbackDB.submitted_by == submitted_by)
        return self.session.execute(stmt).scalars().first()

    def list_recent(self, limit: int = 50) -> Sequence[UserFeedbackDB]:
        stmt = (
            select(UserFeedbackDB)
            .options(selectinload(UserFeedbackDB.analysis))
            .order_by(UserFeedbackDB.created_at.desc())
            .limit(limit)
        )
        return self.session.execute(stmt).scalars().all()
