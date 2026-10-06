#!/usr/bin/env bash
# Run loadtest/ingest.js as a Job inside the kind cluster.
#
#   ./loadtest/run-in-cluster.sh            # 20 VUs, 30s
#   VUS=50 DURATION=60s ./loadtest/run-in-cluster.sh
#
# In-cluster on purpose: requests go to the root-cause-agent Service and are
# balanced across every replica. `kubectl port-forward` tunnels to ONE pod, so a
# load test through it never exercises horizontal scaling at all.
#
# Each run starts from an empty database (Postgres uses an emptyDir, so
# restarting it wipes the data) — otherwise rows left over from the previous run
# make the two runs incomparable.

set -euo pipefail
cd "$(dirname "$0")/.."

VUS="${VUS:-20}"
WORKERS="${WORKERS:-}"   # analysis-worker replicas for this run; unset = leave as is
DURATION="${DURATION:-30s}"
K6_IMAGE="${K6_IMAGE:-grafana/k6:2.2.0}"
RUN_TAG="$(date +%s)"

echo "==> resetting database"
kubectl rollout restart deployment/postgres
kubectl rollout status deployment/postgres --timeout=180s
if kubectl get deployment redis >/dev/null 2>&1; then
  kubectl rollout restart deployment/redis
  kubectl rollout status deployment/redis --timeout=180s
fi
# Fresh pools against the fresh database; also restarts any workers so they
# start with an empty processing list.
if [[ -n "$WORKERS" ]]; then
  kubectl scale deployment/analysis-worker --replicas="$WORKERS"
fi
kubectl rollout restart deployment -l tier=app
kubectl rollout status deployment/root-cause-agent --timeout=180s
if kubectl get deployment analysis-worker >/dev/null 2>&1; then
  kubectl rollout status deployment/analysis-worker --timeout=180s
fi

# `rollout status` returns while the old pods are still Terminating, and for a
# few seconds after that kube-proxy can still route new connections to their
# IPs. The first requests then sit in a dead TCP dial for k6's full 30s timeout
# and are reported as failures that have nothing to do with the app. Wait the
# old pods out completely.
echo "==> waiting for old pods to terminate"
for _ in $(seq 1 90); do
  if ! kubectl get pods --no-headers | grep -q Terminating; then break; fi
  sleep 2
done
sleep 5

echo "==> load-test script ConfigMap"
kubectl create configmap loadtest-script \
  --from-file=ingest.js=loadtest/ingest.js \
  --from-file=pytest-report.json=backend/tests/fixtures/pytest-report.json \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl delete job k6-loadtest --ignore-not-found --wait=true

echo "==> running k6 ($VUS VUs, $DURATION) in-cluster"
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: k6-loadtest
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: k6
          image: ${K6_IMAGE}
          args: ["run", "--quiet", "/scripts/ingest.js"]
          env:
            - { name: BASE_URL, value: "http://root-cause-agent:8000" }
            - { name: VUS, value: "${VUS}" }
            - { name: DURATION, value: "${DURATION}" }
            - { name: RUN_TAG, value: "${RUN_TAG}" }
            - { name: REPORT_PATH, value: "/scripts/pytest-report.json" }
          volumeMounts:
            - { name: scripts, mountPath: /scripts }
      volumes:
        - name: scripts
          configMap: { name: loadtest-script }
EOF

# Poll rather than `kubectl wait`: a failed threshold makes k6 exit non-zero,
# which fails the Job — that is a result to read, not an error to wait out.
for _ in $(seq 1 400); do
  state="$(kubectl get job k6-loadtest -o jsonpath='{.status.succeeded}{.status.failed}')"
  [[ -n "$state" ]] && break
  sleep 2
done
kubectl logs job/k6-loadtest

# Correctness under load, not just latency: every cluster's counter must equal
# the rows actually carrying its signature. A lost-update bug shows up here as a
# non-zero gap while every HTTP request still reports 200.
echo "==> data integrity: cluster counters vs. stored rows"
kubectl exec deploy/postgres -- psql -U tobs -d test_observability -At -F ' ' -c "
  SELECT 'clusters=' || count(*),
         'counted=' || coalesce(sum(c.occurrence_count), 0),
         'actual=' || coalesce(sum(r.n), 0),
         'lost_updates=' || coalesce(sum(r.n - c.occurrence_count), 0)
  FROM failure_clusters c
  JOIN (SELECT failure_signature, count(*) AS n FROM test_runs
        WHERE failure_signature IS NOT NULL GROUP BY failure_signature) r
    ON r.failure_signature = c.pattern_signature;"
kubectl exec deploy/postgres -- psql -U tobs -d test_observability -At -c "
  SELECT 'failures=' || count(*) FILTER (WHERE status IN ('failed','flaky','error'))
  FROM test_runs;"
kubectl exec deploy/postgres -- psql -U tobs -d test_observability -At -c "
  SELECT 'analyses_completed=' || count(*) FROM failure_analyses WHERE status = 'completed';"
