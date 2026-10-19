# AWS deploy notes

Running log for the AWS deployment described in `AWS_DEPLOY_INSTRUCTIONS.md`.
Append as you go; newest entries at the bottom of each section. No secret
values here, ever. Account IDs are written as `ACCOUNT`.

## Verified facts (date, source, command)

- 2026-10-19, local: baseline on `main` (7dbf0f7) with Python 3.12.10 in a
  fresh venv: `ruff check backend/ scripts/ evals/` clean, `mypy` clean
  (52 source files, strict), `pytest` 210 passed, `dump_schema.py` matches
  `db/schema.sql`. The README claimed 186 tests; corrected to 210.
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

## Deviations from AWS_DEPLOY_INSTRUCTIONS.md

- No Docker locally: image bootstrap must use section 5.6 option B (CI
  build-and-push via `workflow_dispatch`) unless Docker Desktop is installed.
  The local container check in section 4 is replaced by the CI `docker` job,
  which already builds the image and imports the app.

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
