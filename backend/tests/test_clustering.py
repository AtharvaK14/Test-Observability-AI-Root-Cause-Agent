"""Failure fingerprinting.

Pure functions, so these are the cheapest and most valuable tests in the suite:
fingerprinting decides whether 487 overnight failures collapse into 4 problems
or stay 487, and it must keep working when the Anthropic API is down.
"""

from __future__ import annotations

import pytest

from backend.analysis.clustering import (
    compute_dedupe_key,
    compute_failure_signature,
    extract_error_type,
    normalize_error_text,
)
from backend.models.enums import TestFramework


def signature(message: str) -> str | None:
    return compute_failure_signature(message, extract_error_type(message))


class TestNormalisation:
    @pytest.mark.parametrize(
        ("left", "right", "why"),
        [
            (
                "Timeout 30000ms exceeded waiting for locator('#submit')",
                "Timeout 29997ms exceeded waiting for locator('#submit')",
                "duration jitter — the single most common cause of cluster fragmentation",
            ),
            (
                "Order 7f3b9a2c-1e4d-4a6b-9c8e-2f1a3b4c5d6e not found",
                "Order 11111111-2222-3333-4444-555555555555 not found",
                "per-run UUIDs",
            ),
            (
                "at /home/runner/work/repo-abc/tests/checkout.spec.ts:42",
                "at /home/runner/work/repo-xyz/tests/checkout.spec.ts:42",
                "CI checkout paths embed a run id",
            ),
            (
                "connect ECONNREFUSED 10.0.0.4:5432",
                "connect ECONNREFUSED 10.0.9.7:5432",
                "ephemeral container IPs",
            ),
            (
                "Failed at 2026-07-28T10:00:00Z",
                "Failed at 2026-07-29T18:22:41Z",
                "timestamps",
            ),
        ],
    )
    def test_volatile_values_collapse(self, left: str, right: str, why: str) -> None:
        assert signature(left) == signature(right), f"should collapse: {why}"

    @pytest.mark.parametrize(
        ("left", "right", "why"),
        [
            (
                "Timeout 30000ms exceeded waiting for locator('#submit')",
                "Timeout 30000ms exceeded waiting for locator('#cart-icon')",
                "different selectors are different problems",
            ),
            (
                "AssertionError: expected 200",
                "TypeError: cannot read property of undefined",
                "different error families",
            ),
        ],
    )
    def test_meaningful_differences_survive(self, left: str, right: str, why: str) -> None:
        assert signature(left) != signature(right), f"should stay distinct: {why}"

    def test_duration_with_unit_suffix_is_normalised(self) -> None:
        """Regression: `\\b\\d+\\b` never matches the digits in "30000ms".

        There is no word boundary between "0" and "m" — both are word
        characters — so a trailing \\b silently skips every duration in every
        timeout message, which is precisely the value that must be normalised.
        """
        assert "<n>" in normalize_error_text("Timeout 30000ms exceeded")
        assert "30000" not in normalize_error_text("Timeout 30000ms exceeded")

    def test_no_error_text_yields_no_signature(self) -> None:
        """Detail-free failures must not all collapse into one giant cluster."""
        assert compute_failure_signature(None) is None
        assert compute_failure_signature("") is None
        assert compute_failure_signature("   ") is None

    def test_signature_is_stable_across_calls(self) -> None:
        message = "AssertionError: expected 1 to equal 2"
        assert signature(message) == signature(message)


class TestErrorTypeExtraction:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("AssertionError: expected 200 to equal 404", "AssertionError"),
            ("selenium.common.exceptions.TimeoutException: Message: x", "TimeoutException"),
            ("Timeout 30000ms exceeded waiting for locator", "Timeout"),
            ("assert 3 == 4", "assert"),
            ("TypeError: t.map is not a function", "TypeError"),
        ],
    )
    def test_extracts_exception_class(self, message: str, expected: str) -> None:
        assert extract_error_type(message) == expected

    def test_falls_back_to_framework_label(self) -> None:
        """Unrecognisable error text still gets a family, not None.

        Returning None would hand the LLM work a regex does for free."""
        assert (
            extract_error_type("something went badly wrong", framework=TestFramework.CYPRESS)
            == "CypressError"
        )

    def test_no_text_means_no_type(self) -> None:
        assert extract_error_type(None, None, TestFramework.PYTEST) is None


