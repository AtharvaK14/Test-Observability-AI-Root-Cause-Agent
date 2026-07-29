"""The rule-based baseline classifier.

Written against hand-built contexts rather than the seed fixtures on purpose.
``scripts/seed_demo.py`` and the rules share an author, so scoring one against
the other is circular; these tests assert the *behaviours* that make the
baseline trustworthy — precedence, abstention, the same-commit veto — each of
which can be checked independently of any dataset.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from backend.analysis.context_retriever import ContextRetriever
from backend.analysis.heuristics import (
    MAX_HEURISTIC_CONFIDENCE,
    RULES_VERSION,
    HeuristicAnalyzer,
    HeuristicClassifier,
)
from backend.config import Settings
from backend.models.enums import AnalysisStatus, RootCauseCategory, TestFramework, TestStatus


def classify(session: Session, settings: Settings, failure_id: str):
    context = ContextRetriever(session, settings).get_failure_context(failure_id)
    assert context is not None
    return HeuristicClassifier(settings).classify(context)


class TestDirectSignals:
    """Rules that fire on strings naming a cause in the failure output."""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            ("java.io.IOException: No space left on device", RootCauseCategory.INFRASTRUCTURE),
            ("Container killed — exit code 137", RootCauseCategory.INFRASTRUCTURE),
            ("stripe.error.RateLimitError: 429 Too Many Requests", RootCauseCategory.EXTERNAL_DEPENDENCY),
            ("AuthError: token expired for the payments API key", RootCauseCategory.EXTERNAL_DEPENDENCY),
            ("Error: connect ECONNREFUSED 10.0.4.19:5432", RootCauseCategory.ENVIRONMENT),
            ("upstream returned 503 Service Unavailable", RootCauseCategory.ENVIRONMENT),
            ("IntegrityError: duplicate key value violates unique constraint", RootCauseCategory.TEST_DATA),
            ("no such user found in the seeded fixture data", RootCauseCategory.TEST_DATA),
        ],
    )
    def test_recognises_the_cause_named_in_the_error(
        self, session: Session, settings: Settings, seed_history, error: str, expected
    ) -> None:
        failure = seed_history(green_runs=10, error=error, error_type="Error")
        assert classify(session, settings, failure.id).category == expected

    def test_direct_evidence_outranks_a_history_pattern(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """The precedence rule, stated as a test.

        A reliable test that just went red on a new commit looks like an app bug
        by history alone. But if the error says `429 rate limit exceeded`, the
        error is naming the cause and the history is only describing the shape.
        Ranking on confidence alone let the circumstantial reading win.
        """
        failure = seed_history(
            green_runs=20,
            error="stripe.error.RateLimitError: 429 Too Many Requests — rate limit exceeded",
            error_type="RateLimitError",
        )
        verdict = classify(session, settings, failure.id)
        assert verdict.category == RootCauseCategory.EXTERNAL_DEPENDENCY

    def test_cross_framework_corroboration_raises_confidence(
        self, session: Session, settings: Settings, seed_history, make_result
    ) -> None:
        """Independent test code failing identically is the strongest signal
        the system has short of reading the code."""
        from backend.db.repository import TestResultRepository

        error = "Error: connect ECONNREFUSED 10.0.4.19:9200"
        alone = classify(
            session, settings, seed_history(green_runs=5, error=error, error_type="Error").id
        )

        corroborated_failure = seed_history(
            test_name="other suite > another test",
            green_runs=5,
            error=error,
            error_type="Error",
            signature="shared-sig",
        )
        TestResultRepository(session).add(
            make_result(
                test_name="an api test",
                framework=TestFramework.PYTEST,
                status=TestStatus.FAILED,
                ci_run_id=corroborated_failure.ci_run_id,
                failure_signature="shared-sig",
                error_message=error,
            )
        )
        session.flush()
        corroborated = classify(session, settings, corroborated_failure.id)

        assert corroborated.category == RootCauseCategory.ENVIRONMENT
        assert corroborated.confidence > alone.confidence


class TestHistoryInference:
    def test_reliable_test_failing_on_a_new_commit_reads_as_an_app_bug(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        failure = seed_history(
            green_runs=20, error="AssertionError: expected 120 but received 0"
        )
        verdict = classify(session, settings, failure.id)
        assert verdict.category == RootCauseCategory.APP_BUG
        assert any("pass rate" in e for e in verdict.key_evidence)

    def test_same_commit_pass_then_fail_vetoes_app_bug(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """The decisive inference, and the reason it is a veto rather than a
        downweight: if the binary was identical in both runs, a code regression
        is ruled out by construction and no confidence score should reinstate it."""
        failure = seed_history(
            green_runs=20, error="AssertionError: expected 120 but received 0"
        )
        # Same commit as the last passing run — nothing changed.
        failure.git_commit = "aaa1111"
        session.flush()

        verdict = classify(session, settings, failure.id)
        assert verdict.category != RootCauseCategory.APP_BUG
        assert verdict.category == RootCauseCategory.FLAKY_TEST
        assert any("same commit" in e.lower() for e in verdict.key_evidence)

    def test_alternating_history_reads_as_flaky(
        self, session: Session, settings: Settings, make_result
    ) -> None:
        from datetime import timedelta

        from backend.db.repository import TestResultRepository
        from backend.utils import utcnow

        repo = TestResultRepository(session)
        now = utcnow()
        for i in range(12):
            repo.add(
                make_result(
                    test_name="wobbly",
                    status=TestStatus.PASSED if i % 2 == 0 else TestStatus.FAILED,
                    ci_run_id=f"r{i}",
                    git_commit=f"c{i}",
                    timestamp=now - timedelta(hours=13 - i),
                )
            )
        failure = repo.add(
            make_result(
                test_name="wobbly",
                status=TestStatus.FAILED,
                ci_run_id="r-final",
                git_commit="c-final",
                error_message="Timeout 30000ms exceeded waiting for locator('#x')",
                error_type="TimeoutError",
                timestamp=now,
            )
        )
        session.flush()

        verdict = classify(session, settings, failure.id)
        assert verdict.category == RootCauseCategory.FLAKY_TEST
        assert any("flakiness" in e for e in verdict.key_evidence)

    def test_retry_recovery_is_flaky_by_definition(
        self, session: Session, settings: Settings, make_result
    ) -> None:
        from backend.db.repository import TestResultRepository

        recovered = TestResultRepository(session).add(
            make_result(
                test_name="retried",
                status=TestStatus.FLAKY,
                attempt=1,
                retry_count=1,
                error_message="Timeout waiting for element",
                error_type="TimeoutError",
            )
        )
        session.flush()
        verdict = classify(session, settings, recovered.id)
        assert verdict.category == RootCauseCategory.FLAKY_TEST
        assert verdict.confidence >= 0.7


class TestHonesty:
    """The properties that make the baseline usable as a control group."""

    def test_abstains_when_nothing_matches(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """A baseline that guesses inflates its own score on easy cases and
        makes the comparison against the LLM meaningless."""
        failure = seed_history(
            green_runs=1, error="Error: something went wrong", error_type="Error"
        )
        verdict = classify(session, settings, failure.id)
        assert verdict.category == RootCauseCategory.UNKNOWN
        assert verdict.requires_human_review is True

    def test_abstention_explains_what_was_missing(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        failure = seed_history(green_runs=0, error="???", error_type="Error")
        verdict = classify(session, settings, failure.id)
        assert verdict.category == RootCauseCategory.UNKNOWN
        assert "history" in verdict.reasoning.lower()

    def test_confidence_never_exceeds_the_ceiling(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """Pattern matches are evidence, not proof — the same strings appear in
        tests that deliberately assert on them."""
        for error in (
            "No space left on device",
            "ECONNREFUSED",
            "duplicate key value violates unique constraint",
            "AssertionError: expected 1 to equal 2",
        ):
            failure = seed_history(
                test_name=f"t-{error[:10]}", green_runs=20, error=error, error_type="Error"
            )
            verdict = classify(session, settings, failure.id)
            assert verdict.confidence <= MAX_HEURISTIC_CONFIDENCE

    def test_low_confidence_is_referred_to_a_human(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        failure = seed_history(green_runs=0, error="unhelpful", error_type="Error")
        assert classify(session, settings, failure.id).requires_human_review is True

    def test_reasoning_discloses_that_it_is_rule_based(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """A reader must be able to tell a rule verdict from an LLM verdict
        without checking the database column."""
        failure = seed_history(green_runs=5, error="No space left on device")
        verdict = classify(session, settings, failure.id)
        assert RULES_VERSION in verdict.reasoning
        assert "deterministic rules" in verdict.reasoning


class TestPersistence:
    def test_writes_a_completed_analysis_row(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        failure = seed_history(green_runs=10, error="No space left on device")
        analyzer = HeuristicAnalyzer(ContextRetriever(session, settings), settings)
        analysis = analyzer.analyze(failure.id, session)

        assert analysis.status == AnalysisStatus.COMPLETED
        assert analysis.root_cause == RootCauseCategory.INFRASTRUCTURE
        assert analysis.reasoning
        assert analysis.key_evidence and analysis.suggestions

    def test_tags_rows_so_the_two_classifiers_stay_separable(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """Without a distinct prompt_version, heuristic and LLM verdicts pool
        into one accuracy number and the comparison is lost."""
        failure = seed_history(green_runs=5, error="ECONNREFUSED")
        analysis = HeuristicAnalyzer(
            ContextRetriever(session, settings), settings
        ).analyze(failure.id, session)
        assert analysis.prompt_version == RULES_VERSION
        assert analysis.model == RULES_VERSION

    def test_costs_no_tokens(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """Zero is the honest value, and it makes the cost comparison against
        the LLM fall out of the existing metrics endpoint."""
        failure = seed_history(green_runs=5, error="ECONNREFUSED")
        analysis = HeuristicAnalyzer(
            ContextRetriever(session, settings), settings
        ).analyze(failure.id, session)
        assert analysis.input_tokens == 0
        assert analysis.output_tokens == 0

    def test_unknown_test_result_raises_lookup_error(
        self, session: Session, settings: Settings
    ) -> None:
        analyzer = HeuristicAnalyzer(ContextRetriever(session, settings), settings)
        with pytest.raises(LookupError):
            analyzer.analyze("does-not-exist", session)

    def test_needs_no_api_key(self, session: Session, seed_history) -> None:
        """The whole point: classification with no key, no network, no cost."""
        offline = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            environment="ci",
            agent_enabled=True,
            analysis_mode="heuristic",
            anthropic_api_key=None,
        )
        failure = seed_history(green_runs=5, error="No space left on device")
        analysis = HeuristicAnalyzer(
            ContextRetriever(session, offline), offline
        ).analyze(failure.id, session)
        assert analysis.status == AnalysisStatus.COMPLETED


class TestModeDispatch:
    def test_ingest_to_verdict_with_no_api_key(
        self, client, fixture_bytes, settings: Settings
    ) -> None:
        """End to end through HTTP in heuristic mode — the path a user without
        an API key actually takes."""
        from backend.analysis.agent import analyze_test_result
        from backend.db.repository import FailureAnalysisRepository, TestResultRepository
        from backend.db.session import session_scope

        response = client.post(
            "/ingest/pytest",
            files={"file": ("r.json", fixture_bytes("pytest-report.json"))},
            data={"ci_run_id": "run-1", "environment": "staging"},
        )
        assert response.status_code == 200

        heuristic = settings.model_copy(
            update={"agent_enabled": True, "analysis_mode": "heuristic"}
        )
        with session_scope() as db:
            failure_id = TestResultRepository(db).list_failures(limit=1)[0].id

        analyze_test_result(failure_id, heuristic)

        with session_scope() as db:
            analysis = FailureAnalysisRepository(db).get_latest_for_test_result(failure_id)
            assert analysis is not None
            assert analysis.status == AnalysisStatus.COMPLETED
            assert analysis.prompt_version == RULES_VERSION
