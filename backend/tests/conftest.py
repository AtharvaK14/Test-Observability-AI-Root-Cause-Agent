"""Shared test fixtures.

The whole suite runs against in-memory SQLite with no API key and no network.
That is a deliberate property, not an accident of convenience: a test suite that
needs Postgres running and an Anthropic key funded is a suite that gets skipped,
and a skipped suite protects nothing.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

# Set before importing anything that reads settings — pydantic-settings caches.
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("ENVIRONMENT", "ci")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.config import Settings, get_settings
from backend.db.models import TestResultDB
from backend.db.repository import TestResultRepository
from backend.db.session import (
    create_all,
    drop_all,
    get_session_factory,
    init_engine,
)
from backend.ingest.base import RunMetadata
from backend.models.enums import TestFramework, TestStatus
from backend.utils import utcnow

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def settings() -> Settings:
    """Settings with the agent switched off — tests that want it opt in."""
    get_settings.cache_clear()
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        environment="ci",
        agent_enabled=False,
        auto_analyze_on_ingest=False,
        anthropic_api_key="test-key-not-used",
    )


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    """A clean database per test.

    Rebuilt rather than rolled back: SQLite's in-memory database lives with the
    connection, and sharing one across tests makes failures order-dependent —
    the single most expensive kind of test flakiness to debug, and an
    embarrassing one to ship in a project about flaky tests.
    """
    init_engine(settings, force=True)
    create_all()
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
        drop_all()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """An HTTP client wired to a fresh database."""
    from backend.main import create_app

    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    init_engine(settings, force=True)
    create_all()
    with TestClient(app) as test_client:
        yield test_client
    drop_all()


@pytest.fixture
def metadata() -> RunMetadata:
    return RunMetadata(
        ci_run_id="run-1",
        environment="staging",
        git_commit="abc1234",
        git_branch="main",
        ci_provider="github-actions",
    )


@pytest.fixture
def fixture_bytes() -> Any:
    """Read a checked-in report fixture."""

    def _read(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()

    return _read


# --- Data builders ----------------------------------------------------------


@pytest.fixture
def make_result() -> Any:
    """Build a TestResultDB with sensible defaults."""

    def _make(**overrides: Any) -> TestResultDB:
        defaults: dict[str, Any] = {
            "test_name": "suite > a test",
            "framework": TestFramework.PLAYWRIGHT,
            "status": TestStatus.PASSED,
            "duration_ms": 1000,
            "ci_run_id": "run-1",
            "environment": "staging",
            "timestamp": utcnow(),
        }
        defaults.update(overrides)
        return TestResultDB(**defaults)

    return _make


@pytest.fixture
def seed_history(session: Session, make_result: Any) -> Any:
    """Seed N green runs followed by one failure — the classic regression shape."""

    def _seed(
        test_name: str = "checkout > completes purchase",
        green_runs: int = 20,
        error: str = "AssertionError: expected total 120 but got 0",
        error_type: str = "AssertionError",
        signature: str = "sig-abc",
        **failure_overrides: Any,
    ) -> TestResultDB:
        repo = TestResultRepository(session)
        now = utcnow()
        for i in range(green_runs):
            repo.add(
                make_result(
                    test_name=test_name,
                    status=TestStatus.PASSED,
                    ci_run_id=f"run-{i}",
                    git_commit="aaa1111",
                    timestamp=now - timedelta(hours=green_runs + 1 - i),
                )
            )
        failure = repo.add(
            make_result(
                test_name=test_name,
                status=TestStatus.FAILED,
                ci_run_id="run-fail",
                git_commit="bbb2222",
                duration_ms=failure_overrides.pop("duration_ms", 1100),
                error_type=error_type,
                error_message=error,
                failure_signature=signature,
                timestamp=now,
                **failure_overrides,
            )
        )
        session.flush()
        return failure

    return _seed


# --- Agent stubbing ---------------------------------------------------------


def content_block(**fields: Any) -> SimpleNamespace:
    """A stand-in for an SDK content block."""
    return SimpleNamespace(**fields)


def api_response(
    content: list[Any],
    stop_reason: str = "tool_use",
    input_tokens: int = 100,
    output_tokens: int = 50,
    stop_details: Any = None,
) -> SimpleNamespace:
    """A stand-in for an SDK Message."""
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class StubAnthropicClient:
    """Replays a scripted sequence of responses and records the requests made.

    This is the seam that makes the agent testable. Without an injectable
    client, verifying "does the loop stop on the terminal tool?" or "does a
    refusal produce a FAILED row?" would need a live API key, real money, and a
    non-deterministic model.
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        outer = self

        class _Messages:
            def create(self, **kwargs: Any) -> Any:
                # Snapshot `messages`: the agent reuses and appends to one list
                # across iterations, so storing the reference would make every
                # recorded call show the *final* conversation state.
                recorded = dict(kwargs)
                if "messages" in recorded:
                    recorded["messages"] = list(recorded["messages"])
                outer.calls.append(recorded)
                if not outer.script:
                    raise AssertionError("stub client ran out of scripted responses")
                return outer.script.pop(0)

        # Annotated Any: these stand in for SDK resource objects, and pinning
        # them to the stub's own inner class would stop subclasses (see the
        # rate-limit stub in test_agent.py) from swapping in their own.
        self.messages: Any = _Messages()
        self.beta: Any = SimpleNamespace(messages=_Messages())


VALID_VERDICT: dict[str, Any] = {
    "category": "app_bug",
    "confidence": 0.88,
    "reasoning": "Twenty consecutive passes, then a failure on a new commit.",
    "key_evidence": ["100% pass rate over 20 prior runs", "first failure on commit bbb2222"],
    "suggestions": ["Open a defect against the checkout service"],
    "requires_human_review": False,
}


@pytest.fixture
def verdict_response() -> Any:
    """A scripted response that submits a valid classification."""

    def _make(**overrides: Any) -> SimpleNamespace:
        payload = {**VALID_VERDICT, **overrides}
        return api_response(
            [
                content_block(
                    type="tool_use",
                    id="tool-1",
                    name="submit_classification",
                    input=payload,
                )
            ]
        )

    return _make
