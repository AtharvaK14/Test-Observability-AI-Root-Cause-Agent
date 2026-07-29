"""Populate the database with a realistic multi-framework failure history.

    python scripts/seed_demo.py            # ~3 weeks of history across 4 frameworks
    python scripts/seed_demo.py --reset    # wipe first

Why this exists: an empty dashboard cannot be evaluated, and neither can a
classifier. Each scenario below is planted with a *known* root cause and a
history shaped so the right answer is inferable from the signals.

⚠️  What this is NOT: a benchmark. These scenarios and the heuristic rules were
written by the same author, so the rules match the plants by construction and
score near-perfectly. That measures whether the pipeline is wired up correctly —
useful, but it is not an accuracy result and must never be quoted as one.

A real accuracy figure requires real failures from real suites, classified and
then adjudicated by a human through the feedback endpoint. Replace this data
with yours as soon as you have it.
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.analysis.clustering import (
    ClusterService,
    compute_dedupe_key,
    compute_failure_signature,
    extract_error_type,
)
from backend.db.models import TestResultDB
from backend.db.repository import TestResultRepository
from backend.db.session import create_all, drop_all, init_engine, session_scope
from backend.models.enums import TestFramework, TestStatus
from backend.utils import utcnow

# Deterministic: the same seed must produce the same dataset, or "accuracy went
# up" could just mean "the data got easier".
RNG = random.Random(20260729)


@dataclass
class Scenario:
    """One planted failure with a known correct answer."""

    test_name: str
    framework: TestFramework
    suite: str
    expected: str
    """The ground-truth category. Score classifiers against this."""

    error: str
    logs: str | None = None
    pass_rate: float = 0.95
    """Share of historical runs that passed — shapes the history signal."""

    flaky: bool = False
    """Alternate pass/fail instead of a clean streak."""

    duration_ms: int = 1200
    fail_duration_ms: int | None = None
    same_commit: bool = False
    """Fail on the same commit the last pass used — rules out a regression."""

    cpu_percent: float | None = None
    memory_mb: float | None = None
    shares_signature_with: str | None = None
    """Another scenario's key, to plant a cross-framework correlation."""


SCENARIOS: dict[str, Scenario] = {
    # --- app_bug: reliable test, new commit, assertion, normal duration ------
    "checkout_total": Scenario(
        test_name="checkout.spec.ts > guest checkout > calculates order total",
        framework=TestFramework.PLAYWRIGHT,
        suite="checkout",
        expected="app_bug",
        error="AssertionError: expected order total to equal 120 but received 0",
        pass_rate=1.0,
        duration_ms=1400,
    ),
    "discount_api": Scenario(
        test_name="tests/test_pricing.py::test_percentage_discount[10pct]",
        framework=TestFramework.PYTEST,
        suite="tests/test_pricing.py",
        expected="app_bug",
        error="AssertionError: assert 100.0 == 90.0",
        pass_rate=1.0,
        duration_ms=340,
    ),
    # --- flaky_test: alternating history, timing error -----------------------
    "cart_badge": Scenario(
        test_name="cart.cy.js > Cart > updates the item count badge",
        framework=TestFramework.CYPRESS,
        suite="Cart",
        expected="flaky_test",
        error=(
            "CypressError: Timed out retrying after 4000ms: Expected to find element "
            "'[data-test=cart-badge]' but never found it."
        ),
        pass_rate=0.55,
        flaky=True,
        duration_ms=2100,
        fail_duration_ms=9800,
    ),
    # --- flaky_test: same commit pass then fail (the decisive signal) --------
    "profile_save": Scenario(
        test_name="profile.spec.ts > saves the display name [webkit]",
        framework=TestFramework.PLAYWRIGHT,
        suite="profile",
        expected="flaky_test",
        error=(
            "Timeout 30000ms exceeded.\nwaiting for locator('#save-confirmation') "
            "to be visible"
        ),
        pass_rate=0.8,
        duration_ms=2400,
        fail_duration_ms=30000,
        same_commit=True,
    ),
    # --- environment: connectivity, corroborated across two frameworks ------
    "search_e2e": Scenario(
        test_name="search.spec.ts > returns results for a known term",
        framework=TestFramework.PLAYWRIGHT,
        suite="search",
        expected="environment",
        error="Error: connect ECONNREFUSED 10.0.4.19:9200 (search-index)",
        logs="[error] GET /api/search 503 Service Unavailable\nupstream connect error",
        pass_rate=0.97,
        duration_ms=900,
        fail_duration_ms=310,
    ),
    "search_api": Scenario(
        test_name="tests/test_search.py::test_search_returns_hits",
        framework=TestFramework.PYTEST,
        suite="tests/test_search.py",
        expected="environment",
        error="Error: connect ECONNREFUSED 10.0.4.19:9200 (search-index)",
        pass_rate=0.97,
        duration_ms=280,
        shares_signature_with="search_e2e",
    ),
    # --- test_data: fixture consumed by an earlier test ---------------------
    "coupon_redeem": Scenario(
        test_name="tests/test_coupons.py::test_redeem_single_use_coupon",
        framework=TestFramework.PYTEST,
        suite="tests/test_coupons.py",
        expected="test_data",
        error=(
            "AssertionError: coupon SAVE20 already exists and has been redeemed — "
            "no such record found in seeded fixture data"
        ),
        pass_rate=0.7,
        duration_ms=410,
    ),
    # --- infrastructure: runner resource exhaustion -------------------------
    "report_export": Scenario(
        test_name="com.acme.ReportTests.testLargeExport",
        framework=TestFramework.SELENIUM,
        suite="com.acme.ReportTests",
        expected="infrastructure",
        error="java.io.IOException: No space left on device",
        logs="Killed signal 9 (SIGKILL)\ncontainer exceeded memory limit",
        pass_rate=0.9,
        duration_ms=8000,
        cpu_percent=98.4,
        memory_mb=142.0,
    ),
    # --- external_dependency: third-party rate limit ------------------------
    "payment_charge": Scenario(
        test_name="tests/test_payments.py::test_charge_card_succeeds",
        framework=TestFramework.PYTEST,
        suite="tests/test_payments.py",
        expected="external_dependency",
        error=(
            "stripe.error.RateLimitError: 429 Too Many Requests — rate limit exceeded "
            "for this API key in test mode"
        ),
        pass_rate=0.93,
        duration_ms=760,
    ),
    # --- unknown: genuinely uninformative --------------------------------
    "legacy_widget": Scenario(
        test_name="legacy.cy.js > Widgets > renders the legacy widget",
        framework=TestFramework.CYPRESS,
        suite="Widgets",
        expected="unknown",
        error="Error: something went wrong",
        pass_rate=0.6,
        duration_ms=1500,
    ),
}

