"""Pydantic schemas for test execution data — the ingest/read wire contract.

Split into ``*Create`` (what CI runners send) and ``*Read`` (what the dashboard
receives) rather than one shared model. They genuinely differ: a runner has no
``id`` and no ``failure_signature`` (we derive both), and the dashboard needs
computed fields the database does not store. Sharing one model forces every
field optional, which throws away the validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.models.enums import TestFramework, TestStatus


class SystemMetrics(BaseModel):
    """Host resource state captured during a test's execution.

    Rarely present, disproportionately valuable when it is. A browser test that
    times out on a runner sitting at 98% CPU with 200MB free almost certainly
    timed out *because of that*, not because of a bad locator — and no amount of
    staring at the test code will reveal it. Capturing these turns a category of
    "unexplained flake" into a diagnosable infrastructure problem.
    """

    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory_mb: float | None = Field(default=None, ge=0)
    disk_io_read_mb: float | None = Field(default=None, ge=0)
    disk_io_write_mb: float | None = Field(default=None, ge=0)
    network_latency_ms: float | None = Field(default=None, ge=0)

    def is_empty(self) -> bool:
        """True when no metric was captured — lets the context builder omit the
        whole block instead of feeding the agent a wall of nulls to reason about."""
        return all(value is None for value in self.model_dump().values())


class TestResultCreate(BaseModel):
    """A single normalised test execution attempt, as submitted for ingestion.

    Every framework parser produces these. Once a Cypress result and a PyTest
    result are both instances of this class, everything downstream — history,
    clustering, agent context — works identically for both, which is the entire
    payoff of normalisation.
    """

    model_config = ConfigDict(extra="forbid")

    test_name: str = Field(min_length=1, max_length=512)
    test_suite: str | None = Field(default=None, max_length=512)
    test_file: str | None = Field(default=None, max_length=512)
    framework: TestFramework
    status: TestStatus
    duration_ms: int = Field(default=0, ge=0)
    attempt: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)

    error_type: str | None = Field(default=None, max_length=255)
    error_message: str | None = None
    stack_trace: str | None = None
    logs: str | None = None

    screenshot_url: str | None = Field(default=None, max_length=1024)
    video_url: str | None = Field(default=None, max_length=1024)
    trace_url: str | None = Field(default=None, max_length=1024)

    environment: str = Field(default="staging", max_length=64)
    git_commit: str | None = Field(default=None, max_length=64)
    git_branch: str | None = Field(default=None, max_length=255)
    ci_run_id: str = Field(default="local", max_length=255)
    ci_provider: str | None = Field(default=None, max_length=64)
    ci_job_url: str | None = Field(default=None, max_length=1024)
    worker_id: str | None = Field(default=None, max_length=128)

    metrics: SystemMetrics = Field(default_factory=SystemMetrics)

    started_at: datetime | None = None
    timestamp: datetime | None = None
    raw_payload: dict[str, Any] | None = None

    @field_validator("test_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        """Normalise whitespace in the test name.

        Test identity is a string match, so a trailing space silently forks one
        test's history into two — and the resulting "brand new test that has
        never passed" is exactly the wrong conclusion for the agent to draw.
        """
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("test_name cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def _require_error_detail_on_failure(self) -> TestResultCreate:
        """A failure with no error text at all is almost always a parser bug.

        We do not reject it — losing real data to a strict schema is worse — but
        we synthesise a marker so the gap is visible in the dashboard rather than
        showing up as a mysteriously blank failure that the agent then confidently
        classifies as UNKNOWN.
        """
        if self.status in (TestStatus.FAILED, TestStatus.ERROR) and not any(
            (self.error_message, self.stack_trace, self.logs)
        ):
            self.error_message = "<no error detail reported by test framework>"
        return self


class TestResultRead(BaseModel):
    """A stored test execution, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    test_name: str
    test_suite: str | None = None
    test_file: str | None = None
    framework: TestFramework
    status: TestStatus
    duration_ms: int
    attempt: int
    retry_count: int

    error_type: str | None = None
    error_message: str | None = None
    stack_trace: str | None = None
    logs: str | None = None
    failure_signature: str | None = None

    screenshot_url: str | None = None
    video_url: str | None = None
    trace_url: str | None = None

    environment: str
    git_commit: str | None = None
    git_branch: str | None = None
    ci_run_id: str
    ci_provider: str | None = None
    ci_job_url: str | None = None
    worker_id: str | None = None

    cpu_percent: float | None = None
    memory_mb: float | None = None
    network_latency_ms: float | None = None

    started_at: datetime | None = None
    timestamp: datetime
    created_at: datetime


class TestResultSummary(BaseModel):
    """Trimmed view for list endpoints.

    Failure lists routinely carry megabyte stack traces; sending them for 50 rows
    the user has not opened yet makes the dashboard feel broken. Detail views
    fetch the full record.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    test_name: str
    test_suite: str | None = None
    framework: TestFramework
    status: TestStatus
    duration_ms: int
    attempt: int
    error_type: str | None = None
    error_message: str | None = None
    failure_signature: str | None = None
    environment: str
    git_branch: str | None = None
    git_commit: str | None = None
    ci_run_id: str
    timestamp: datetime

    @field_validator("error_message")
    @classmethod
    def _clamp_error(cls, value: str | None) -> str | None:
        if value and len(value) > 500:
            return value[:500] + "..."
        return value


class IngestResponse(BaseModel):
    """Outcome of an ingestion request.

    Reports ``skipped`` and ``errors`` explicitly rather than just a success
    count. A CI step that uploads 400 results and gets back ``{"status": "ok"}``
    while 380 were silently dropped is worse than no observability at all,
    because it manufactures false confidence.
    """

    status: str
    framework: TestFramework
    ingested: int
    skipped_duplicates: int = 0
    failures_detected: int = 0
    analyses_queued: int = 0
    errors: list[str] = Field(default_factory=list)
    test_result_ids: list[str] = Field(default_factory=list)
    ci_run_id: str | None = None
    received_at: datetime
