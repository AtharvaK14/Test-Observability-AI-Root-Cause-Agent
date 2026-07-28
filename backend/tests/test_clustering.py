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
