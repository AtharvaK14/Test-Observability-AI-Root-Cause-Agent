// Load test: CI-style report ingestion, plus a liveness probe running alongside.
//
// Measures the path the Redis queue changes. A /health-only test would measure
// uvicorn and nothing else; this one measures:
//   - ingest latency/throughput (what a CI upload step waits on)
//   - /health latency *while* ingest load runs (is the server starved?)
//   - analysis drain time after load stops (how long until every failure has a
//     verdict) — printed by teardown(), since it is a single wall-clock number
//
// Run in-cluster with loadtest/run-in-cluster.sh, so requests go through the
// Service and are balanced across replicas. `kubectl port-forward` pins one pod.

import http from 'k6/http';
import exec from 'k6/execution';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const VUS = Number(__ENV.VUS || 20);
const DURATION = __ENV.DURATION || '30s';
const RUN_TAG = __ENV.RUN_TAG || `${Date.now()}`;
const REPORT = open(__ENV.REPORT_PATH || '../backend/tests/fixtures/pytest-report.json', 'b');

export const options = {
  scenarios: {
    ingest: {
      executor: 'constant-vus',
      exec: 'ingest',
      vus: VUS,
      duration: DURATION,
    },
    health: {
      executor: 'constant-arrival-rate',
      exec: 'health',
      rate: 10,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 5,
    },
  },
  // Scoped to the measured scenarios so setup()'s warm-up is excluded. The
  // count>0 entries exist only to print per-scenario request counts.
  thresholds: {
    'http_req_failed{scenario:ingest}': ['rate<0.01'],
    'http_req_failed{scenario:health}': ['rate<0.01'],
    'http_req_duration{scenario:ingest}': ['p(95)<5000'],
    'http_req_duration{scenario:health}': ['p(95)<1000'],
    'http_reqs{scenario:ingest}': ['count>0'],
  },
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  // The drain measurement in teardown() can legitimately take minutes when the
  // workers are the bottleneck; k6's 60s default would cut it off silently.
  teardownTimeout: '330s',
};

export function setup() {
  // Warm-up, excluded from the measured scenarios: wait until the Service
  // answers 20 times in a row, so the first measured requests are not paying
  // for endpoint propagation or a cold connection pool.
  let consecutive = 0;
  for (let i = 0; i < 300 && consecutive < 20; i++) {
    const res = http.get(`${BASE_URL}/health/ready`, { tags: { name: 'warmup' } });
    consecutive = res.status === 200 ? consecutive + 1 : 0;
    if (consecutive === 0) sleep(0.5);
  }
  if (consecutive < 20) throw new Error('service never became ready');
}

export function ingest() {
  // Unique ci_run_id per iteration: the dedupe key includes it, so a repeated
  // id would turn every request after the first into a cheap "skipped" no-op.
  const ciRunId = `lt-${RUN_TAG}-${exec.scenario.iterationInTest}`;
  const res = http.post(
    `${BASE_URL}/ingest/pytest`,
    {
      file: http.file(REPORT, 'report.json', 'application/json'),
      ci_run_id: ciRunId,
      environment: 'loadtest',
    },
    { tags: { name: 'POST /ingest/pytest' } },
  );
  check(res, {
    'ingest 200': (r) => r.status === 200,
    'ingest queued analyses': (r) => r.status === 200 && r.json('analyses_queued') > 0,
  });
  sleep(0.1);
}

export function health() {
  const res = http.get(`${BASE_URL}/health`, { tags: { name: 'GET /health' } });
  check(res, { 'health 200': (r) => r.status === 200 });
}

export function teardown() {
  // Poll until no failure is left without an analysis. Measured from the end of
  // the load phase, so it is the backlog the system still owed when CI stopped.
  // With the Redis queue, /health/ready also reports the queue depth at that
  // moment — the size of the backlog the workers must still clear.
  const ready = http.get(`${BASE_URL}/health/ready`);
  const queueCheck = ready.status === 200 ? ready.json('checks.queue') : 'unavailable';
  console.log(`QUEUE_AT_LOAD_END ${queueCheck}`);

  const started = Date.now();
  const deadlineMs = 300 * 1000;
  let pending = -1;
  while (Date.now() - started < deadlineMs) {
    const res = http.get(`${BASE_URL}/api/analysis/queue?limit=1`);
    pending = res.status === 200 ? res.json('count') : -1;
    if (pending === 0) break;
    sleep(0.5);
  }
  const seconds = ((Date.now() - started) / 1000).toFixed(1);
  console.log(`ANALYSIS_DRAIN pending=${pending} seconds=${seconds}`);
}
