"""SQLAlchemy ORM models — the durable record of test execution and analysis.

Schema design notes (the SDET reasoning behind each table):

``test_runs``
    One row per *test execution attempt*, not per test. A test retried three
    times produces three rows sharing a ``ci_run_id`` with ``attempt`` 0/1/2.
    Collapsing retries into one row destroys the single most valuable flakiness
    signal there is — "failed, then passed, unchanged code".

``failure_analyses``
    One row per agent verdict on a test run. Modelled one-to-many rather than
    one-to-one on purpose: re-running the agent after a prompt change must not
    overwrite history, otherwise you can never answer "did prompt v3 classify
    better than v2 on the same failures?".

``failure_clusters``
    Groups executions whose normalised error text hashes identically. Turns
    "487 failures overnight" into "3 problems", which is the difference between
    a triage queue a human can work and one they ignore.

``feedback``
    Human adjudication. This is the ground truth that makes any accuracy claim
    about the agent falsifiable, and the training signal for prompt iteration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, JSONColumn, UTCDateTime
from backend.models.enums import (
    AnalysisStatus,
    FeedbackVerdict,
    RootCauseCategory,
    TestFramework,
    TestStatus,
)
from backend.utils import new_id, utcnow


def _enum_column(enum_cls: type, length: int = 32) -> SAEnum:
    """Build a portable enum column.

    ``native_enum=False`` renders VARCHAR + CHECK rather than a Postgres ENUM
    type. Native enums require ``ALTER TYPE ... ADD VALUE`` to extend, which
    cannot run inside a transaction on older Postgres and cannot be reversed at
    all — a bad trade for a taxonomy we fully expect to grow.

    ``create_constraint=True`` is not the default and has to be asked for. It is
    worth asking for: application-level validation does nothing about a backfill
    script or a manual ``UPDATE`` writing ``'APP_BUG'`` instead of ``'app_bug'``,
    and that mistake does not raise anywhere — it just quietly drops rows out of
    every dashboard query that filters on the category. The cost is that adding
    a taxonomy member needs a migration to alter the constraint.

    ``values_callable`` stores the enum *value* (``"playwright"``) rather than
    the member *name* (``"PLAYWRIGHT"``), so raw SQL and the JSON API agree.

    The constraint is named after the enum class, so no single table may use the
    same enum for two columns without an explicit name.
    """
    snake = "".join(
        f"_{ch.lower()}" if ch.isupper() and i else ch.lower()
        for i, ch in enumerate(enum_cls.__name__)
    )
    return SAEnum(
        enum_cls,
        name=snake,
        native_enum=False,
        create_constraint=True,
        length=length,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class TestResultDB(Base):
    """A single test execution attempt, normalised across all frameworks."""

    __tablename__ = "test_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    # --- Identity -----------------------------------------------------------
    test_name: Mapped[str] = mapped_column(String(512), index=True)
    """Fully-qualified name. Normalised per framework so the *same* logical test
    keeps a stable identity across runs — that stability is what makes history
    queries meaningful. Parameterised cases keep their parameters, because
    ``test_login[admin]`` and ``test_login[guest]`` fail for different reasons."""

    test_suite: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    """Describe block / class / module. Lets us spot 'the whole checkout suite
    went red', which points at the app or environment rather than at one test."""

    test_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """Source path — the handle a human needs to actually go fix the thing."""

    framework: Mapped[TestFramework] = mapped_column(_enum_column(TestFramework), index=True)

    # --- Outcome ------------------------------------------------------------
    status: Mapped[TestStatus] = mapped_column(_enum_column(TestStatus), index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    """0-based retry index within a CI run. attempt>0 that passes ⇒ flaky."""

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    """Total attempts the runner made for this test in this CI run."""

    # --- Failure detail -----------------------------------------------------
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    """Exception class or error family (``TimeoutError``, ``AssertionError``,
    ``ElementClickInterceptedException``). Indexed because it is the cheapest
    high-signal grouping key available before any LLM is involved — timeouts and
    assertion failures have almost disjoint root-cause distributions."""

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    stack_trace: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Captured stdout/stderr/console output. For browser frameworks this is
    where the actual cause usually hides: a 500 from an XHR, a CSP violation, an
    unhandled promise rejection — none of which appear in the assertion text."""

    failure_signature: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    """Hash of the *normalised* error (ids, timestamps, ports, hex, paths
    stripped). Two runs with the same signature are the same problem. Computed
    at ingest time, deterministically, with no LLM in the loop — clustering must
    keep working when the Anthropic API is down."""

    # --- Artefacts ----------------------------------------------------------
    screenshot_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    video_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    trace_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    """Playwright trace / Cypress video / Selenium HAR. Stored as references,
    never as blobs — the database is for querying, object storage is for bytes."""

    # --- Execution context --------------------------------------------------
    environment: Mapped[str] = mapped_column(String(64), default="staging", index=True)
    git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ci_run_id: Mapped[str] = mapped_column(String(255), index=True)
    """Groups everything from one pipeline execution. The join key for the most
    diagnostic question in the system: 'what else broke at the same moment?'"""

    ci_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ci_job_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Shard/worker identifier. If failures concentrate on one worker, the cause
    is that runner — a conclusion no amount of reading the test code reaches."""

    # --- System metrics during execution ------------------------------------
    # Denormalised onto the row rather than a separate metrics table: they are
    # always written together, always read together, and always 1:1.
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_io_read_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_io_write_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    network_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Timing -------------------------------------------------------------
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    """When the execution finished. Named ``timestamp`` (not ``finished_at``)
    to match the wire schema CI runners already post."""

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    """When *we* received it. Distinct from ``timestamp`` because results are
    frequently uploaded hours late from a retried pipeline; conflating the two
    makes 'failures in the last hour' silently wrong."""

    # --- Provenance ---------------------------------------------------------
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn, nullable=True)
    """The original framework-specific fragment. When a parser turns out to have
    dropped a field, this is what lets you backfill instead of asking CI to
    re-run a pipeline that no longer exists."""

    dedupe_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    """Idempotency key: hash of (ci_run_id, framework, test_name, attempt).
    CI upload steps get retried, and a duplicate upload that doubled every
    failure count would quietly corrupt every metric in the dashboard."""

    analyses: Mapped[list[FailureAnalysisDB]] = relationship(
        back_populates="test_run",
        cascade="all, delete-orphan",
        order_by="FailureAnalysisDB.created_at.desc()",
    )

    __table_args__ = (
        # Covers the hot path: "history of this test, newest first".
        Index("ix_test_runs_name_timestamp", "test_name", "timestamp"),
        # Covers the dashboard's default view: "recent failures".
        Index("ix_test_runs_status_timestamp", "status", "timestamp"),
        # Covers "everything from this pipeline run", used by cross-framework correlation.
        Index("ix_test_runs_ci_run_framework", "ci_run_id", "framework"),
        CheckConstraint("duration_ms >= 0", name="duration_non_negative"),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
    )

    def __repr__(self) -> str:
        return (
            f"<TestResultDB {self.test_name!r} {self.framework} "
            f"{self.status} attempt={self.attempt}>"
        )

    @property
    def is_failure(self) -> bool:
        """Did this execution fail to demonstrate the behaviour it covers?"""
        return self.status in (TestStatus.FAILED, TestStatus.FLAKY, TestStatus.ERROR)


