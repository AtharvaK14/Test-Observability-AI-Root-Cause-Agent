"""Read endpoints for the dashboard, plus the feedback loop.

Two things distinguish these from a generic CRUD API:

**They answer questions, not return tables.** ``/tests/{name}/trends`` does not
just hand back a series — it computes a flakiness score and a verdict, because
"is this test getting worse?" is what someone actually wants and it is not
visible by eye in a red/green grid.

**Feedback is an upsert, not an insert.** A reviewer who clicks twice must not
cast two votes; the accuracy metric is only meaningful if each human adjudicates
each analysis once.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from itertools import pairwise
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Path, Query
from fastapi import status as http_status
from redis import RedisError
from sqlalchemy import func, select

from backend.analysis import dispatch
from backend.api.deps import SessionDep, SettingsDep
from backend.db.models import FailureAnalysisDB, TestResultDB, UserFeedbackDB
from backend.db.repository import (
    FAILING,
    FailureAnalysisRepository,
    FailureClusterRepository,
    FeedbackRepository,
    TestResultRepository,
)
from backend.models.analysis import (
    AgentMetrics,
    AnalysisRequest,
    ClusterRead,
    ConfusionPair,
    DashboardSummary,
    FailureAnalysisRead,
    FailureWithAnalysis,
    FeedbackCreate,
    FeedbackRead,
    FlakyTestEntry,
    FrameworkBreakdown,
    PaginatedFailures,
    RootCauseSlice,
    TestTrends,
    TrendPoint,
)
from backend.models.enums import (
    AnalysisStatus,
    RootCauseCategory,
    TestFramework,
    TestStatus,
)
from backend.models.test_result import TestResultRead, TestResultSummary
from backend.utils import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

HoursBack = Annotated[int, Query(ge=1, le=24 * 90, description="Hours of history to include.")]


# --- Failure feed -----------------------------------------------------------


@router.get("/failures", response_model=PaginatedFailures, summary="Recent failures")
def list_failures(
    session: SessionDep,
    hours: HoursBack = 24,
    framework: Annotated[TestFramework | None, Query()] = None,
    environment: Annotated[str | None, Query()] = None,
    test_name: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query(description="Substring of test name or error.")] = None,
    root_cause: Annotated[str | None, Query(description="Filter by agent verdict.")] = None,
    needs_review: Annotated[bool | None, Query()] = None,
    include_retry_attempts: Annotated[
        bool,
        Query(
            description=(
                "Show every retry attempt as its own row. Off by default so a "
                "test that failed twice then passed counts as one failure."
            )
        ),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedFailures:
    """The dashboard's main feed: recent failures with their verdicts."""
    repo = TestResultRepository(session)
    since = utcnow() - timedelta(hours=hours)
    filters: dict[str, Any] = {
        "since": since,
        "framework": framework,
        "environment": environment,
        "test_name": test_name,
        "search": search,
        "final_attempts_only": not include_retry_attempts,
    }

    rows = repo.list_failures(**filters, limit=limit, offset=offset)
    total = repo.count_failures(**filters)

    items: list[FailureWithAnalysis] = []
    for row in rows:
        # `analyses` is eager-loaded and ordered newest-first by the relationship,
        # so this is a list access, not an N+1 query.
        latest = row.analyses[0] if row.analyses else None

        # Verdict filters are applied here rather than in SQL because filtering
        # on a joined analysis in the same query silently drops failures that
        # have no analysis yet — and "not yet analysed" is a state the dashboard
        # must be able to show.
        if root_cause and (latest is None or latest.root_cause != root_cause):
            continue
        if needs_review is not None and (
            latest is None or latest.requires_human_review != needs_review
        ):
            continue

        items.append(
            FailureWithAnalysis(
                test=TestResultSummary.model_validate(row),
                analysis=FailureAnalysisRead.model_validate(latest) if latest else None,
            )
        )

    return PaginatedFailures(
        items=items,
        total=total if not (root_cause or needs_review is not None) else len(items),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/failures/{test_result_id}",
    summary="Full detail for one failure",
    response_model=dict,
)
def get_failure_detail(
    session: SessionDep,
    test_result_id: Annotated[str, Path()],
) -> dict[str, Any]:
    """Everything about one failure: the execution, every verdict, the cluster.

    Returns *all* analyses rather than only the newest. Analyses are append-only,
    so this is where you see that prompt v2 said `flaky_test` and prompt v3 said
    `test_data` for the same failure — which is the whole point of keeping them.
    """
    repo = TestResultRepository(session)
    result = repo.get(test_result_id)
    if result is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"test result {test_result_id!r} not found",
        )

    cluster = None
    if result.failure_signature:
        found = FailureClusterRepository(session).get_by_signature(result.failure_signature)
        cluster = ClusterRead.model_validate(found) if found else None

    return {
        "test": TestResultRead.model_validate(result).model_dump(),
        "analyses": [
            FailureAnalysisRead.model_validate(a).model_dump() for a in result.analyses
        ],
        "cluster": cluster.model_dump() if cluster else None,
        "sibling_failures": [
            {
                "id": s.id,
                "test_name": s.test_name,
                "framework": s.framework.value,
                "error_message": (s.error_message or "")[:200] or None,
                "same_signature": s.failure_signature == result.failure_signature,
            }
            for s in repo.get_ci_run_siblings(result.ci_run_id, exclude_id=result.id, limit=20)
        ],
    }


