"""Repository queries and the ingestion service."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from backend.db.models import FailureAnalysisDB, UserFeedbackDB
from backend.db.repository import (
    FailureAnalysisRepository,
    FeedbackRepository,
    TestResultRepository,
)
from backend.ingest.base import parse_report
from backend.ingest.service import IngestionService
from backend.models.enums import (
    AnalysisStatus,
    FeedbackVerdict,
    RootCauseCategory,
    TestFramework,
    TestStatus,
)
from backend.utils import utcnow


class TestTestResultRepository:
    def test_history_excludes_nothing_and_orders_newest_first(
        self, session: Session, seed_history
    ) -> None:
        seed_history(green_runs=5)
        history = TestResultRepository(session).get_history("checkout > completes purchase")
        assert len(history) == 6
        assert history[0].status == TestStatus.FAILED
        assert history[0].timestamp >= history[1].timestamp

    def test_last_pass_before_bounds_the_suspect_commit_range(
        self, session: Session, seed_history
    ) -> None:
        """This is what turns "the test is red" into "it broke between A and B"."""
        failure = seed_history(green_runs=3)
        last_pass = TestResultRepository(session).get_last_pass_before(
            failure.test_name, failure.timestamp
        )
        assert last_pass is not None
        assert last_pass.git_commit == "aaa1111"
        assert failure.git_commit == "bbb2222"

    def test_duration_baseline_samples_only_passing_runs(
        self, session: Session, seed_history
    ) -> None:
        """Including failures drags the baseline toward the timeout value and
        makes every failure look normal-duration."""
        seed_history(green_runs=4, duration_ms=30000)
        stats = TestResultRepository(session).get_duration_stats("checkout > completes purchase")
        assert stats["sample_size"] == 4
        assert stats["avg_duration_ms"] == 1000

    def test_ci_run_siblings_exclude_the_subject(self, session: Session, make_result) -> None:
        repo = TestResultRepository(session)
        subject = repo.add(
            make_result(test_name="a", status=TestStatus.FAILED, ci_run_id="shared")
        )
        repo.add(
            make_result(
                test_name="b",
                status=TestStatus.FAILED,
                ci_run_id="shared",
                framework=TestFramework.PYTEST,
            )
        )
        repo.add(make_result(test_name="c", status=TestStatus.PASSED, ci_run_id="shared"))

        siblings = repo.get_ci_run_siblings("shared", exclude_id=subject.id)
        assert [s.test_name for s in siblings] == ["b"], "passes are not siblings"

    def test_signature_lookup_spans_tests_and_frameworks(
        self, session: Session, make_result
    ) -> None:
        """The same signature in unrelated tests is near-proof of a shared cause."""
        repo = TestResultRepository(session)
        for name, framework in (
            ("pw test", TestFramework.PLAYWRIGHT),
            ("py test", TestFramework.PYTEST),
        ):
            repo.add(
                make_result(
                    test_name=name,
                    framework=framework,
                    status=TestStatus.FAILED,
                    failure_signature="shared-sig",
                )
            )
        assert repo.count_by_signature("shared-sig") == 2
        assert len(repo.get_distinct_tests_for_signature("shared-sig")) == 2

    def test_dedupe_key_lookup(self, session: Session, make_result) -> None:
        repo = TestResultRepository(session)
        repo.add(make_result(dedupe_key="key-1"))
        assert repo.find_existing_dedupe_keys(["key-1", "key-2"]) == {"key-1"}
        assert repo.find_existing_dedupe_keys([]) == set()

    def test_failure_count_collapses_retry_attempts_by_default(
        self, session: Session, make_result
    ) -> None:
        """A test that failed twice then passed is ONE problem, not three."""
        repo = TestResultRepository(session)
        for attempt, status in enumerate(
            [TestStatus.FAILED, TestStatus.FAILED, TestStatus.FLAKY]
        ):
            repo.add(
                make_result(
                    test_name="retried", status=status, attempt=attempt, retry_count=2
                )
            )
        assert repo.count_failures() == 1
        assert repo.count_failures(final_attempts_only=False) == 3

    def test_list_and_count_agree(self, session: Session, make_result) -> None:
        """A total that disagrees with the rows beneath it is a filter drift bug."""
        repo = TestResultRepository(session)
        for i in range(5):
            repo.add(make_result(test_name=f"t{i}", status=TestStatus.FAILED))
        assert repo.count_failures() == len(repo.list_failures(limit=100))

    def test_flakiest_ignores_undersampled_tests(self, session: Session, make_result) -> None:
        """A test that ran twice and failed once is not "50% flaky" — it is
        unmeasured, and without a floor it tops the leaderboard."""
        repo = TestResultRepository(session)
        repo.add(make_result(test_name="rare", status=TestStatus.FAILED))
        for i in range(10):
            repo.add(
                make_result(
                    test_name="common",
                    status=TestStatus.FAILED if i % 2 else TestStatus.PASSED,
                    ci_run_id=f"r{i}",
                )
            )
        ranked = repo.get_flakiest_tests(min_runs=5)
        assert [r["test_name"] for r in ranked] == ["common"]
        assert ranked[0]["failure_rate"] == pytest.approx(0.5)

    def test_unanalyzed_queue_excludes_analysed_failures(
        self, session: Session, make_result
    ) -> None:
        repo = TestResultRepository(session)
        analysed = repo.add(make_result(test_name="done", status=TestStatus.FAILED))
        repo.add(make_result(test_name="pending", status=TestStatus.FAILED))
        FailureAnalysisRepository(session).add(
            FailureAnalysisDB(
                test_result_id=analysed.id,
                status=AnalysisStatus.COMPLETED,
                root_cause=RootCauseCategory.FLAKY_TEST,
            )
        )
        assert [r.test_name for r in repo.get_unanalyzed_failures()] == ["pending"]


class TestAnalysisRepository:
    def test_accuracy_is_measured_only_over_adjudicated_analyses(
        self, session: Session, make_result
    ) -> None:
        """Averaging in unreviewed analyses assumes the agent was right about
        them — which is the thing under test."""
        runs = TestResultRepository(session)
        analyses = FailureAnalysisRepository(session)
        feedback = FeedbackRepository(session)

        for index, verdict in enumerate(
            [FeedbackVerdict.CORRECT, FeedbackVerdict.CORRECT, FeedbackVerdict.INCORRECT, None]
        ):
            run = runs.add(make_result(test_name=f"t{index}", status=TestStatus.FAILED))
            analysis = analyses.add(
                FailureAnalysisDB(
                    test_result_id=run.id,
                    status=AnalysisStatus.COMPLETED,
                    root_cause=RootCauseCategory.FLAKY_TEST,
                    confidence_score=0.8,
                )
            )
            if verdict is not None:
                feedback.add(
                    UserFeedbackDB(
                        analysis_id=analysis.id,
                        verdict=verdict,
                        corrected_root_cause=(
                            RootCauseCategory.TEST_DATA
                            if verdict == FeedbackVerdict.INCORRECT
                            else None
                        ),
                        submitted_by="reviewer",
                    )
                )

        metrics = analyses.get_accuracy_metrics()
        assert metrics["adjudicated"] == 3
        # Rounded to 4dp at the repository boundary — 0.01% resolution is more
        # than a hand-labelled accuracy figure can honestly support.
        assert metrics["accuracy"] == pytest.approx(2 / 3, abs=1e-4)
        assert metrics["total_completed_analyses"] == 4
        assert metrics["feedback_coverage"] == pytest.approx(0.75)

    def test_confusion_pairs_expose_systematic_mistakes(
        self, session: Session, make_result
    ) -> None:
        """"Wrong" teaches nothing; "you said flaky_test, it was test_data" is a
        labelled example and a ranked prompt-improvement backlog."""
        run = TestResultRepository(session).add(
            make_result(test_name="t", status=TestStatus.FAILED)
        )
        analysis = FailureAnalysisRepository(session).add(
            FailureAnalysisDB(
                test_result_id=run.id,
                status=AnalysisStatus.COMPLETED,
                root_cause=RootCauseCategory.FLAKY_TEST,
            )
        )
        FeedbackRepository(session).add(
            UserFeedbackDB(
                analysis_id=analysis.id,
                verdict=FeedbackVerdict.INCORRECT,
                corrected_root_cause=RootCauseCategory.TEST_DATA,
                submitted_by="reviewer",
            )
        )
        pairs = FailureAnalysisRepository(session).get_confusion_pairs()
        assert pairs == [{"predicted": "flaky_test", "actual": "test_data", "count": 1}]

    def test_cost_metrics_aggregate_token_spend(self, session: Session, make_result) -> None:
        run = TestResultRepository(session).add(
            make_result(test_name="t", status=TestStatus.FAILED)
        )
        FailureAnalysisRepository(session).add(
            FailureAnalysisDB(
                test_result_id=run.id,
                status=AnalysisStatus.COMPLETED,
                input_tokens=1200,
                output_tokens=300,
                latency_ms=8400,
            )
        )
        cost = FailureAnalysisRepository(session).get_cost_metrics(
            utcnow() - timedelta(days=1)
        )
        assert cost["input_tokens"] == 1200
        assert cost["avg_latency_ms"] == pytest.approx(8400)


class TestIngestionService:
    def test_stores_fingerprints_and_clusters(
        self, session: Session, fixture_bytes, metadata, settings
    ) -> None:
        report = parse_report(
            TestFramework.PLAYWRIGHT, fixture_bytes("playwright-report.json"), metadata
        )
        outcome = IngestionService(session, settings).ingest(report)

        assert outcome.stored_count == len(report.results)
        assert outcome.failures
        assert all(f.failure_signature or f.error_message is None for f in outcome.failures)
        assert outcome.distinct_problems >= 1

    def test_reingesting_the_same_file_stores_nothing(
        self, session: Session, fixture_bytes, metadata, settings
    ) -> None:
        """CI upload steps get retried. A duplicated report that doubled every
        failure count would corrupt every metric on the dashboard, invisibly."""
        service = IngestionService(session, settings)
        payload = fixture_bytes("cypress-report.json")

        first = service.ingest(parse_report(TestFramework.CYPRESS, payload, metadata))
        second = service.ingest(parse_report(TestFramework.CYPRESS, payload, metadata))

        assert first.stored_count > 0
        assert second.stored_count == 0
        assert second.skipped_duplicates == first.stored_count

    def test_intra_batch_collisions_are_dropped_not_fatal(
        self, session: Session, settings, metadata
    ) -> None:
        """A merged multi-shard report can contain the same key twice. Letting
        the batch die on a unique violation loses the whole upload."""
        from backend.ingest.base import ParsedReport
        from backend.models.test_result import TestResultCreate

        duplicate = TestResultCreate(
            test_name="same",
            framework=TestFramework.PYTEST,
            status=TestStatus.FAILED,
            error_message="boom",
            ci_run_id="run-1",
        )
        report = ParsedReport(
            framework=TestFramework.PYTEST,
            results=[duplicate, duplicate.model_copy()],
        )
        outcome = IngestionService(session, settings).ingest(report)
        assert outcome.stored_count == 1
        assert outcome.skipped_duplicates == 1
        assert any("collided" in w for w in outcome.warnings)

    def test_analysis_is_not_queued_for_intermediate_retries(
        self, session: Session, settings, fixture_bytes, metadata
    ) -> None:
        """Analysing all three attempts of one flaky test triples the spend for
        three near-identical verdicts."""
        enabled = settings.model_copy(
            update={"agent_enabled": True, "auto_analyze_on_ingest": True}
        )
        report = parse_report(
            TestFramework.PLAYWRIGHT, fixture_bytes("playwright-report.json"), metadata
        )
        outcome = IngestionService(session, enabled).ingest(report)

        queued = {r.id for r in outcome.stored if r.id in set(outcome.analysis_queue)}
        for row in outcome.stored:
            if row.id in queued:
                assert row.attempt == row.retry_count

    def test_agent_disabled_queues_nothing(
        self, session: Session, settings, fixture_bytes, metadata
    ) -> None:
        report = parse_report(
            TestFramework.PYTEST, fixture_bytes("pytest-report.json"), metadata
        )
        outcome = IngestionService(session, settings).ingest(report)
        assert outcome.failures and outcome.analysis_queue == []

    def test_muted_cluster_suppresses_analysis(
        self, session: Session, settings, metadata
    ) -> None:
        """A known issue that fires 200 times a day costs 200 API calls to
        re-derive a verdict a human already accepted."""
        from backend.ingest.base import ParsedReport
        from backend.models.test_result import TestResultCreate

        enabled = settings.model_copy(
            update={"agent_enabled": True, "auto_analyze_on_ingest": True}
        )
        service = IngestionService(session, enabled)

        def failure(run_id: str) -> ParsedReport:
            return ParsedReport(
                framework=TestFramework.PYTEST,
                results=[
                    TestResultCreate(
                        test_name="known",
                        framework=TestFramework.PYTEST,
                        status=TestStatus.FAILED,
                        error_message="ConnectionError: seed-db unreachable",
                        ci_run_id=run_id,
                    )
                ],
            )

        first = service.ingest(failure("run-a"))
        assert len(first.analysis_queue) == 1

        cluster = service.clusters.clusters.get_by_signature(
            first.failures[0].failure_signature or ""
        )
        assert cluster is not None
        cluster.is_muted = True
        session.flush()

        second = service.ingest(failure("run-b"))
        assert second.stored_count == 1
        assert second.analysis_queue == []


class TestTimezoneHandling:
    """Regression cover for the naive/aware datetime trap.

    SQLite has no timezone type, so without the UTCDateTime TypeDecorator every
    timestamp read back is naive — and comparing one to utcnow() raises. The bug
    appears only on the SQLite path, so a Postgres-only staging environment
    would never catch it.
    """

    def test_timestamps_survive_a_round_trip_as_aware(
        self, session: Session, make_result
    ) -> None:
        repo = TestResultRepository(session)
        stored = repo.add(make_result(test_name="tz", timestamp=utcnow()))
        session.expire_all()

        reloaded = repo.get(stored.id)
        assert reloaded is not None
        assert reloaded.timestamp.tzinfo is not None
        # The operation that used to raise TypeError.
        assert reloaded.timestamp <= utcnow()

    def test_cluster_last_seen_can_be_compared_after_reload(
        self, session: Session, settings, make_result
    ) -> None:
        from backend.analysis.clustering import ClusterService

        service = ClusterService(session)
        first = TestResultRepository(session).add(
            make_result(
                test_name="a",
                status=TestStatus.FAILED,
                failure_signature="sig-1",
                timestamp=utcnow() - timedelta(hours=1),
            )
        )
        service.upsert_for_result(first)
        session.flush()
        session.expire_all()

        second = TestResultRepository(session).add(
            make_result(
                test_name="b",
                status=TestStatus.FAILED,
                failure_signature="sig-1",
                timestamp=utcnow(),
            )
        )
        cluster = service.upsert_for_result(second)
        assert cluster is not None
        assert cluster.occurrence_count == 2
        assert cluster.last_seen == second.timestamp