class FailureAnalysisDB(Base):
    """One agent verdict about one failed test execution."""

    __tablename__ = "failure_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    test_result_id: Mapped[str] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"), index=True
    )

    status: Mapped[AnalysisStatus] = mapped_column(
        _enum_column(AnalysisStatus), default=AnalysisStatus.PENDING, index=True
    )
    """Written as PENDING *before* the API call. A worker that dies mid-analysis
    leaves a visible stuck row rather than no row at all — the difference between
    a bug you can see and one you cannot."""

    root_cause: Mapped[RootCauseCategory | None] = mapped_column(
        _enum_column(RootCauseCategory), nullable=True, index=True
    )
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The agent's argument. Non-negotiable: an unexplained classification cannot
    be checked by a human, so it cannot be trusted, so it will not be used."""

    key_evidence: Mapped[list[str]] = mapped_column(JSONColumn, default=list)
    """Specific observations the verdict rests on. Kept apart from ``reasoning``
    so a reviewer can check the *facts* without re-reading the prose."""

    suggestions: Mapped[list[str]] = mapped_column(JSONColumn, default=list)
    """Concrete remediation steps. Stored as real JSON rather than the
    spec's ``Text``-holding-a-JSON-string so the DB can query inside it."""

    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why the analysis failed, when status is FAILED."""

    # --- Model provenance ---------------------------------------------------
    # Without these, "accuracy improved last week" is uninterpretable: you cannot
    # tell whether the prompt got better, the model changed, or the mix of
    # incoming failures shifted.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    """Agentic loop turns used. A rising average means the agent is not getting
    what it needs from the initial context — a retrieval problem, not a prompt one."""

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Cost and speed per analysis — the numbers behind any "2 hours to 15
    minutes" claim, and the early warning when context assembly starts bloating."""

    cluster_id: Mapped[str | None] = mapped_column(
        ForeignKey("failure_clusters.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, onupdate=utcnow
    )

    test_run: Mapped[TestResultDB] = relationship(back_populates="analyses")
    cluster: Mapped[FailureClusterDB | None] = relationship(back_populates="analyses")
    feedback: Mapped[list[UserFeedbackDB]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0.0 AND confidence_score <= 1.0)",
            name="confidence_in_range",
        ),
        Index("ix_failure_analyses_root_cause_created", "root_cause", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<FailureAnalysisDB {self.root_cause} conf={self.confidence_score}>"


class FailureClusterDB(Base):
    """A recurring failure pattern, grouping executions that share a signature."""

    __tablename__ = "failure_clusters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    pattern_signature: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    """Same deterministic hash stored on ``test_runs.failure_signature``."""

    representative_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    """A human-readable exemplar, so the cluster is recognisable at a glance
    instead of being a bare hash in a list."""

    root_cause: Mapped[RootCauseCategory | None] = mapped_column(
        _enum_column(RootCauseCategory), nullable=True, index=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    occurrence_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    affected_tests: Mapped[list[str]] = mapped_column(JSONColumn, default=list)
    affected_frameworks: Mapped[list[str]] = mapped_column(JSONColumn, default=list)
    """One signature spanning Playwright *and* PyTest is near-proof of an
    application or environment fault: independent test code, identical symptom."""

    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    suggested_fix: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_muted: Mapped[bool] = mapped_column(Boolean, default=False)
    """Acknowledged known-issue. Suppresses re-analysis so the agent does not
    burn tokens re-deriving the same verdict 200 times a day."""

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, onupdate=utcnow
    )

    analyses: Mapped[list[FailureAnalysisDB]] = relationship(back_populates="cluster")

    __table_args__ = (
        CheckConstraint("occurrence_count >= 0", name="occurrence_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<FailureClusterDB {self.pattern_signature[:8]} n={self.occurrence_count}>"


class UserFeedbackDB(Base):
    """Human adjudication of an agent verdict — the accuracy ground truth."""

    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("failure_analyses.id", ondelete="CASCADE"), index=True
    )

    verdict: Mapped[FeedbackVerdict] = mapped_column(_enum_column(FeedbackVerdict), index=True)
    corrected_root_cause: Mapped[RootCauseCategory | None] = mapped_column(
        _enum_column(RootCauseCategory), nullable=True
    )
    """What the human says it actually was. The single most valuable column in
    the schema for improving the agent: 'wrong' teaches almost nothing, whereas
    'you said flaky_test, it was test_data' is a labelled example. Mine these
    for a confusion matrix and feed the systematic confusions back into the
    prompt as few-shot corrections."""

    feedback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    analysis: Mapped[FailureAnalysisDB] = relationship(back_populates="feedback")

    __table_args__ = (
        # One verdict per person per analysis; re-submitting is an update, not a
        # second vote. Otherwise a reviewer clicking twice skews the accuracy metric.
        UniqueConstraint("analysis_id", "submitted_by", name="analysis_id_submitted_by"),
    )

    def __repr__(self) -> str:
        return f"<UserFeedbackDB {self.verdict} on {self.analysis_id[:8]}>"
