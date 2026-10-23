# deploy/

Scripts that stand the service up on AWS (ECS Express Mode on Fargate, RDS
PostgreSQL, ECR, SSM Parameter Store, CloudWatch Logs, S3) and tear it down
again. Everything is tagged `project=tobs` and `teardown-by=2026-10-31`.

| File | What it does |
|---|---|
| `lib.sh` | Names, region and profile defaults, helpers. Sourced by the others. |
| `up.sh` | Creates or reconciles the stack. Idempotent, safe to rerun. |
| `down.sh` | Deletes the stack. Dry run unless given `--yes`. |
| `smoke.sh` | Checks a live URL: health, auth (401 without a key), docs off, ingest, summary. |
| `policies/` | IAM trust and permission policies, ECR and S3 lifecycle rules. Placeholders (`ACCOUNT`, `REGION`, `BUCKET`, `OIDC_SUB`) are filled in by `up.sh`. |
| `NOTES.md` | Verified facts, deviations, decisions and costs, kept as the work happens. |

`deploy/.state/ids.env` (gitignored) records the IDs `up.sh` discovered so
`down.sh` knows what to delete. `down.sh` also finds everything by name and tag
if that file is gone.

## Prerequisites

- AWS CLI v2, `openssl`, `curl`, `git`, run from bash (Git Bash on Windows).
- A named profile with admin rights, short-lived via IAM Identity Center:
  `aws configure sso --profile tobs`, then `aws sso login --profile tobs`.
- Docker is optional. Without it, the first image is pushed by the
  `Deploy to AWS` workflow with `build_only` checked.

## Runbook

```bash
export AWS_PROFILE=tobs AWS_REGION=us-east-1 EXPECTED_ACCOUNT_ID=<12 digits>

deploy/up.sh                        # infra, RDS (10 to 20 min), roles, OIDC, bucket
# set repo variables AWS_ROLE_ARN (printed) and AWS_REGION, secret TOBS_API_KEY
# run the "Deploy to AWS" workflow with build_only=true to push the first image
IMAGE_TAG=<7 char sha> deploy/up.sh # creates the Express service
BASE_URL=<endpoint> deploy/smoke.sh

deploy/down.sh                      # see what would be deleted
deploy/down.sh --yes                # delete it
```

Every script refuses to run if the profile's account is not
`EXPECTED_ACCOUNT_ID` (or the account recorded in the state file).

## Pipeline

`.github/workflows/deploy-aws.yml` runs after CI passes on `main`. It assumes
`tobs-github-deploy` through GitHub OIDC (no AWS keys in GitHub), builds and
pushes `tobs-api:<sha7>`, deploys with
`aws-actions/amazon-ecs-deploy-express-service`, runs `smoke.sh` against the
new endpoint, and copies the smoke log, CI coverage and a summary to
`s3://tobs-reports-<account>-<region>/reports/<sha>/`. With the `AWS_ROLE_ARN`
variable unset the job is skipped.

The role's trust policy accepts only
`repo:AtharvaK14/Test-Observability-AI-Root-Cause-Agent:ref:refs/heads/main`.
Confirm that value with the temporary `oidc-debug.yml` workflow before relying
on it, and pass `OIDC_SUB=...` to `up.sh` if it differs.