# --- Aggregates -------------------------------------------------------------


@router.get("/summary", response_model=DashboardSummary, summary="Dashboard counters")
def get_summary(
    session: SessionDep,
    hours: HoursBack = 24,
    framework: Annotated[TestFramework | None, Query()] = None,
) -> DashboardSummary:
    """Top-of-dashboard numbers for a time window."""
    since = utcnow() - timedelta(hours=hours)
    runs = TestResultRepository(session)
    analyses = FailureAnalysisRepository(session)

    total_runs = int(
        session.execute(
            select(func.count(TestResultDB.id)).where(TestResultDB.timestamp >= since)
        ).scalar_one()
    )
    total_failures = runs.count_failures(since=since, framework=framework)
    unique_failing = int(
        session.execute(
            select(func.count(func.distinct(TestResultDB.test_name))).where(
                TestResultDB.timestamp >= since, TestResultDB.status.in_(FAILING)
            )
        ).scalar_one()
    )

    status_counts: dict[AnalysisStatus, int] = dict(
        session.execute(
            select(FailureAnalysisDB.status, func.count(FailureAnalysisDB.id))
            .where(FailureAnalysisDB.created_at >= since)
            .group_by(FailureAnalysisDB.status)
        ).all()  # type: ignore[arg-type]
    )
    completed = status_counts.get(AnalysisStatus.COMPLETED, 0)
    pending = status_counts.get(AnalysisStatus.PENDING, 0)

    needs_review = int(
        session.execute(
            select(func.count(FailureAnalysisDB.id)).where(
                FailureAnalysisDB.created_at >= since,
                FailureAnalysisDB.requires_human_review.is_(True),
            )
        ).scalar_one()
    )

    distribution = analyses.get_root_cause_distribution(since, framework=framework)
    total_classified = sum(d["count"] for d in distribution) or 1
    slices = [
        RootCauseSlice(
            root_cause=RootCauseCategory(d["root_cause"]),
            count=d["count"],
            avg_confidence=d["avg_confidence"],
            percentage=round(d["count"] / total_classified * 100, 1),
        )
        for d in distribution
        if d["root_cause"]
    ]

    return DashboardSummary(
        window_hours=hours,
        total_runs=total_runs,
        total_failures=total_failures,
        unique_failing_tests=unique_failing,
        analyzed_failures=completed,
        pending_analyses=pending,
        needs_review=needs_review,
        root_cause_distribution=slices,
        framework_breakdown=[
            FrameworkBreakdown(**row) for row in runs.get_framework_breakdown(since)
        ],
        top_clusters=[
            ClusterRead.model_validate(c)
            for c in FailureClusterRepository(session).list_active(since=since, limit=5)
        ],
    )


