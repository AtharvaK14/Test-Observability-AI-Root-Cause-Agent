# AWS deploy notes

Running log for the AWS deployment described in `AWS_DEPLOY_INSTRUCTIONS.md`.
Append as you go; newest entries at the bottom of each section. No secret
values here, ever. Account IDs are written as `ACCOUNT`.

## Verified facts (date, source, command)

- 2026-10-19, local: baseline on `main` (7dbf0f7) with Python 3.12.10 in a
  fresh venv: `ruff check backend/ scripts/ evals/` clean, `mypy` clean
  (52 source files, strict), `pytest` 210 passed (186 in `backend/tests`, 24 in
  `evals/root_cause/tests`), `dump_schema.py` matches `db/schema.sql`. The
  README's "186 tests" was the backend-only count. After Phase A: 241 (217 +
  24); README updated.
- 2026-10-19, local: `scripts/score_classifier.py` (heuristic) on
  `seed_demo.py --reset` data: 10/10 correct. The script itself warns this is
  not a quotable figure (rules and seeded scenarios share an author). The
  README's "82% vs 54%" sentence is a hypothetical example, now labelled so.
- 2026-10-19, repo: `db/migrations/versions/` holds only `.gitkeep`; there is
  no Alembic revision. Prod startup skips `create_all()`, so an empty RDS
  would 500 on every request without Change 3.
- 2026-10-19, local machine: AWS CLI, Docker, jq and shellcheck are NOT
  installed. `gh` 2.102.0 is logged in as `AtharvaK14`. Git remote is
  `AtharvaK14/Test-Observability-AI-Root-Cause-Agent` (capitalization as
  GitHub reports it; use exactly this in the OIDC `sub`).
- 2026-10-19, GitHub API: latest releases and pinned commit SHAs (annotated
  tags dereferenced): actions/checkout v7.0.1 3d3c42e5; configure-aws-credentials
  v6.3.0 e1253824; amazon-ecr-login v2.1.7 03f1aad4; setup-buildx-action v4.4.1
  f87e5991; build-push-action v7.4.0 c3c9e263; amazon-ecs-deploy-express-service
  v1.2.2 7c48a2de; download-artifact v8.0.2 9000827c. The instructions expected
  configure-aws-credentials v5 and build-push-action v6; newer majors chosen.
- 2026-10-19, `aws-actions/amazon-ecs-deploy-express-service` action.yml at
  v1.2.2 (U9): `environment-variables` and `secrets` are JSON arrays
  (`[{"name":..,"valueFrom":..}]`); `tags` uses lowercase `key`/`value`;
  outputs `service-arn` and `endpoint`. Health check default `/ping`, CPU
  default 256, memory default 512 MiB. README's deploy-role permission list
  matches section 6.1 exactly.
- 2026-10-19, AWS docs "Create your first Express Mode service using the AWS
  CLI": infrastructure role trust principal is `ecs.amazonaws.com` (Sid
  `AllowAccessInfrastructureForECSExpressServices`); describe and monitor take
  `--service-arn`; URL format `https://<service>.ecs.<region>.on.aws/`; a task
  definition supplied instead must name its container `Main`; IAM roles may
  need about a minute to propagate before the first create.
- 2026-10-19, same page vs action/CLI reference: CONFLICT on CPU/memory units.
  Tutorial shows `--cpu 2 --memory 4` (vCPU/GB), the action documents CPU units
  and MiB (256..16384 / 512..122880). Scripts use 512/1024; VERIFY with
  `aws ecs create-express-gateway-service help` before the first create.
- 2026-10-19, local: `shellcheck` 0.11.0 (via pip `shellcheck-py`) clean on
  deploy/*.sh; `actionlint` clean on all workflows; `down.sh` dry run against
  a stubbed `aws` lists the expected deletes and refuses a mismatched account.

## Deviations from AWS_DEPLOY_INSTRUCTIONS.md

- No Docker locally: image bootstrap must use section 5.6 option B (CI
  build-and-push via `workflow_dispatch`) unless Docker Desktop is installed.
  The local container check in section 4 is replaced by the CI `docker` job,
  which already builds the image and imports the app.

- Extra SSM parameter `/tobs/prod/DB_PASSWORD` (SecureString, free tier).
  `up.sh` stores the generated master password there before
  `create-db-instance`, so a crash between creating the DB and writing
  `DATABASE_URL` (the DB takes 10+ minutes) cannot lose it. Same prefix and
  protection as the listed secrets; deleted by `down.sh`.
- `DATABASE_URL` carries `?sslmode=require` from the start (U3 still to be
  checked against `rds.force_ssl`; require works either way).
- `deploy-aws.yml` is skipped while the `AWS_ROLE_ARN` variable is unset, so
  merging it before AWS exists does not turn `main` red.
- `.gitattributes` forces LF for `*.sh` (this checkout uses core.autocrlf, and
  CRLF breaks bash).

## Decisions and tradeoffs

- Secrets store: SSM Parameter Store SecureString (free standard tier) over
  Secrets Manager (about $0.40 per secret per month). ECS injects both the
  same way; Secrets Manager is the choice once rotation matters.
- Schema on first deploy: `CREATE_SCHEMA_ON_START=true` for the first deploy
  only (runs `create_all()`, idempotent via checkfirst). Alternatives:
  (a) apply `db/schema.sql` with `psql` from a one-off ECS task in the VPC
  (more correct, but RDS is private, so it needs run-task plumbing or a
  bastion); (b) write the first Alembic revision against a real Postgres
  (proper, out of budget). Chosen: the flag. Known limitation, in the README.
- Connection budget: 1 task x (DB_POOL_SIZE 5 + DB_MAX_OVERFLOW 10) = 15
  connections at most. `db.t4g.micro` (1 GiB) gets a default
  `max_connections` of roughly LEAST(DBInstanceClassMemory/9531392, 5000),
  about 100 after reserved memory; verify on the live instance with
  `SHOW max_connections`. 15 is well under it.

## Costs observed

(none yet: no AWS resources exist)

## Commands that failed and why

## Open questions

- Owner: has an AWS account ever existed under this identity? (Free plan
  eligibility, section 1.3.)
- Owner: AWS account ID and region (default `us-east-1`).
- Owner: install AWS CLI v2 (and optionally Docker Desktop) on this machine,
  then `aws configure sso --profile tobs`.

## Evidence index
