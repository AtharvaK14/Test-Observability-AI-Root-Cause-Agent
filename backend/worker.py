"""Analysis worker: consumes the Redis queue that the API produces to.

    REDIS_URL=redis://localhost:6379/0 python -m backend.worker

Delivery is at-least-once. A job is claimed with BLMOVE, which atomically moves
it from the shared queue into this worker's own processing list, and is removed
from there only after the analysis finishes. A worker that dies mid-job leaves
the job in its processing list, and its heartbeat key expires; any surviving
worker then moves those jobs back onto the queue. Reprocessing is harmless —
analysis appends a row rather than mutating one, and the read paths use the
latest.

Deliberately single-threaded. Scale by adding replicas: each one is an
independent consumer, and Redis hands every job to exactly one of them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Callable
from types import FrameType

import redis

from backend.analysis.agent import analyze_test_result
from backend.analysis.dispatch import (
    HEARTBEAT_PREFIX,
    PROCESSING_PREFIX,
    QUEUE_KEY,
    job_key,
)
from backend.config import Settings, get_settings
from backend.db.session import init_engine
from backend.logging_config import configure_logging
from backend.utils import utcnow

logger = logging.getLogger(__name__)

POLL_TIMEOUT_SECONDS = 5
HEARTBEAT_TTL_SECONDS = 30
RECOVERY_INTERVAL_SECONDS = 15
MAX_BACKOFF_SECONDS = 10.0


class AnalysisWorker:
    def __init__(
        self,
        client: redis.Redis,
        worker_id: str,
        analyze: Callable[[str], None] = analyze_test_result,
    ) -> None:
        self.client = client
        self.worker_id = worker_id
        self.processing_key = f"{PROCESSING_PREFIX}{worker_id}"
        self.heartbeat_key = f"{HEARTBEAT_PREFIX}{worker_id}"
        self.analyze = analyze
        self._stopping = False
        self._last_recovery = 0.0

    # --- lifecycle -----------------------------------------------------------

    def stop(self, *_: object) -> None:
        """SIGTERM handler: finish the current job, then exit.

        Kubernetes sends SIGTERM and waits terminationGracePeriodSeconds before
        SIGKILL. Exiting between jobs means a rolling deploy never interrupts an
        analysis half-way, and the poll timeout bounds how long that takes.
        """
        logger.info("worker stopping after current job", extra={"worker": self.worker_id})
        self._stopping = True

    def run(self) -> None:
        logger.info("worker started", extra={"worker": self.worker_id, "queue": QUEUE_KEY})
        own_list_checked = False
        backoff = 1.0
        while not self._stopping:
            try:
                self.heartbeat()
                if not own_list_checked:
                    # Jobs we claimed but may not have finished: left by a
                    # previous process with this id (container restart in the
                    # same pod), or by a Redis error mid-job in this one.
                    self.requeue(self.processing_key)
                    own_list_checked = True
                if time.monotonic() - self._last_recovery > RECOVERY_INTERVAL_SECONDS:
                    self.recover_dead_workers()
                self.process_one(POLL_TIMEOUT_SECONDS)
                backoff = 1.0
            except (redis.ConnectionError, redis.TimeoutError) as exc:
                # Ride out Redis restarts instead of crashing: a crash per blip
                # walks the pod into CrashLoopBackOff, whose delays outlast the
                # blip. Found when the worker started before Redis on deploy.
                logger.warning(
                    "redis unavailable; retrying",
                    extra={"error": f"{type(exc).__name__}: {exc}", "retry_in_s": backoff},
                )
                own_list_checked = False
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        # Best effort: if Redis is gone it expires within HEARTBEAT_TTL_SECONDS.
        with contextlib.suppress(redis.RedisError):
            self.client.delete(self.heartbeat_key)
        logger.info("worker stopped", extra={"worker": self.worker_id})

    # --- work ----------------------------------------------------------------

    def process_one(self, timeout: float) -> bool:
        """Claim and run at most one job. Returns False if the queue stayed empty."""
        # The stub says int, but Redis takes fractional seconds — and truncating
        # a sub-second timeout to 0 would mean "block forever".
        claimed = self.client.blmove(
            QUEUE_KEY, self.processing_key, timeout, "RIGHT", "LEFT"  # type: ignore[arg-type]
        )
        if claimed is None:
            return False
        raw = claimed if isinstance(claimed, str) else bytes(claimed).decode()

        try:
            job = json.loads(raw)
            job_id, test_result_id = job["job_id"], job["test_result_id"]
        except (ValueError, KeyError, TypeError):
            logger.error("discarding malformed job", extra={"payload": str(raw)[:200]})
            self.client.lrem(self.processing_key, 1, raw)
            return True

        self.client.hset(
            job_key(job_id),
            mapping={"status": "running", "worker": self.worker_id, "started_at": _now()},
        )
        started = time.perf_counter()
        status = "done"
        try:
            # Logs and swallows its own failures (writing a FAILED analysis row
            # where it can); this guard is for anything that escapes it.
            self.analyze(test_result_id)
        except Exception:
            status = "failed"
            logger.exception("analysis job crashed", extra={"job_id": job_id})

        pipe = self.client.pipeline(transaction=True)
        pipe.hset(
            job_key(job_id),
            mapping={
                "status": status,
                "finished_at": _now(),
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        pipe.lrem(self.processing_key, 1, raw)
        pipe.execute()
        return True

    # --- liveness and recovery -----------------------------------------------

    def heartbeat(self) -> None:
        self.client.set(self.heartbeat_key, _now(), ex=HEARTBEAT_TTL_SECONDS)

    def requeue(self, processing_key: str) -> int:
        """Move every job in a processing list back to the head of the queue.

        LMOVE is atomic per element, so two workers recovering the same dead
        list concurrently still move each job exactly once.
        """
        moved = 0
        while self.client.lmove(processing_key, QUEUE_KEY, "RIGHT", "RIGHT") is not None:
            moved += 1
        if moved:
            logger.warning(
                "requeued unfinished jobs", extra={"from": processing_key, "count": moved}
            )
        return moved

    def recover_dead_workers(self) -> int:
        """Requeue jobs claimed by workers whose heartbeat has expired."""
        self._last_recovery = time.monotonic()
        moved = 0
        for key in self.client.scan_iter(match=f"{PROCESSING_PREFIX}*"):
            worker_id = str(key)[len(PROCESSING_PREFIX):]
            if worker_id == self.worker_id:
                continue
            if not self.client.exists(f"{HEARTBEAT_PREFIX}{worker_id}"):
                moved += self.requeue(str(key))
        return moved


def _now() -> str:
    return utcnow().isoformat()


def main(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    configure_logging(settings)
    if not settings.redis_url:
        logger.error("REDIS_URL is not set; the worker has no queue to consume")
        return 2

    init_engine(settings)
    client = redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        # Must exceed the BLMOVE block, or every idle poll reads as a timeout.
        socket_timeout=POLL_TIMEOUT_SECONDS + 10,
        health_check_interval=30,
    )
    # HOSTNAME is the pod name in Kubernetes: unique per pod, and a new one per
    # replacement pod — which is why recovery is keyed on heartbeats, not names.
    worker_id = os.environ.get("HOSTNAME") or socket.gethostname()
    worker = AnalysisWorker(client, worker_id)

    def _handle(signum: int, _frame: FrameType | None) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
