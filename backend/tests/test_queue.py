"""Redis queue + worker: dispatch, processing, crash recovery, degraded modes.

Runs against fakeredis, so like the rest of the suite it needs no server. What
fakeredis cannot prove — real network behaviour, two processes racing on one
list — is exercised by the in-cluster load test instead.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from backend.analysis import dispatch
from backend.config import Settings, get_settings
from backend.db.models import FailureAnalysisDB
from backend.db.session import create_all, drop_all, get_session_factory, init_engine
from backend.worker import AnalysisWorker

FORM = {"ci_run_id": "run-q", "environment": "staging"}


@pytest.fixture
def fake_redis(monkeypatch) -> fakeredis.FakeRedis:
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(dispatch, "get_redis", lambda _url: server)
    return server


@pytest.fixture
def queued_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "redis_url": "redis://fake:6379/0",
            "agent_enabled": True,
            "auto_analyze_on_ingest": True,
            "analysis_mode": "heuristic",
        }
    )


@pytest.fixture
def queued_client(queued_settings: Settings, fake_redis) -> Iterator[TestClient]:
    from backend.main import create_app

    get_settings.cache_clear()
    app = create_app(queued_settings)
    app.dependency_overrides[get_settings] = lambda: queued_settings
    init_engine(queued_settings, force=True)
    create_all()
    with TestClient(app) as client:
        yield client
    drop_all()


def ingest(client: TestClient, fixture_bytes: Any) -> dict[str, Any]:
    response = client.post(
        "/ingest/pytest",
        files={"file": ("report.json", fixture_bytes("pytest-report.json"))},
        data=FORM,
    )
    assert response.status_code == 200, response.text
    return response.json()


def analyses_for(test_result_id: str) -> list[FailureAnalysisDB]:
    with get_session_factory()() as session:
        return list(
            session.query(FailureAnalysisDB).filter_by(test_result_id=test_result_id).all()
        )


def make_worker(fake_redis, worker_id: str = "w1", **kwargs) -> AnalysisWorker:
    get_settings.cache_clear()
    return AnalysisWorker(fake_redis, worker_id, **kwargs)


class TestDispatch:
    def test_ingest_enqueues_one_job_per_failure_instead_of_running_inline(
        self, queued_client, fake_redis, fixture_bytes
    ) -> None:
        body = ingest(queued_client, fixture_bytes)

        assert body["analyses_queued"] > 0
        assert len(body["analysis_job_ids"]) == body["analyses_queued"]
        assert fake_redis.llen(dispatch.QUEUE_KEY) == body["analyses_queued"]
        # Nothing ran in the API process: the queue is now the only consumer.
        queued_ids = [json.loads(j)["test_result_id"] for j in fake_redis.lrange(dispatch.QUEUE_KEY, 0, -1)]
        assert all(analyses_for(tid) == [] for tid in queued_ids)

    def test_job_status_is_pollable(self, queued_client, fixture_bytes) -> None:
        job_id = ingest(queued_client, fixture_bytes)["analysis_job_ids"][0]
        job = queued_client.get(f"/api/analysis/jobs/{job_id}").json()
        assert job["status"] == "queued"
        assert job["test_result_id"]

    def test_unknown_job_is_404(self, queued_client) -> None:
        assert queued_client.get(f"/api/analysis/jobs/{'0' * 32}").status_code == 404

    def test_jobs_endpoint_explains_itself_without_a_queue(self, client) -> None:
        response = client.get(f"/api/analysis/jobs/{'0' * 32}")
        assert response.status_code == 404
        assert "REDIS_URL" in response.json()["detail"]

    def test_readiness_reports_queue_depth(self, queued_client, fixture_bytes) -> None:
        queued = ingest(queued_client, fixture_bytes)["analyses_queued"]
        checks = queued_client.get("/health/ready").json()["checks"]
        assert checks["queue"] == f"ok (redis, depth {queued})"

    def test_redis_down_degrades_without_failing_the_upload(
        self, queued_settings, monkeypatch, fixture_bytes
    ) -> None:
        """The rows are already committed when enqueue fails. Returning an error
        would make CI retry an upload that is stored; instead the failures stay
        in /api/analysis/queue for recovery, and readiness says so."""
        from backend.main import create_app

        class Down:
            def pipeline(self, *a, **kw):
                raise redis.ConnectionError("refused")

            def llen(self, *_a):
                raise redis.ConnectionError("refused")

        monkeypatch.setattr(dispatch, "get_redis", lambda _url: Down())
        app = create_app(queued_settings)
        app.dependency_overrides[get_settings] = lambda: queued_settings
        init_engine(queued_settings, force=True)
        create_all()
        try:
            with TestClient(app) as client:
                body = ingest(client, fixture_bytes)
                assert body["analyses_queued"] > 0
                assert body["analysis_job_ids"] == []
                pending = client.get("/api/analysis/queue").json()["count"]
                assert pending == body["analyses_queued"]
                ready = client.get("/health/ready").json()
                assert ready["status"] == "ready"
                assert ready["checks"]["queue"].startswith("degraded")
        finally:
            drop_all()


class TestWorker:
    def test_processes_a_job_end_to_end(
        self, queued_client, queued_settings, fake_redis, fixture_bytes, monkeypatch
    ) -> None:
        monkeypatch.setattr("backend.analysis.agent.get_settings", lambda: queued_settings)
        body = ingest(queued_client, fixture_bytes)
        worker = make_worker(fake_redis)

        processed = 0
        while worker.process_one(timeout=0.01):
            processed += 1

        assert processed == body["analyses_queued"]
        assert fake_redis.llen(dispatch.QUEUE_KEY) == 0
        assert fake_redis.llen(worker.processing_key) == 0
        for job_id in body["analysis_job_ids"]:
            job = fake_redis.hgetall(dispatch.job_key(job_id))
            assert job["status"] == "done"
            assert job["worker"] == "w1"
            assert len(analyses_for(job["test_result_id"])) == 1

    def test_empty_queue_returns_false(self, fake_redis) -> None:
        assert make_worker(fake_redis).process_one(timeout=0.01) is False

    def test_crashing_analysis_marks_the_job_failed_and_releases_it(self, fake_redis) -> None:
        def explode(_test_result_id: str) -> None:
            raise RuntimeError("boom")

        [job_id] = dispatch.enqueue(fake_redis, ["tr-1"])
        worker = make_worker(fake_redis, analyze=explode)

        assert worker.process_one(timeout=0.01) is True
        assert fake_redis.hget(dispatch.job_key(job_id), "status") == "failed"
        assert fake_redis.llen(worker.processing_key) == 0

    def test_malformed_payload_is_discarded_not_retried_forever(self, fake_redis) -> None:
        fake_redis.lpush(dispatch.QUEUE_KEY, "{not json")
        worker = make_worker(fake_redis, analyze=lambda _t: None)
        assert worker.process_one(timeout=0.01) is True
        assert fake_redis.llen(worker.processing_key) == 0
        assert fake_redis.llen(dispatch.QUEUE_KEY) == 0


class TestCrashRecovery:
    """A worker killed mid-job must not lose the job: the reason the queue exists."""

    def claim_without_finishing(self, fake_redis, worker_id: str) -> str:
        """Simulate a worker that claimed a job and then died (SIGKILL, OOM)."""
        [job_id] = dispatch.enqueue(fake_redis, ["tr-orphan"])
        fake_redis.blmove(
            dispatch.QUEUE_KEY, f"{dispatch.PROCESSING_PREFIX}{worker_id}", 1, "RIGHT", "LEFT"
        )
        return job_id

    def test_dead_workers_jobs_are_requeued_and_completed(self, fake_redis) -> None:
        job_id = self.claim_without_finishing(fake_redis, "dead-pod")
        assert fake_redis.llen(dispatch.QUEUE_KEY) == 0  # stranded

        seen: list[str] = []
        survivor = make_worker(fake_redis, "live-pod", analyze=seen.append)
        survivor.heartbeat()
        assert survivor.recover_dead_workers() == 1
        assert survivor.process_one(timeout=0.01) is True

        assert seen == ["tr-orphan"]
        assert fake_redis.hget(dispatch.job_key(job_id), "status") == "done"
        assert fake_redis.llen(f"{dispatch.PROCESSING_PREFIX}dead-pod") == 0

    def test_live_workers_jobs_are_not_stolen(self, fake_redis) -> None:
        self.claim_without_finishing(fake_redis, "busy-pod")
        make_worker(fake_redis, "busy-pod").heartbeat()  # alive, mid-analysis

        survivor = make_worker(fake_redis, "other-pod")
        assert survivor.recover_dead_workers() == 0
        assert fake_redis.llen(f"{dispatch.PROCESSING_PREFIX}busy-pod") == 1

    def test_restarted_worker_reclaims_its_own_unfinished_job(self, fake_redis) -> None:
        """Container restart inside the same pod: same HOSTNAME, same list."""
        self.claim_without_finishing(fake_redis, "pod-a")
        worker = make_worker(fake_redis, "pod-a")
        assert worker.requeue(worker.processing_key) == 1
        assert fake_redis.llen(dispatch.QUEUE_KEY) == 1

    def test_worker_survives_redis_being_unavailable(self, fake_redis, monkeypatch) -> None:
        """Regression: the worker crashed when it started before Redis did."""
        seen: list[str] = []
        worker = make_worker(fake_redis, analyze=seen.append)
        dispatch.enqueue(fake_redis, ["tr-after-outage"])
        monkeypatch.setattr("backend.worker.time.sleep", lambda _s: None)

        real_heartbeat = worker.heartbeat
        outages = {"left": 2}

        def flaky_heartbeat() -> None:
            if outages["left"]:
                outages["left"] -= 1
                raise redis.ConnectionError("Connection refused")
            real_heartbeat()

        real_process = worker.process_one

        def process_then_stop(timeout: float) -> bool:
            worker.stop()
            return real_process(0.01)

        monkeypatch.setattr(worker, "heartbeat", flaky_heartbeat)
        monkeypatch.setattr(worker, "process_one", process_then_stop)
        worker.run()

        assert outages["left"] == 0
        assert seen == ["tr-after-outage"]

    def test_stop_exits_the_run_loop_and_clears_the_heartbeat(self, fake_redis, monkeypatch) -> None:
        worker = make_worker(fake_redis, analyze=lambda _t: None)
        monkeypatch.setattr("backend.worker.POLL_TIMEOUT_SECONDS", 0.01)
        calls = {"n": 0}
        real = worker.process_one

        def one_then_stop(timeout: float) -> bool:
            calls["n"] += 1
            worker.stop()
            return real(timeout)

        monkeypatch.setattr(worker, "process_one", one_then_stop)
        worker.run()
        assert calls["n"] == 1
        assert not fake_redis.exists(worker.heartbeat_key)
