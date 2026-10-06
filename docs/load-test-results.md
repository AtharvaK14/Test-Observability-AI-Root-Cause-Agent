# Load test results: Kubernetes (kind), before and after the Redis queue

**Date:** 2026-10-06 · **Where:** a single local machine. No cloud, no managed
services. Everything below can be reproduced with the commands in
[How to reproduce](#how-to-reproduce).

## Summary

| | Before: in-process analysis | After: Redis queue, 4 workers |
|---|---|---|
| Ingest throughput (20 VUs, 30 s) | **54–64 req/s** | **161–171 req/s** |
| Ingest latency p50 / p95 | 250–257 ms / 373–594 ms | 13–20 ms / 25–47 ms |
| `/health` p95 under load | 7.7–11.7 ms | 2.9–3.6 ms |
| Failed requests | 0 | 0 |
| Backlog when load stopped | none (analysed inline) | 4,952 jobs |
| Time until every failure was analysed | ≈ 30 s (end of load) | 51.3 s (30 s load + 21.3 s drain) |
| Failures analysed / ingested | 100% | 100% |
| Cluster counter drift (lost updates) | 0 | 0 |

The queue does not make analysis faster. It **takes analysis off the request
path.** The API accepted 2.5–3.2× more CI uploads per second, with latency
an order of magnitude lower. Analysis now runs behind ingestion as a backlog, and the
worker count sets how quickly that backlog clears (next section).

## Scaling the worker

Same 30 s load against the same 2 API replicas, changing only
`analysis-worker` replicas. Drain throughput = backlog ÷ drain time.

| Workers | Ingests | Ingest p95 | Backlog at end of load | Drain time | Drain throughput | Scaling |
|---|---|---|---|---|---|---|
| 1 | 4,990 | 47.3 ms | 12,712 | 215.4 s | 59 jobs/s | 1.0× |
| 2 | 5,144 | 27.8 ms | 10,281 | 86.0 s | 120 jobs/s | 2.0× |
| 4 | 4,823 | 46.9 ms | 4,952 | 21.3 s | 232 jobs/s | 3.9× |

Drain throughput scales almost linearly with workers, which is what you want
from independent consumers on one queue. Ingest latency stays flat whatever the
worker count, because the API never waits for a worker.

## Failure behaviour

| Scenario | What was done | Result |
|---|---|---|
| Worker crash holding claimed jobs | Mid-load, 25 live jobs were `LMOVE`d into the processing list of a "ghost" worker that never heartbeats. This is the exact Redis state a SIGKILL/OOM mid-job leaves behind. | A surviving worker requeued all 25 within **13 s** (recovery interval 15 s). 15,411 / 15,411 failures analysed. |
| Worker pod deleted mid-load | `kubectl delete pod --grace-period=0 --force` on 1 of 2 workers | The worker's SIGTERM handler finished its in-flight job and exited cleanly. Nothing was stranded. 15,540 / 15,540 analysed. |
| Worker starts before Redis | Observed on first deploy: the worker crashed once on `Connection refused` | Fixed. The worker now retries with backoff instead of crashing (`test_worker_survives_redis_being_unavailable`). |

## Raw runs

All runs use 2 API replicas, 20 ingest VUs plus 10 req/s to `/health` for 30 s,
heuristic analysis mode, and start from an empty database.

**Before (in-process `BackgroundTasks`), 4 runs:**

| Run | Ingests | p50 | p95 | p99 | health p95 | Failures | Analysed | Lost updates |
|---|---|---|---|---|---|---|---|---|
| 1 | 1,852 | 252.3 ms | 381.7 ms | 443.1 ms | 7.7 ms | 5,556 | 5,556 | 0 |
| 2 | 1,914 | 250.6 ms | 388.9 ms | 445.1 ms | 10.3 ms | 5,742 | 5,742 | 0 |
| 3 | 1,859 | 254.8 ms | 373.2 ms | 440.6 ms | 11.7 ms | 5,577 | 5,577 | 0 |
| 4 ‡ | 1,621 | 257.2 ms | 594.4 ms | 764.4 ms | 11.0 ms | 4,863 | 4,863 | 0 |

‡ Run 4 was made on the final code, after the queue work, using the
reproduction steps below. The in-process code path is call-for-call the same
as in runs 1–3, so the slower tail is most likely run-to-run variance on a
shared machine. It is reported, not dropped.

**After (Redis queue):**

| Run | Workers | Ingests | p50 | p95 | p99 | Failures | Analysed | Lost updates |
|---|---|---|---|---|---|---|---|---|
| a | 1 | 5,269 | 11.5 ms | 21.4 ms | 66.8 ms | 15,807 | † | 0 |
| b | 1 | 5,236 | 11.8 ms | 23.9 ms | 74.7 ms | 15,708 | † | 0 |
| c | 1 | 5,084 | 12.7 ms | 45.8 ms | 103.7 ms | 15,252 | † | 0 |
| d | 1 | 4,990 | 14.8 ms | 47.3 ms | 100.4 ms | 14,970 | 14,970 | 0 |
| e | 2 | 5,144 | 13.3 ms | 27.8 ms | 84.9 ms | 15,432 | 15,432 | 0 |
| f | 4 | 4,823 | 19.8 ms | 46.9 ms | 111.1 ms | 14,469 | 14,469 | 0 |
| g (worker killed) | 2 | 5,180 | 13.0 ms | 24.9 ms | 95.1 ms | 15,540 | 15,540 | 0 |

† Runs a–c ran before the k6 `teardownTimeout` was raised. k6's 60 s default
cut off the drain measurement, so they count only for ingest-side numbers.

## Bugs the load test found (all fixed, all with regression tests)

The unit tests run on in-memory SQLite, and all three of these passed there.
Each one only showed up against real PostgreSQL under concurrent load:

1. **Analysis ran before the ingest transaction committed.** FastAPI runs the
   `get_db` dependency's teardown, where the commit happened, *after* background
   tasks. So against Postgres, every auto-analysis logged "analysis target
   missing", and auto-analysis on ingest never worked, docker-compose included.
   SQLite's `StaticPool` shares one connection, which made the uncommitted rows
   visible and hid the bug. Fix: commit before dispatch.
   Test: `TestIngestCommitsBeforeDispatch`.
2. **Concurrent ingests creating the same new cluster returned a 500.** Two
   check-then-insert requests collided on the unique index. Fix: a SAVEPOINT
   around the INSERT; the losing request joins the existing cluster.
   Test: `TestConcurrentClusterCreation`.
3. **Cluster counters silently lost about 55% of increments.**
   `occurrence_count` was read, incremented in Python, and written back. After
   one run, the counters read 923 / 760 / 827 where 1,935 rows each actually
   existed, and every request had returned 200. Fix: `SELECT … FOR UPDATE` on
   the cluster row, with clusters upserted in signature order so concurrent
   ingests always lock in the same order and cannot deadlock.
   Test: `TestClusterLocking`.

Every run above ends with a database check that compares each cluster's counter
to the actual rows carrying its signature (`lost_updates=0`). Latency numbers
from a system that corrupts its own data aren't worth reporting.

## Caveats: what these numbers are and are not

- **One machine.** The single-node kind cluster, the k6 load generator, and
  every component share the same 32 CPUs and the 16 GB allocated to Docker.
  These figures are a relative before/after comparison, not production capacity.
- **Heuristic analysis mode.** Rule-based classification takes about 13 ms per
  job, with no API calls and no cost. In Claude mode an analysis takes tens of
  seconds, so the case for taking it off the request path is much stronger.
  That mode was **not** load-tested, deliberately: it would spend money.
- **Worst-case lock contention.** Every request uploads the same fixture, so
  every request hits the same 3 clusters. Real CI traffic spreads across many
  more signatures. The "before" ingest p95 rose from about 116 ms to about
  380 ms once fix 3 serialised those 3 hot rows. That cost is the price of
  correct counters, and it is reported here rather than hidden. The runs before
  that fix are excluded, because their counters were wrong.
- **Why ingest got faster with the queue:** measured, the API stopped running
  analysis in its own process. The *mechanism* is inferred, not profiled. Most
  likely, in-process analyses competed with requests for the API's threadpool,
  CPU, and connection pool, and held cluster-row locks that ingest also needed.
- **Throughput is computed over the 30 s load window** (ingests ÷ 30). k6's own
  `http_reqs` rate divides by the full run, including the drain phase.

## Evidence the queue does real work

From `kubectl exec deploy/redis -- redis-cli monitor` during one ingest: the API
pod (`10.244.0.42`) produces and the worker pod (`10.244.0.41`) consumes. Payloads
are truncated here.

```
[10.244.0.42] "MULTI"
[10.244.0.42] "HSET" "analysis:job:ad8a3c45…" "status" "queued" "test_result_id" "91f6bd6b-…"
[10.244.0.42] "EXPIRE" "analysis:job:ad8a3c45…" "86400"
[10.244.0.42] "LPUSH" "analysis:queue" "{\"job_id\": \"ad8a3c45…\", …}"
              … same for the other 2 failures …
[10.244.0.42] "EXEC"
[10.244.0.41] "HSET" "analysis:job:ad8a3c45…" "status" "running" "worker" "analysis-worker-678d47fb6b-8vkms"
[10.244.0.41] "MULTI"
[10.244.0.41] "HSET" "analysis:job:ad8a3c45…" "status" "done" "duration_ms" "99.1"
[10.244.0.41] "LREM" "analysis:processing:analysis-worker-678d47fb6b-8vkms" "1" "{…}"
[10.244.0.41] "EXEC"
[10.244.0.41] "BLMOVE" "analysis:queue" "analysis:processing:analysis-worker-…" "RIGHT" "LEFT" "5"
```

## Environment

| | |
|---|---|
| Host | Windows 11 Home, 32 logical CPUs; Docker Desktop VM: 16 GB |
| Docker | 29.8.2 |
| kind / Kubernetes | kind v0.33.0 / node v1.37.0, single node |
| k6 | 2.2.0 (`grafana/k6:2.2.0`, run as an in-cluster Job) |
| API | 2 replicas, `python:3.12-slim`, uvicorn, 1 process each |
| Data | `postgres:16-alpine`, `redis:7-alpine`, both 1 replica on emptyDir |

## How to reproduce

```bash
kind create cluster --name root-cause-agent
./k8s/deploy.sh

# "After" — Redis queue, choose worker count:
WORKERS=1 ./loadtest/run-in-cluster.sh
WORKERS=2 ./loadtest/run-in-cluster.sh
WORKERS=4 ./loadtest/run-in-cluster.sh

# "Before" — in-process analysis: drop REDIS_URL from the live ConfigMap and
# run with no workers. The runner restarts the API so it picks this up.
kubectl patch configmap agent-config --type=json   -p='[{"op":"remove","path":"/data/REDIS_URL"}]'
WORKERS=0 ./loadtest/run-in-cluster.sh

# Back to queue mode:
kubectl apply -f k8s/configmap.yaml
WORKERS=1 ./loadtest/run-in-cluster.sh
```

Each run resets Postgres and Redis, waits for the old pods to terminate, warms
up, runs k6 from inside the cluster, and prints the k6 summary, the drain time,
and the counter-integrity check. In-cluster, because `kubectl port-forward`
tunnels to a single pod and would never exercise the second replica.

Tear everything down with `kind delete cluster --name root-cause-agent`.