RUNS = 24
"""CI runs to simulate. Enough for history signals to be meaningful."""


def _passed(scenario: Scenario, run_index: int, is_last_run: bool) -> bool:
    """Decide one run's outcome.

    Failures are **streaky, not independent coin flips**. An i.i.d. draw at a
    70% pass rate produces a sequence that alternates constantly, which scores
    as highly flaky — so every scenario looked flaky regardless of its actual
    cause, and the flakiness rule swamped everything else.

    Real failures cluster: something breaks, stays broken for a few runs, and
    gets fixed. Modelling that is what makes the flakiness signal mean anything,
    because it is the *contrast* between streaky and alternating histories that
    distinguishes "broken" from "flaky" in the first place.
    """
    if is_last_run:
        # Every scenario ends in a failure, so there is always something to
        # score the classifiers on.
        return False
    if scenario.same_commit:
        # "Passed, then failed, with no commit in between" only holds if the
        # immediately preceding run actually PASSED. A red streak would put a
        # failure there instead, and the last-known-pass would fall back to an
        # older, different commit — quietly destroying the signal this scenario
        # exists to plant.
        return True
    if scenario.flaky:
        # Genuine non-determinism: alternates run to run.
        return run_index % 2 == 0
    # Otherwise: a contiguous red streak leading up to the final failure.
    failing_runs = max(1, round(RUNS * (1 - scenario.pass_rate)))
    return run_index < RUNS - failing_runs


def seed(reset: bool) -> None:
    init_engine()
    if reset:
        drop_all()
    create_all()

    now = utcnow()
    signatures: dict[str, str | None] = {}

    with session_scope() as session:
        repo = TestResultRepository(session)
        clusters = ClusterService(session)
        stored = failed = 0

        # Commit per run, generated up front so a scenario can deliberately
        # reuse the previous run's commit. (Computing `prev_commit` inside the
        # loop and reassigning it at the bottom does not work — the top-of-loop
        # assignment clobbers the carry-over on the next iteration, which is
        # exactly the bug this replaced.)
        commits = [f"{RNG.getrandbits(28):07x}" for _ in range(RUNS)]

        for run_index in range(RUNS):
            timestamp = now - timedelta(hours=(RUNS - run_index) * 8)
            ci_run_id = f"seed-run-{run_index:03d}"

            for key, scenario in SCENARIOS.items():
                is_last_run = run_index == RUNS - 1
                passed = _passed(scenario, run_index, is_last_run)

                run_commit = commits[run_index]
                if is_last_run and scenario.same_commit:
                    # Reuse the *actual* previous run's commit, so "passed and
                    # failed on the same commit" is true and provable from the
                    # stored data rather than merely intended.
                    run_commit = commits[run_index - 1]

                error_type = extract_error_type(scenario.error, framework=scenario.framework)
                signature = (
                    signatures.get(scenario.shares_signature_with or "")
                    or compute_failure_signature(scenario.error, error_type)
                )
                signatures.setdefault(key, signature)

                duration = scenario.duration_ms
                if not passed:
                    duration = scenario.fail_duration_ms or scenario.duration_ms
                duration = max(1, int(duration * RNG.uniform(0.9, 1.1)))

                row = TestResultDB(
                    test_name=scenario.test_name,
                    test_suite=scenario.suite,
                    framework=scenario.framework,
                    status=TestStatus.PASSED if passed else TestStatus.FAILED,
                    duration_ms=duration,
                    error_type=None if passed else error_type,
                    error_message=None if passed else scenario.error,
                    logs=None if passed else scenario.logs,
                    failure_signature=None if passed else signature,
                    environment="staging",
                    git_commit=run_commit,
                    git_branch="main",
                    ci_run_id=ci_run_id,
                    ci_provider="github-actions",
                    cpu_percent=None if passed else scenario.cpu_percent,
                    memory_mb=None if passed else scenario.memory_mb,
                    timestamp=timestamp,
                    dedupe_key=compute_dedupe_key(
                        ci_run_id, scenario.framework, scenario.test_name, 0
                    ),
                )
                repo.add(row)
                stored += 1
                if not passed:
                    failed += 1
                    clusters.upsert_for_result(row)

    print(f"seeded {stored} executions across {RUNS} CI runs ({failed} failures)")
    print(f"{len(SCENARIOS)} scenarios, each ending in a failure with a known cause:\n")
    for key, scenario in SCENARIOS.items():
        print(f"  {scenario.expected:<20} {key:<16} {scenario.framework.value}")
    print(
        "\nGround truth lives in scripts/seed_demo.py::SCENARIOS[*].expected — "
        "score classifiers against it with scripts/score_classifier.py"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset", action="store_true", help="drop all tables before seeding"
    )
    args = parser.parse_args()
    seed(reset=args.reset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
