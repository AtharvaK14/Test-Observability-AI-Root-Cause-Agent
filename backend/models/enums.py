"""Controlled vocabularies shared by the API layer and the database layer.

These are ``str`` enums so they serialise straight to JSON and compare equal to
plain strings — which matters because CI runners POST raw strings and SQL
queries filter on raw strings.
"""

from __future__ import annotations

from enum import StrEnum


class TestFramework(StrEnum):
    """Test runners this system can ingest.

    Each value has a corresponding parser in ``backend.api.ingest``. The point of
    normalising to one vocabulary is cross-framework analysis: a login flow may
    be covered by a Playwright E2E test *and* a PyTest API test, and when both
    fail in the same CI run that correlation is the strongest possible signal
    that the application broke rather than the tests.
    """

    PLAYWRIGHT = "playwright"
    CYPRESS = "cypress"
    PYTEST = "pytest"
    SELENIUM = "selenium"


class TestStatus(StrEnum):
    """Normalised outcome of a single test execution.

    ``FLAKY`` is not a runner-reported status in most frameworks — it is derived:
    a test that failed on attempt N and passed on attempt N+1 within the same CI
    run. Playwright and Cypress report retries natively; for PyTest this comes
    from ``pytest-rerunfailures``. Keeping FLAKY distinct from PASSED is the
    whole point of the system: a suite that is "green after retries" is hiding
    exactly the failures worth investigating.
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    FLAKY = "flaky"
    ERROR = "error"  # Runner crashed / fixture or hook error — not a test assertion


# Statuses that represent a test that did not do its job. Used everywhere we ask
# "should the agent look at this?", so it lives in one place rather than being
# re-spelled as an ad-hoc list in each query.
FAILING_STATUSES: frozenset[TestStatus] = frozenset(
    {TestStatus.FAILED, TestStatus.FLAKY, TestStatus.ERROR}
)


class RootCauseCategory(StrEnum):
    """The classification taxonomy the agent must choose from.

    Chosen so that each category maps to a *different owner and a different
    remediation*, which is what makes the classification actionable:

    - ``APP_BUG``          -> file a defect against the dev team; the test worked.
    - ``FLAKY_TEST``       -> the test author fixes the test; the app is fine.
    - ``ENVIRONMENT``      -> the environment/platform team; deploy or service state.
    - ``TEST_DATA``        -> fixtures/seed data; often a stale or consumed record.
    - ``INFRASTRUCTURE``   -> CI platform; runner OOM, disk, permissions, image drift.
    - ``EXTERNAL_DEPENDENCY`` -> third party; usually not fixable, needs a stub.
    - ``UNKNOWN``          -> explicit "insufficient evidence", never a silent guess.

    ``UNKNOWN`` existing as a first-class option matters: a taxonomy without an
    escape hatch pushes the model into confident wrong answers, which destroys
    trust in the tool faster than admitting uncertainty.
    """

    APP_BUG = "app_bug"
    FLAKY_TEST = "flaky_test"
    ENVIRONMENT = "environment"
    TEST_DATA = "test_data"
    INFRASTRUCTURE = "infrastructure"
    EXTERNAL_DEPENDENCY = "external_dependency"
    UNKNOWN = "unknown"


class AnalysisStatus(StrEnum):
    """Lifecycle of an agent analysis record.

    Analyses are persisted *before* the LLM is called (``PENDING``) so a crashed
    worker leaves a visible stuck row instead of a silent gap. ``FAILED`` records
    the agent erroring out (rate limit, malformed response) and keeps the error
    text, so "the agent never ran" and "the agent ran and gave up" stay
    distinguishable when you are measuring coverage.
    """

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class FeedbackVerdict(StrEnum):
    """Human adjudication of an analysis — the ground truth for accuracy metrics.

    Without this, "80% classification accuracy" is an unfalsifiable claim.
    """

    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNCERTAIN = "uncertain"
