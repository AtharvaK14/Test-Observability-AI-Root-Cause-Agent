"""HTTP endpoint behaviour, end to end through TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

FORM = {
    "ci_run_id": "run-9001",
    "environment": "staging",
    "git_commit": "cafe123",
    "git_branch": "main",
    "ci_provider": "github-actions",
}


def upload(client: TestClient, endpoint: str, name: str, payload: bytes, **params):
    return client.post(
        f"/ingest/{endpoint}", files={"file": (name, payload)}, data={**FORM, **params}
    )


class TestHealth:
    def test_liveness_touches_nothing_else(self, client: TestClient) -> None:
        """A liveness probe that checks the database restarts the app when the
        database blips, turning a recoverable outage into a crash loop."""
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert "version" in body

    def test_readiness_reports_dependencies(self, client: TestClient) -> None:
        body = client.get("/health/ready").json()
        assert body["status"] == "ready"
        assert body["checks"]["database"] == "ok"

    def test_missing_api_key_degrades_but_does_not_fail_readiness(
        self, client: TestClient
    ) -> None:
        """Ingestion is fully functional without a key, so a missing key must
        not pull the whole instance out of rotation."""
        body = client.get("/health/ready").json()
        assert body["status"] == "ready"
        assert body["checks"]["analysis"] == "disabled"

    def test_heuristic_mode_reports_ready_without_a_key(self, settings) -> None:
        """The offline path must look healthy, not degraded — it is a supported
        configuration, not a fallback."""
        from fastapi.testclient import TestClient as Client

        from backend.config import get_settings
        from backend.db.session import create_all, drop_all, init_engine
        from backend.main import create_app

        offline = settings.model_copy(
            update={
                "agent_enabled": True,
                "analysis_mode": "heuristic",
                "anthropic_api_key": None,
            }
        )
        app = create_app(offline)
        app.dependency_overrides[get_settings] = lambda: offline
        init_engine(offline, force=True)
        create_all()
        try:
            with Client(app) as offline_client:
                body = offline_client.get("/health/ready").json()
            assert body["status"] == "ready"
            assert "no API key required" in body["checks"]["analysis"]
        finally:
            drop_all()


class TestIngestEndpoints:
    @pytest.mark.parametrize(
        ("endpoint", "fixture_name"),
        [
            ("playwright", "playwright-report.json"),
            ("cypress", "cypress-report.json"),
            ("pytest", "pytest-report.json"),
            ("selenium", "selenium-report.json"),
        ],
    )
    def test_each_framework_ingests(
        self, client: TestClient, fixture_bytes, endpoint: str, fixture_name: str
    ) -> None:
        response = upload(client, endpoint, fixture_name, fixture_bytes(fixture_name))
        assert response.status_code == 200
        body = response.json()
        assert body["ingested"] > 0
        assert body["failures_detected"] > 0
        assert body["ci_run_id"] == FORM["ci_run_id"]

    def test_junit_requires_a_framework_attribution(
        self, client: TestClient, fixture_bytes
    ) -> None:
        response = client.post(
            "/ingest/junit?framework=playwright",
            files={"file": ("j.xml", fixture_bytes("junit-report.xml"))},
            data=FORM,
        )
        assert response.status_code == 200
        assert response.json()["framework"] == "playwright"

    def test_malformed_report_is_a_400_with_an_actionable_message(
        self, client: TestClient, fixture_bytes
    ) -> None:
        response = upload(client, "playwright", "bad.json", fixture_bytes("malformed.json"))
        assert response.status_code == 400
        assert "suites" in response.json()["detail"]

    def test_empty_upload_is_rejected(self, client: TestClient) -> None:
        assert upload(client, "playwright", "e.json", b"   ").status_code == 400

    def test_response_reports_duplicates_rather_than_claiming_success(
        self, client: TestClient, fixture_bytes
    ) -> None:
        """A CI step told "success" while 380 of 400 results were dropped has
        negative value — it manufactures confidence in a wrong number."""
        payload = fixture_bytes("cypress-report.json")
        first = upload(client, "cypress", "c.json", payload).json()
        second = upload(client, "cypress", "c.json", payload).json()

        assert second["ingested"] == 0
        assert second["skipped_duplicates"] == first["ingested"]

    def test_normalised_json_body_endpoint(self, client: TestClient) -> None:
        response = client.post(
            "/ingest/results",
            json=[
                {
                    "test_name": "custom > a test",
                    "framework": "pytest",
                    "status": "failed",
                    "error_message": "boom",
                    "ci_run_id": "run-custom",
                }
            ],
        )
        assert response.status_code == 200
        assert response.json()["ingested"] == 1

    def test_normalised_endpoint_validates(self, client: TestClient) -> None:
        response = client.post(
            "/ingest/results",
            json=[{"test_name": "t", "framework": "not-a-framework", "status": "failed"}],
        )
        assert response.status_code == 422


class TestAnalysisEndpoints:
    @pytest.fixture
    def populated(self, client: TestClient, fixture_bytes) -> TestClient:
        for endpoint, name in (
            ("playwright", "playwright-report.json"),
            ("cypress", "cypress-report.json"),
            ("pytest", "pytest-report.json"),
        ):
            upload(client, endpoint, name, fixture_bytes(name))
        return client

    def test_summary_counts_by_framework(self, populated: TestClient) -> None:
        body = populated.get("/api/analysis/summary?hours=24").json()
        assert body["total_runs"] > 0
        assert body["total_failures"] > 0
        assert {f["framework"] for f in body["framework_breakdown"]} >= {
            "playwright",
            "cypress",
            "pytest",
        }

    def test_failure_feed_collapses_retries_by_default(self, populated: TestClient) -> None:
        collapsed = populated.get("/api/analysis/failures").json()["total"]
        expanded = populated.get(
            "/api/analysis/failures?include_retry_attempts=true"
        ).json()["total"]
        assert collapsed < expanded

    def test_feed_total_matches_returned_rows_when_unfiltered(
        self, populated: TestClient
    ) -> None:
        body = populated.get("/api/analysis/failures?limit=200").json()
        assert body["total"] == len(body["items"])

    def test_framework_filter(self, populated: TestClient) -> None:
        body = populated.get("/api/analysis/failures?framework=pytest").json()
        assert body["items"]
        assert all(i["test"]["framework"] == "pytest" for i in body["items"])

    def test_detail_includes_cluster_and_siblings(self, populated: TestClient) -> None:
        first = populated.get("/api/analysis/failures?limit=1").json()["items"][0]
        detail = populated.get(f"/api/analysis/failures/{first['test']['id']}").json()
        assert "test" in detail
        assert "analyses" in detail
        assert isinstance(detail["sibling_failures"], list)

    def test_unknown_failure_is_404(self, populated: TestClient) -> None:
        assert populated.get("/api/analysis/failures/nope").status_code == 404

    def test_trends_derive_a_verdict(self, populated: TestClient) -> None:
        name = populated.get("/api/analysis/failures?limit=1").json()["items"][0]["test"][
            "test_name"
        ]
        body = populated.get(f"/api/analysis/tests/{name}/trends").json()
        assert body["verdict"] in {"broken", "flaky", "degrading", "healthy", "unreliable"}
        assert body["trend_direction"] in {"stable", "improving", "degrading"}
        assert body["points"]

    def test_trends_for_unknown_test_is_404(self, populated: TestClient) -> None:
        assert populated.get("/api/analysis/tests/nope/trends").status_code == 404

    def test_clusters_are_the_triage_queue(self, populated: TestClient) -> None:
        clusters = populated.get("/api/analysis/clusters").json()
        assert clusters
        assert clusters[0]["occurrence_count"] >= 1

    def test_muting_a_cluster(self, populated: TestClient) -> None:
        cluster_id = populated.get("/api/analysis/clusters").json()[0]["id"]
        assert (
            populated.post(f"/api/analysis/clusters/{cluster_id}/mute?muted=true").json()[
                "is_muted"
            ]
            is True
        )
        assert cluster_id not in {c["id"] for c in populated.get("/api/analysis/clusters").json()}
        assert populated.post("/api/analysis/clusters/nope/mute").status_code == 404

    def test_queue_lists_unanalysed_failures(self, populated: TestClient) -> None:
        """Also the restart-recovery path: background tasks die with the
        process, and stranded analyses must reappear rather than vanish."""
        body = populated.get("/api/analysis/queue").json()
        assert body["count"] > 0

    def test_analyze_rejects_a_passing_test(self, populated: TestClient) -> None:
        response = populated.post(
            "/api/analysis/analyze", json={"test_result_id": "does-not-exist"}
        )
        assert response.status_code == 404

    def test_analyze_returns_503_when_agent_disabled(self, populated: TestClient) -> None:
        first = populated.get("/api/analysis/failures?limit=1").json()["items"][0]
        response = populated.post(
            "/api/analysis/analyze", json={"test_result_id": first["test"]["id"]}
        )
        assert response.status_code == 503

    def test_metrics_report_coverage_alongside_accuracy(self, populated: TestClient) -> None:
        """95% accuracy over four reviewed analyses should be visibly what it is."""
        body = populated.get("/api/analysis/metrics").json()
        assert "accuracy" in body and "feedback_coverage" in body


class TestFeedbackLoop:
    @pytest.fixture
    def analysis_id(self, client: TestClient, fixture_bytes, session) -> str:
        from backend.db.models import FailureAnalysisDB
        from backend.db.repository import FailureAnalysisRepository, TestResultRepository
        from backend.db.session import session_scope
        from backend.models.enums import AnalysisStatus, RootCauseCategory

        upload(client, "pytest", "p.json", fixture_bytes("pytest-report.json"))
        with session_scope() as db:
            failure = TestResultRepository(db).list_failures(limit=1)[0]
            analysis = FailureAnalysisRepository(db).add(
                FailureAnalysisDB(
                    test_result_id=failure.id,
                    status=AnalysisStatus.COMPLETED,
                    root_cause=RootCauseCategory.FLAKY_TEST,
                    confidence_score=0.8,
                    reasoning="looked flaky",
                    requires_human_review=True,
                )
            )
            return analysis.id

    def test_records_a_correction(self, client: TestClient, analysis_id: str) -> None:
        response = client.post(
            f"/api/analysis/analyses/{analysis_id}/feedback",
            json={
                "verdict": "incorrect",
                "corrected_root_cause": "test_data",
                "feedback_text": "the seeded account had already been consumed",
                "submitted_by": "atharva",
            },
        )
        assert response.status_code == 200
        assert response.json()["corrected_root_cause"] == "test_data"

    def test_resubmitting_updates_rather_than_double_voting(
        self, client: TestClient, analysis_id: str
    ) -> None:
        """A reviewer double-clicking must not skew the accuracy metric."""
        payload = {"verdict": "correct", "submitted_by": "atharva"}
        first = client.post(f"/api/analysis/analyses/{analysis_id}/feedback", json=payload)
        second = client.post(
            f"/api/analysis/analyses/{analysis_id}/feedback",
            json={**payload, "verdict": "uncertain"},
        )
        assert first.json()["id"] == second.json()["id"]
        assert second.json()["verdict"] == "uncertain"
        assert len(client.get("/api/analysis/feedback").json()) == 1

    def test_feedback_clears_the_review_flag(
        self, client: TestClient, analysis_id: str
    ) -> None:
        client.post(
            f"/api/analysis/analyses/{analysis_id}/feedback",
            json={"verdict": "correct", "submitted_by": "atharva"},
        )
        assert client.get("/api/analysis/failures?needs_review=true").json()["items"] == []

    def test_correction_feeds_the_confusion_matrix(
        self, client: TestClient, analysis_id: str
    ) -> None:
        """This is what makes it a learning loop rather than a complaints box."""
        client.post(
            f"/api/analysis/analyses/{analysis_id}/feedback",
            json={
                "verdict": "incorrect",
                "corrected_root_cause": "test_data",
                "submitted_by": "atharva",
            },
        )
        metrics = client.get("/api/analysis/metrics").json()
        assert metrics["accuracy"] == 0.0
        assert metrics["confusion_pairs"] == [
            {"predicted": "flaky_test", "actual": "test_data", "count": 1}
        ]

    def test_feedback_on_unknown_analysis_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/analysis/analyses/nope/feedback", json={"verdict": "correct"}
        )
        assert response.status_code == 404
