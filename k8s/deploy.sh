#!/usr/bin/env bash
# Build the image, load it into the local kind cluster, and apply everything.
#
#   kind create cluster --name root-cause-agent    # once
#   ./k8s/deploy.sh
#   kubectl port-forward service/root-cause-agent 8000:8000
#
# Secrets are created here, imperatively, rather than committed as manifests.
# The Postgres password is the throwaway local-dev one docker-compose already
# uses; ANTHROPIC_API_KEY is copied from .env only if it is set there, and is
# never echoed.

set -euo pipefail

CLUSTER="${CLUSTER:-root-cause-agent}"
IMAGE="root-cause-agent:local"
PG_USER="tobs"
PG_PASSWORD="${PG_PASSWORD:-tobs}"
PG_DB="test_observability"

cd "$(dirname "$0")/.."

echo "==> building $IMAGE"
docker build -f ci/docker/Dockerfile.backend -t "$IMAGE" .

echo "==> loading $IMAGE into kind cluster '$CLUSTER'"
kind load docker-image "$IMAGE" --name "$CLUSTER"

# `create --dry-run | apply` makes every step idempotent: re-running the script
# updates objects in place instead of failing on "already exists".
apply_stdin() { kubectl apply -f -; }

echo "==> schema ConfigMap from db/schema.sql"
kubectl create configmap postgres-schema \
  --from-file=01-schema.sql=db/schema.sql \
  --dry-run=client -o yaml | apply_stdin

echo "==> secrets"
kubectl create secret generic postgres-credentials \
  --from-literal=username="$PG_USER" \
  --from-literal=password="$PG_PASSWORD" \
  --from-literal=libpq-url="postgresql://$PG_USER:$PG_PASSWORD@postgres:5432/$PG_DB" \
  --dry-run=client -o yaml | apply_stdin

agent_secret_args=(
  --from-literal=DATABASE_URL="postgresql+psycopg://$PG_USER:$PG_PASSWORD@postgres:5432/$PG_DB"
)
# Strip inline comments and whitespace the way python-dotenv would; Docker and
# kubectl --from-env-file do not, so .env cannot be passed through directly.
api_key="$(sed -n 's/^ANTHROPIC_API_KEY=\([^#[:space:]]*\).*/\1/p' .env 2>/dev/null || true)"
if [[ -n "$api_key" ]]; then
  agent_secret_args+=(--from-literal=ANTHROPIC_API_KEY="$api_key")
  echo "    ANTHROPIC_API_KEY found in .env (not shown)"
fi
kubectl create secret generic agent-secrets "${agent_secret_args[@]}" \
  --dry-run=client -o yaml | apply_stdin

echo "==> manifests"
kubectl apply -f k8s/configmap.yaml -f k8s/postgres.yaml -f k8s/redis.yaml
kubectl apply -f k8s/deployment.yaml -f k8s/service.yaml -f k8s/worker.yaml

# A changed ConfigMap/Secret or a re-loaded image with the same tag is not a
# spec change, so pods would otherwise keep running the old one.
kubectl rollout restart deployment -l tier=app

echo "==> waiting for rollout"
for d in postgres redis root-cause-agent analysis-worker; do
  kubectl rollout status "deployment/$d" --timeout=180s
done
kubectl get pods -o wide
