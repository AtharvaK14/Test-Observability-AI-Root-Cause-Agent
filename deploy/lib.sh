#!/usr/bin/env bash
# shellcheck disable=SC2034  # names defined here are used by the scripts that source this
# Shared settings and helpers for up.sh, down.sh and smoke.sh. Sourced, not run.
#
# Every name the stack uses is defined here once, so down.sh can find what
# up.sh made even when deploy/.state/ids.env is gone.

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-tobs}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PAGER=""

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEPLOY_DIR/.." && pwd)"
STATE_DIR="$DEPLOY_DIR/.state"
STATE_FILE="$STATE_DIR/ids.env"

PROJECT=tobs
TEARDOWN_BY=2026-10-31

CLUSTER=tobs
SERVICE=tobs-api-v1
ECR_REPO=tobs-api
LOG_GROUP=/tobs/api
DB_ID=tobs-db
DB_NAME=test_observability
DB_USER=tobs
DB_CLASS="${DB_CLASS:-db.t4g.micro}"   # U2: db.t3.micro if t4g is not orderable
RDS_SG_NAME=tobs-rds-sg
SSM_PREFIX=/tobs/prod
ROLE_EXEC=tobs-ecs-execution
ROLE_INFRA=tobs-ecs-infrastructure
ROLE_TASK=tobs-ecs-task
ROLE_DEPLOY=tobs-github-deploy
GITHUB_REPO="${GITHUB_REPO:-AtharvaK14/Test-Observability-AI-Root-Cause-Agent}"
OIDC_HOST=token.actions.githubusercontent.com

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
  done
}

account_id() { aws sts get-caller-identity --query Account --output text; }

# The bucket name embeds the account and region, so it is derived, not fixed.
report_bucket() { printf 'tobs-reports-%s-%s' "$1" "$AWS_REGION"; }

state_get() {
  [ -f "$STATE_FILE" ] || return 0
  grep -E "^$1=" "$STATE_FILE" | tail -n 1 | cut -d= -f2- || true
}

state_set() {
  mkdir -p "$STATE_DIR"
  touch "$STATE_FILE"
  local tmp
  tmp="$(mktemp)"
  grep -vE "^$1=" "$STATE_FILE" > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  mv "$tmp" "$STATE_FILE"
}

# Refuse to act on an account other than the one the owner named, or the one
# the state file was written for. A wrong profile must never create or delete.
check_account() {
  local actual expected
  actual="$(account_id)" || die "cannot call STS with profile $AWS_PROFILE (run: aws sso login --profile $AWS_PROFILE)"
  expected="${EXPECTED_ACCOUNT_ID:-$(state_get ACCOUNT_ID)}"
  [ -n "$expected" ] || die "set EXPECTED_ACCOUNT_ID to the account you mean to use"
  [ "$actual" = "$expected" ] || die "profile $AWS_PROFILE is account $actual, expected $expected"
  printf '%s' "$actual"
}

# Tag shapes differ by service; these keep the two values in one place.
tags_kv() { printf 'Key=project,Value=%s Key=teardown-by,Value=%s' "$PROJECT" "$TEARDOWN_BY"; }
tags_kv_lower() { printf 'key=project,value=%s key=teardown-by,value=%s' "$PROJECT" "$TEARDOWN_BY"; }