@router.get(
    "/tests/{test_name:path}/trends",
    response_model=TestTrends,
    summary="Trend for one test",
)
def get_test_trends(
    session: SessionDep,
    test_name: Annotated[str, Path(description="Fully-qualified test name.")],
    days: Annotated[int, Query(ge=1, le=180)] = 30,
) -> TestTrends:
    """Daily pass rate for one test, plus a derived health verdict.

    The verdict is the point. A chart shows what happened; ``verdict`` says
    whether to care — and the distinction it draws is one a red/green grid
    cannot express:

    - **broken** — fails essentially every run. Not flaky, just red. Somebody
      needs to fix it or delete it.
    - **flaky** — alternates. Non-deterministic, and the most expensive kind of
      failure because it erodes trust in every other result.
    - **degrading** — was reliable, is drifting worse. The one worth catching
      early, and the one nobody notices day to day.
    - **healthy** — behaving.
    """
    repo = TestResultRepository(session)
    rows = repo.get_daily_breakdown(test_name=test_name, days=days)
    if not rows:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"no runs recorded for test {test_name!r} in the last {days} days",
        )

    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"passed": 0, "failed": 0, "flaky": 0, "total": 0, "duration": 0}
    )
    framework: TestFramework | None = None
    ordered_statuses: list[TestStatus] = []

    for row in rows:
        framework = row.framework
        day = row.timestamp.date().isoformat()
        bucket = buckets[day]
        bucket["total"] += 1
        bucket["duration"] += row.duration_ms or 0
        if row.status == TestStatus.PASSED:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1
            if row.status == TestStatus.FLAKY:
                bucket["flaky"] += 1
        ordered_statuses.append(row.status)

    points = [
        TrendPoint(
            date=day,
            total=b["total"],
            passed=b["passed"],
            failed=b["failed"],
            flaky=b["flaky"],
            pass_rate=round(b["passed"] / b["total"] * 100, 1) if b["total"] else 0.0,
            avg_duration_ms=round(b["duration"] / b["total"], 1) if b["total"] else None,
        )
        for day, b in sorted(buckets.items())
    ]

    total_runs = len(ordered_statuses)
    passed = sum(1 for s in ordered_statuses if s == TestStatus.PASSED)
    pass_rate = round(passed / total_runs * 100, 1) if total_runs else 0.0

    # Flakiness as instability, not failure rate: the share of consecutive run
    # pairs whose outcome changed. A test that fails every single time scores
    # 0.0 here — it is broken, not flaky, and calling it flaky sends people
    # hunting for a race condition that does not exist.
    booleans = [s == TestStatus.PASSED for s in ordered_statuses]
    transitions = sum(1 for a, b in pairwise(booleans) if a != b)
    flakiness = round(transitions / (len(booleans) - 1), 3) if len(booleans) > 1 else 0.0

    direction, verdict = _classify_trend(points, pass_rate, flakiness)

    return TestTrends(
        test_name=test_name,
        framework=framework,
        window_days=days,
        total_runs=total_runs,
        pass_rate=pass_rate,
        flakiness_score=flakiness,
        trend_direction=direction,
        verdict=verdict,
        points=points,
    )


def _classify_trend(
    points: list[TrendPoint], pass_rate: float, flakiness: float
) -> tuple[str, str]:
    """Derive a direction and a verdict from a trend series."""
    direction = "stable"
    if len(points) >= 4:
        midpoint = len(points) // 2
        early = [p for p in points[:midpoint] if p.total]
        late = [p for p in points[midpoint:] if p.total]
        if early and late:
            early_rate = sum(p.pass_rate for p in early) / len(early)
            late_rate = sum(p.pass_rate for p in late) / len(late)
            delta = late_rate - early_rate
            # A 10-point swing: below that, normal week-to-week variation on a
            # test that runs a handful of times a day produces a "trend" on
            # nearly every test, and a signal that fires constantly is noise.
            if delta <= -10:
                direction = "degrading"
            elif delta >= 10:
                direction = "improving"

    if pass_rate <= 10:
        verdict = "broken"
    elif flakiness >= 0.3:
        verdict = "flaky"
    elif direction == "degrading":
        verdict = "degrading"
    elif pass_rate >= 95:
        verdict = "healthy"
    else:
        verdict = "unreliable"
    return direction, verdict


@router.get("/flaky", response_model=list[FlakyTestEntry], summary="Flakiness leaderboard")
def get_flaky_tests(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    min_runs: Annotated[
        int,
        Query(ge=1, description="Ignore tests with fewer runs — small samples are noise."),
    ] = 5,
) -> list[FlakyTestEntry]:
    """Tests ranked by failure rate over a window.

    ``min_runs`` matters more than it looks: without a floor, a test that ran
    twice and failed once tops the board at "50%", and a leaderboard full of
    unmeasured tests is one nobody checks twice.
    """
    entries = TestResultRepository(session).get_flakiest_tests(
        days=days, limit=limit, min_runs=min_runs
    )
    return [FlakyTestEntry(**entry) for entry in entries]


