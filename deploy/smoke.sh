#!/usr/bin/env bash
# Smoke-test a deployed instance. Exits non-zero on the first failed check.
#
#   BASE_URL=https://tobs-api-v1.ecs.us-east-1.on.aws API_KEY=... deploy/smoke.sh
#
# With no API_KEY in the environment it reads /tobs/prod/API_KEY from SSM
# (needs the tobs profile). The key is never printed. Retries the health check
# for up to about 10 minutes, since a fresh ALB and task take a while.

set -euo pipefail

BASE_URL="${BASE_URL:-${1:-}}"
[ -n "$BASE_URL" ] || { echo "usage: BASE_URL=https://... $0" >&2; exit 2; }
BASE_URL="${BASE_URL%/}"
case "$BASE_URL" in http*://*) ;; *) BASE_URL="https://$BASE_URL" ;; esac

if [ -z "${API_KEY:-}" ]; then
  API_KEY="$(aws ssm get-parameter --profile "${AWS_PROFILE:-tobs}" --name /tobs/prod/API_KEY \
    --with-decryption --query Parameter.Value --output text)"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE="$REPO_ROOT/backend/tests/fixtures/pytest-report.json"
RUN_ID="smoke-$(date -u +%Y%m%dT%H%M%SZ)"
COMMIT="${GIT_COMMIT:-$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)}"

pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*" >&2; exit 1; }

status_of() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"; }

expect() {  # expected-status, description, curl args...
  local want="$1" what="$2" got
  shift 2
  got="$(status_of "$@")"
  if [ "$got" = "$want" ]; then pass "$what ($got)"; else fail "$what: expected $want, got $got"; fi
}

start=$(date +%s)
delay=5
until [ "$(status_of "$BASE_URL/health")" = 200 ]; do
  [ $(( $(date +%s) - start )) -lt 600 ] || fail "/health never returned 200 within 10 minutes"
  sleep "$delay"
  delay=$(( delay < 60 ? delay * 2 : 60 ))
done
pass "/health (ready after $(( $(date +%s) - start ))s)"

ready="$(curl -fsS --max-time 20 "$BASE_URL/health/ready")" || fail "/health/ready did not return 200"
case "$ready" in
  *'"database":"ok"'*) pass "/health/ready reports database ok" ;;
  *) fail "/health/ready: $ready" ;;
esac
case "$ready" in
  *postgresql*|*password*|*"$API_KEY"*) fail "/health/ready body leaks configuration" ;;
esac

expect 401 "unauthenticated summary is rejected" "$BASE_URL/api/analysis/summary"
expect 401 "wrong key is rejected" -H "X-API-Key: wrong" "$BASE_URL/api/analysis/summary"
expect 404 "/docs is off" "$BASE_URL/docs"
expect 404 "/openapi.json is off" "$BASE_URL/openapi.json"

ingest="$(curl -fsS --max-time 60 -H "X-API-Key: $API_KEY" \
  -F "file=@$FIXTURE" -F "ci_run_id=$RUN_ID" -F "environment=staging" \
  -F "git_commit=$COMMIT" -F "git_branch=main" -F "ci_provider=smoke" \
  "$BASE_URL/ingest/pytest")" || fail "authenticated ingest of pytest-report.json"
pass "authenticated ingest: $ingest"

summary="$(curl -fsS --max-time 20 -H "X-API-Key: $API_KEY" "$BASE_URL/api/analysis/summary")" \
  || fail "authenticated summary"
pass "authenticated summary: $summary"

echo "all smoke checks passed against $BASE_URL"