class TestDedupeKey:
    def test_same_execution_yields_same_key(self) -> None:
        args = ("run-1", TestFramework.PLAYWRIGHT, "suite > test", 0)
        assert compute_dedupe_key(*args) == compute_dedupe_key(*args)

    @pytest.mark.parametrize(
        "changed",
        [
            ("run-2", TestFramework.PLAYWRIGHT, "suite > test", 0),
            ("run-1", TestFramework.CYPRESS, "suite > test", 0),
            ("run-1", TestFramework.PLAYWRIGHT, "suite > other", 0),
            ("run-1", TestFramework.PLAYWRIGHT, "suite > test", 1),
        ],
    )
    def test_each_component_changes_the_key(self, changed: tuple) -> None:
        base = compute_dedupe_key("run-1", TestFramework.PLAYWRIGHT, "suite > test", 0)
        assert compute_dedupe_key(*changed) != base

    def test_retry_attempts_are_distinct(self) -> None:
        """Attempts must not deduplicate each other — they are separate executions."""
        first = compute_dedupe_key("run-1", TestFramework.PLAYWRIGHT, "t", 0)
        second = compute_dedupe_key("run-1", TestFramework.PLAYWRIGHT, "t", 1)
        assert first != second


class TestConcurrentClusterCreation:
    """Regression: two ingests racing to create the same new cluster.

    Both saw "no cluster" and both INSERTed; the loser hit the unique index on
    pattern_signature and its whole upload failed with a 500. Found by the k6
    load test against Postgres. Reproduced here by making the existence check
    miss a row that is already there — exactly what the losing request saw.
    """

    def test_losing_the_insert_race_joins_the_existing_cluster(
        self, session, make_result, monkeypatch
    ) -> None:
        from backend.analysis.clustering import ClusterService
        from backend.models.enums import TestStatus

        service = ClusterService(session)
        first = make_result(
            test_name="a", status=TestStatus.FAILED, failure_signature="sig-race"
        )
        session.add(first)
        winner = service.upsert_for_result(first)
        session.commit()

        real_lookup = service.clusters.get_by_signature
        calls = {"n": 0}

        def stale_then_real(sig: str, **kwargs):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_lookup(sig, **kwargs)

        monkeypatch.setattr(service.clusters, "get_by_signature", stale_then_real)

        second = make_result(
            test_name="b", status=TestStatus.FAILED, failure_signature="sig-race"
        )
        session.add(second)
        joined = service.upsert_for_result(second)
        session.commit()

        assert joined is not None and winner is not None
        assert joined.id == winner.id
        assert joined.occurrence_count == 2
        assert joined.affected_tests == ["a", "b"]
        # The test row itself survived the failed INSERT's savepoint rollback.
        assert session.get(type(second), second.id) is not None


class TestClusterLocking:
    """Regression: concurrent ingests lost over half of all counter increments.

    occurrence_count was read, incremented in Python, and written back, so two
    transactions reading N both wrote N+1. Found by comparing counters with
    actual row counts after a k6 run against Postgres. The fix row-locks the
    cluster; these tests pin the two properties that fix depends on.
    """

    def test_cluster_lookup_for_update_locks_the_row_on_postgres(
        self, session, monkeypatch
    ) -> None:
        from sqlalchemy.dialects import postgresql

        from backend.db.repository import FailureClusterRepository

        captured = []
        real_execute = session.execute

        def capture(stmt, *args, **kwargs):
            captured.append(stmt)
            return real_execute(stmt, *args, **kwargs)

        monkeypatch.setattr(session, "execute", capture)
        FailureClusterRepository(session).get_by_signature("sig", for_update=True)

        # Compiled for Postgres: SQLite, which the suite runs on, drops the clause.
        sql = str(captured[0].compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE" in sql

    def test_ingest_upserts_clusters_in_signature_order(self, session, monkeypatch) -> None:
        """One global lock order is what keeps concurrent ingests deadlock-free."""
        from backend.analysis.clustering import ClusterService
        from backend.ingest.base import ParsedReport
        from backend.ingest.service import IngestionService
        from backend.models.enums import TestStatus
        from backend.models.test_result import TestResultCreate

        seen: list[str | None] = []
        real_upsert = ClusterService.upsert_for_result

        def spy(self, result):
            seen.append(result.failure_signature)
            return real_upsert(self, result)

        monkeypatch.setattr(ClusterService, "upsert_for_result", spy)

        errors = ["ZeroDivisionError: z", "KeyError: 'k'", "ValueError: v", "AssertionError: a"]
        report = ParsedReport(
            framework=TestFramework.PYTEST,
            results=[
                TestResultCreate(
                    test_name=f"t{i}",
                    framework=TestFramework.PYTEST,
                    status=TestStatus.FAILED,
                    error_message=msg,
                    ci_run_id="run-order",
                    environment="ci",
                )
                for i, msg in enumerate(errors)
            ],
        )
        IngestionService(session).ingest(report)

        assert len(seen) == len(errors)
        assert seen == sorted(seen, key=lambda s: s or "")
