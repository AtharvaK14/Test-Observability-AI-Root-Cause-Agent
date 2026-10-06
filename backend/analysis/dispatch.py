"""Where analysis work goes once ingestion has committed: a Redis queue, or in-process.

With ``REDIS_URL`` set, each failure becomes a job on a Redis list, consumed by
``python -m backend.worker`` running as its own process (its own Deployment in
Kubernetes). Without it, analysis runs as a FastAPI background task in the API
process — the zero-infrastructure path that local development and the test
suite use.

Why the queue exists. In-process background tasks:
  * die with the process — a deploy or crash mid-batch drops queued analyses;
  * share the API's threadpool and CPU, so a burst of slow analyses (Claude
    mode: tens of seconds each) competes with the requests CI is waiting on;
  * scale only by scaling the API.
A queue makes the work durable, isolates it, and lets workers scale on their own.

Redis layout (all keys prefixed ``analysis:``):
  queue                  LIST  pending job payloads; producers LPUSH, workers BLMOVE from the right
  processing:<worker>    LIST  jobs a worker has claimed but not finished
  worker:<worker>        STR   heartbeat with a TTL; absent ⇒ that worker is dead
  job:<job_id>           HASH  status for polling: queued → running → done | failed
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

import redis
from fastapi import BackgroundTasks

from backend.analysis.agent import analyze_test_result
from backend.config import Settings
from backend.utils import utcnow

logger = logging.getLogger(__name__)

QUEUE_KEY = "analysis:queue"
PROCESSING_PREFIX = "analysis:processing:"
HEARTBEAT_PREFIX = "analysis:worker:"
JOB_PREFIX = "analysis:job:"
JOB_TTL_SECONDS = 24 * 3600


def job_key(job_id: str) -> str:
    return f"{JOB_PREFIX}{job_id}"


@lru_cache(maxsize=4)
def get_redis(url: str) -> redis.Redis:
    """Process-wide client for the API side (it pools connections internally).

    Short timeouts: an enqueue is a sub-millisecond operation, and a Redis that
    is not answering must not hold a CI upload open for the default forever.
    """
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
    )


def enqueue(client: redis.Redis, test_result_ids: Sequence[str]) -> list[str]:
    """Push one job per failure in a single round trip. Returns the job IDs.

    MULTI/EXEC, so a job's status hash and its queue entry appear together — a
    worker can never pop a job whose status record does not exist yet.
    """
    now = utcnow().isoformat()
    jobs = [
        {"job_id": uuid.uuid4().hex, "test_result_id": tid, "enqueued_at": now}
        for tid in test_result_ids
    ]
    pipe = client.pipeline(transaction=True)
    for job in jobs:
        key = job_key(job["job_id"])
        pipe.hset(
            key,
            mapping={
                "status": "queued",
                "test_result_id": job["test_result_id"],
                "enqueued_at": now,
            },
        )
        pipe.expire(key, JOB_TTL_SECONDS)
        pipe.lpush(QUEUE_KEY, json.dumps(job))
    pipe.execute()
    return [job["job_id"] for job in jobs]


def dispatch_analyses(
    test_result_ids: Sequence[str],
    background: BackgroundTasks,
    settings: Settings,
) -> list[str]:
    """Hand failures to whichever analysis backend is configured.

    Call only after the rows are committed — the consumer opens its own
    connection and must be able to see them.

    Returns job IDs to poll at ``/api/analysis/jobs/{id}``; empty when running
    in-process, since background tasks have no observable identity.
    """
    if not test_result_ids:
        return []

    if not settings.redis_url:
        for test_result_id in test_result_ids:
            background.add_task(analyze_test_result, test_result_id)
        return []

    try:
        job_ids = enqueue(get_redis(settings.redis_url), test_result_ids)
    except redis.RedisError as exc:
        # Ingestion has already committed; failing the upload now would make CI
        # retry a report that is stored. The failures stay visible in
        # /api/analysis/queue and scripts/analyze_pending.py drains them.
        logger.error(
            "could not enqueue analyses; failures remain in /api/analysis/queue",
            extra={"count": len(test_result_ids), "error": f"{type(exc).__name__}: {exc}"},
        )
        return []

    logger.info("enqueued analyses", extra={"count": len(job_ids)})
    return job_ids


def get_job(client: redis.Redis, job_id: str) -> dict[str, Any] | None:
    """Status record for one job, or None if unknown or expired."""
    record: dict[str, Any] = client.hgetall(job_key(job_id))  # type: ignore[assignment]
    return record or None


def queue_depth(client: redis.Redis) -> int:
    return int(client.llen(QUEUE_KEY))
