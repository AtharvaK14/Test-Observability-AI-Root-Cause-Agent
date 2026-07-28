"""Pydantic schemas for agent output, feedback, clusters, and dashboard metrics.

``AgentClassification`` is the important one: it is the *validation boundary*
between an LLM's free-form text and the database. Everything the model returns
passes through it, so an invented category or a confidence of 1.5 fails loudly
at parse time instead of being stored and then rendered as authoritative.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.models.enums import (
    AnalysisStatus,
    FeedbackVerdict,
    RootCauseCategory,
    TestFramework,
)
from backend.models.test_result import TestResultSummary


class AgentClassification(BaseModel):
    """The structured verdict the agent must produce.

    Doubles as the tool schema sent to Claude, so the contract is defined once
    and the model is constrained to it at generation time rather than merely
    checked afterwards.
    """

    model_config = ConfigDict(extra="ignore")

    category: RootCauseCategory
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)
    key_evidence: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    requires_human_review: bool = False

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, value: Any) -> Any:
        """Accept ``APP_BUG`` / ``app_bug`` / ``App Bug`` alike.

        The prompt lists categories in upper case (they read as labels), the
        enum stores lower case. Rather than fight the model over casing —
        a battle that produces occasional silent misclassification to UNKNOWN —
        normalise here. Genuinely invented categories still raise.
        """
        if isinstance(value, str):
            return value.strip().lower().replace(" ", "_").replace("-", "_")
        return value

    @field_validator("suggestions", "key_evidence")
    @classmethod
    def _drop_empty(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


class FailureAnalysisRead(BaseModel):
    """A stored agent verdict, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    test_result_id: str
    status: AnalysisStatus
    root_cause: RootCauseCategory | None = None
    confidence_score: float | None = None
    reasoning: str | None = None
    key_evidence: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    requires_human_review: bool = False
    error_message: str | None = None

    model: str | None = None
    prompt_version: str | None = None
    iterations: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None

    cluster_id: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class FailureWithAnalysis(BaseModel):
    """A failure paired with its current verdict — the dashboard's row shape."""

    test: TestResultSummary
    analysis: FailureAnalysisRead | None = None


class PaginatedFailures(BaseModel):
    """Failure feed with pagination metadata."""

    items: list[FailureWithAnalysis]
    total: int
    limit: int
    offset: int


class AnalysisRequest(BaseModel):
    """Ask the agent to analyse a specific test result."""

    test_result_id: str
    force: bool = Field(
        default=False,
        description=(
            "Re-analyse even if a verdict exists. Adds a row rather than "
            "replacing one, so prompt versions stay comparable."
        ),
    )


class FeedbackCreate(BaseModel):
    """Human adjudication of a verdict.

    ``corrected_root_cause`` is requested (not required) on INCORRECT because it
    is what turns feedback from a complaint into a labelled training example.
    """

    verdict: FeedbackVerdict
    corrected_root_cause: RootCauseCategory | None = None
    feedback_text: str | None = Field(default=None, max_length=4000)
    submitted_by: str | None = Field(default=None, max_length=255)


class FeedbackRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    analysis_id: str
    verdict: FeedbackVerdict
    corrected_root_cause: RootCauseCategory | None = None
    feedback_text: str | None = None
    submitted_by: str | None = None
    created_at: datetime


class ClusterRead(BaseModel):
    """A recurring failure pattern."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    pattern_signature: str
    representative_error: str | None = None
    root_cause: RootCauseCategory | None = None
    confidence: float | None = None
    occurrence_count: int
    affected_tests: list[str] = Field(default_factory=list)
    affected_frameworks: list[str] = Field(default_factory=list)
    first_seen: datetime
    last_seen: datetime
    suggested_fix: str | None = None
    is_muted: bool = False


# --- Dashboard aggregates ---------------------------------------------------


class TrendPoint(BaseModel):
    """One day in a pass-rate trend."""

    date: str
    total: int
    passed: int
    failed: int
    flaky: int
    pass_rate: float
    avg_duration_ms: float | None = None


class TestTrends(BaseModel):
    """Trend series plus the derived judgement a human actually wants.

    The chart shows what happened; ``verdict`` says whether to care. A test
    drifting from 99% to 80% over two weeks is invisible in a daily red/green
    view and is exactly the thing worth catching early.
    """

    test_name: str
    framework: TestFramework | None = None
    window_days: int
    total_runs: int
    pass_rate: float
    flakiness_score: float
    trend_direction: str
    verdict: str
    points: list[TrendPoint]


class RootCauseSlice(BaseModel):
    root_cause: RootCauseCategory
    count: int
    avg_confidence: float | None = None
    percentage: float


class FrameworkBreakdown(BaseModel):
    """Pass/fail totals for one framework in a window."""

    framework: str
    total_runs: int
    failed_runs: int
    pass_rate: float


class ConfusionPair(BaseModel):
    """A systematic misclassification: what the agent said vs what it was.

    Ranked by frequency, this is the prompt-improvement backlog. Typed rather
    than a bare dict so the OpenAPI schema documents it for the dashboard.
    """

    predicted: RootCauseCategory | None = None
    actual: RootCauseCategory | None = None
    count: int


class FlakyTestEntry(BaseModel):
    test_name: str
    framework: str
    total_runs: int
    failed_runs: int
    flaky_runs: int
    failure_rate: float
    pass_rate: float


class AgentMetrics(BaseModel):
    """Everything needed to defend an accuracy or time-saved claim."""

    window_days: int
    total_completed_analyses: int
    accuracy: float | None = None
    correct: int = 0
    incorrect: int = 0
    uncertain: int = 0
    adjudicated: int = 0
    feedback_coverage: float | None = None
    avg_latency_ms: float | None = None
    max_latency_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    confusion_pairs: list[ConfusionPair] = Field(default_factory=list)


class DashboardSummary(BaseModel):
    """Top-of-dashboard counters."""

    window_hours: int
    total_runs: int
    total_failures: int
    unique_failing_tests: int
    analyzed_failures: int
    pending_analyses: int
    needs_review: int
    root_cause_distribution: list[RootCauseSlice] = Field(default_factory=list)
    framework_breakdown: list[FrameworkBreakdown] = Field(default_factory=list)
    top_clusters: list[ClusterRead] = Field(default_factory=list)