@router.get("/clusters", response_model=list[ClusterRead], summary="Recurring failure patterns")
def list_clusters(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    include_muted: Annotated[bool, Query()] = False,
) -> list[ClusterRead]:
    """Failure patterns seen recently, biggest first — the real triage queue.

    This is the endpoint that turns "487 tests failed overnight" into "you have
    four problems", which is the difference between a queue someone works and a
    queue everyone ignores.
    """
    since = utcnow() - timedelta(days=days)
    clusters = FailureClusterRepository(session).list_active(
        since=since, limit=limit, include_muted=include_muted
    )
    return [ClusterRead.model_validate(c) for c in clusters]


@router.post("/clusters/{cluster_id}/mute", response_model=ClusterRead, summary="Mute a cluster")
def mute_cluster(
    session: SessionDep,
    cluster_id: Annotated[str, Path()],
    muted: Annotated[bool, Query(description="True to mute, false to unmute.")] = True,
) -> ClusterRead:
    """Acknowledge a known issue so the agent stops re-analysing it.

    A cost control with teeth: an unmuted known issue that fires 200 times a day
    costs 200 API calls a day to re-derive a verdict a human already accepted.
    """
    repo = FailureClusterRepository(session)
    cluster = repo.get(cluster_id)
    if cluster is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"cluster {cluster_id!r} not found",
        )
    cluster.is_muted = muted
    session.flush()
    return ClusterRead.model_validate(cluster)


# --- Agent operations -------------------------------------------------------


@router.post("/analyze", summary="Trigger analysis for a failure", status_code=202)
def request_analysis(
    session: SessionDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    payload: AnalysisRequest,
) -> dict[str, Any]:
    """Queue an on-demand analysis.

    Returns 202 immediately rather than blocking: an agent run takes tens of
    seconds, and a UI that hangs that long on a button click reads as broken.
    """
    repo = TestResultRepository(session)
    result = repo.get(payload.test_result_id)
    if result is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"test result {payload.test_result_id!r} not found",
        )
    if not result.is_failure:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"test result has status {result.status.value}; only failures are analysed",
        )
    if not settings.agent_enabled:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent is disabled (AGENT_ENABLED=false)",
        )

    existing = FailureAnalysisRepository(session).get_latest_for_test_result(result.id)
    if existing and existing.status == AnalysisStatus.COMPLETED and not payload.force:
        return {
            "status": "already_analyzed",
            "analysis_id": existing.id,
            "hint": "pass force=true to re-analyse; this appends a new row rather than replacing",
        }

    job_ids = dispatch.dispatch_analyses([result.id], background, settings)
    response: dict[str, Any] = {"status": "queued", "test_result_id": result.id}
    if job_ids:
        response["job_id"] = job_ids[0]
        response["poll"] = f"/api/analysis/jobs/{job_ids[0]}"
    return response


@router.get("/jobs/{job_id}", summary="Status of a queued analysis job")
def get_analysis_job(
    settings: SettingsDep,
    job_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
) -> dict[str, Any]:
    """Poll a job from the Redis queue: queued → running → done | failed.

    "done" means the worker finished; the verdict itself is in the failure's
    analysis, as for any other analysis. Records expire after 24 hours.
    """
    if not settings.redis_url:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="job tracking needs the Redis queue (REDIS_URL); analysis runs in-process",
        )
    try:
        job = dispatch.get_job(dispatch.get_redis(settings.redis_url), job_id)
    except RedisError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {type(exc).__name__}",
        ) from exc
    if job is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"job {job_id!r} not found (unknown, or expired after 24h)",
        )
    return {"job_id": job_id, **job}


@router.get("/queue", summary="Failures awaiting analysis")
def get_analysis_queue(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Failures with no analysis yet.

    Also the recovery path. FastAPI background tasks die with the process, so a
    restart mid-batch strands pending analyses — they reappear here rather than
    vanishing, which is the difference between a recoverable gap and a silent one.
    """
    pending = TestResultRepository(session).get_unanalyzed_failures(limit=limit)
    return {
        "count": len(pending),
        "items": [
            {
                "test_result_id": r.id,
                "test_name": r.test_name,
                "framework": r.framework.value,
                "error_message": (r.error_message or "")[:200] or None,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            }
            for r in pending
        ],
    }


@router.get("/metrics", response_model=AgentMetrics, summary="Agent accuracy and cost")
def get_agent_metrics(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> AgentMetrics:
    """Everything needed to defend, or refute, an accuracy claim.

    Accuracy is computed only over analyses that received human feedback.
    Averaging in unreviewed analyses would mean assuming the agent was right
    about them, which is precisely the thing under test. ``feedback_coverage``
    is reported alongside so a 95% accuracy over four reviewed analyses is
    visibly what it is.
    """
    since = utcnow() - timedelta(days=days)
    repo = FailureAnalysisRepository(session)
    accuracy = repo.get_accuracy_metrics(since=since)
    cost = repo.get_cost_metrics(since)

    return AgentMetrics(
        window_days=days,
        total_completed_analyses=accuracy["total_completed_analyses"],
        accuracy=accuracy["accuracy"],
        correct=accuracy["correct"],
        incorrect=accuracy["incorrect"],
        uncertain=accuracy["uncertain"],
        adjudicated=accuracy["adjudicated"],
        feedback_coverage=accuracy["feedback_coverage"],
        avg_latency_ms=cost["avg_latency_ms"],
        max_latency_ms=cost["max_latency_ms"],
        input_tokens=cost["input_tokens"],
        output_tokens=cost["output_tokens"],
        confusion_pairs=[
            ConfusionPair(
                predicted=RootCauseCategory(pair["predicted"]) if pair["predicted"] else None,
                actual=RootCauseCategory(pair["actual"]) if pair["actual"] else None,
                count=pair["count"],
            )
            for pair in repo.get_confusion_pairs()
        ],
    )


# --- Feedback loop ----------------------------------------------------------


@router.post(
    "/analyses/{analysis_id}/feedback",
    response_model=FeedbackRead,
    summary="Adjudicate an analysis",
)
def submit_feedback(
    session: SessionDep,
    analysis_id: Annotated[str, Path()],
    payload: FeedbackCreate,
) -> FeedbackRead:
    """Record a human verdict on an agent verdict.

    An upsert keyed on (analysis, reviewer): a double-click must not become two
    votes, or the accuracy metric silently skews toward whoever clicks hardest.

    ``corrected_root_cause`` is what makes this a learning loop rather than a
    complaints box. "Wrong" teaches nothing; "you said flaky_test, it was
    test_data" is a labelled example, and the aggregate of those is the
    confusion matrix on ``/metrics`` — which is the prompt-improvement backlog,
    ranked by how often each confusion actually happens.
    """
    analyses = FailureAnalysisRepository(session)
    analysis = analyses.get(analysis_id)
    if analysis is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"analysis {analysis_id!r} not found",
        )

    feedback_repo = FeedbackRepository(session)
    existing = feedback_repo.get_for_analysis(analysis_id, submitted_by=payload.submitted_by)

    if existing is not None:
        existing.verdict = payload.verdict
        existing.corrected_root_cause = payload.corrected_root_cause
        existing.feedback_text = payload.feedback_text
        session.flush()
        record = existing
    else:
        record = feedback_repo.add(
            UserFeedbackDB(
                analysis_id=analysis_id,
                verdict=payload.verdict,
                corrected_root_cause=payload.corrected_root_cause,
                feedback_text=payload.feedback_text,
                submitted_by=payload.submitted_by,
            )
        )

    # A human has now looked at it, whatever they concluded — so it should leave
    # the review queue rather than sitting there forever.
    analysis.requires_human_review = False
    session.flush()

    logger.info(
        "feedback recorded",
        extra={
            "analysis_id": analysis_id,
            "verdict": payload.verdict.value,
            "predicted": analysis.root_cause.value if analysis.root_cause else None,
            "corrected": payload.corrected_root_cause.value
            if payload.corrected_root_cause
            else None,
        },
    )
    return FeedbackRead.model_validate(record)


@router.get("/feedback", response_model=list[FeedbackRead], summary="Recent feedback")
def list_feedback(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[FeedbackRead]:
    """Recent human adjudications, newest first."""
    return [
        FeedbackRead.model_validate(f) for f in FeedbackRepository(session).list_recent(limit)
    ]
