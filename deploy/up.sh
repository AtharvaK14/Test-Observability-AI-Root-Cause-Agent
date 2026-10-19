#!/usr/bin/env bash
# Provision the tobs stack on AWS. Idempotent: every step checks before it
# creates, so rerunning after a failure (or after the image exists) is safe.
#
#   EXPECTED_ACCOUNT_ID=123456789012 deploy/up.sh            # everything
#   EXPECTED_ACCOUNT_ID=... IMAGE_TAG=abc1234 deploy/up.sh   # pick the image
#
# The service is only created once an image with IMAGE_TAG (default: the
# current commit's short SHA) is in ECR. Without Docker, push it with the
# deploy-aws workflow's build-only dispatch, then rerun this script.
#
# Flags marked VERIFY come from AWS docs, not from running them; check them
# with `aws <service> <command> help` on first use and record any difference in
# deploy/NOTES.md.

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_tools aws openssl
ACCOUNT="$(check_account)"
state_set ACCOUNT_ID "$ACCOUNT"
state_set REGION "$AWS_REGION"
log "account $ACCOUNT, region $AWS_REGION, profile $AWS_PROFILE"

REGISTRY="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
BUCKET="$(report_bucket "$ACCOUNT")"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "$REPO_ROOT" rev-parse --short=7 HEAD)}"
SSM_ARN="arn:aws:ssm:$AWS_REGION:$ACCOUNT:parameter$SSM_PREFIX"

render() {  # substitute placeholders in a policy template
  sed -e "s|ACCOUNT|$ACCOUNT|g" -e "s|REGION|$AWS_REGION|g" -e "s|BUCKET|$BUCKET|g" \
      -e "s|OIDC_SUB|${OIDC_SUB:-}|g" "$DEPLOY_DIR/policies/$1"
}

ssm_exists() { aws ssm get-parameter --name "$1" --query Parameter.Name --output text >/dev/null 2>&1; }

# --- 1. ECR -------------------------------------------------------------------
if ! aws ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1; then
  log "creating ECR repository $ECR_REPO"
  # shellcheck disable=SC2046  # tag pairs are meant to split into words
  aws ecr create-repository --repository-name "$ECR_REPO" \
    --image-tag-mutability IMMUTABLE \
    --image-scanning-configuration scanOnPush=true \
    --tags $(tags_kv) >/dev/null
fi
aws ecr put-lifecycle-policy --repository-name "$ECR_REPO" \
  --lifecycle-policy-text "file://$DEPLOY_DIR/policies/ecr-lifecycle.json" >/dev/null

# --- 2. CloudWatch Logs ---------------------------------------------------------
if [ -z "$(aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
      --query "logGroups[?logGroupName=='$LOG_GROUP'].logGroupName" --output text)" ]; then
  log "creating log group $LOG_GROUP"
  aws logs create-log-group --log-group-name "$LOG_GROUP" \
    --tags "project=$PROJECT,teardown-by=$TEARDOWN_BY"
fi
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 3

# --- 3. API key in SSM ------------------------------------------------------------
if ! ssm_exists "$SSM_PREFIX/API_KEY"; then
  log "generating API key into $SSM_PREFIX/API_KEY (value not printed)"
  # shellcheck disable=SC2046
  aws ssm put-parameter --name "$SSM_PREFIX/API_KEY" --type SecureString \
    --value "$(openssl rand -hex 32)" --tags $(tags_kv) >/dev/null
fi

# --- 4. RDS (slow; start it now, wait for it later) -------------------------------
VPC_ID="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true \
  --query 'Vpcs[0].VpcId' --output text)"
[ "$VPC_ID" != "None" ] || die "no default VPC in $AWS_REGION"
state_set VPC_ID "$VPC_ID"

RDS_SG="$(aws ec2 describe-security-groups \
  --filters Name=vpc-id,Values="$VPC_ID" Name=group-name,Values="$RDS_SG_NAME" \
  --query 'SecurityGroups[0].GroupId' --output text)"
if [ "$RDS_SG" = "None" ]; then
  log "creating security group $RDS_SG_NAME (no ingress yet)"
  RDS_SG="$(aws ec2 create-security-group --group-name "$RDS_SG_NAME" \
    --description "tobs RDS, ingress only from the ECS service SG" --vpc-id "$VPC_ID" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=project,Value=$PROJECT},{Key=teardown-by,Value=$TEARDOWN_BY},{Key=Name,Value=$RDS_SG_NAME}]" \
    --query GroupId --output text)"
fi
state_set RDS_SG "$RDS_SG"

if ! aws rds describe-db-instances --db-instance-identifier "$DB_ID" >/dev/null 2>&1; then
  PG_VERSION="$(aws rds describe-db-engine-versions --engine postgres --default-only \
    --query 'DBEngineVersions[0].EngineVersion' --output text)"
  log "creating RDS $DB_ID ($DB_CLASS, postgres $PG_VERSION); takes 5 to 15 minutes"
  # Hex: no characters that need URL-escaping in DATABASE_URL. Stored in SSM
  # before the create call so a crash between the two cannot orphan it.
  DB_PASSWORD="$(openssl rand -hex 24)"
  aws ssm put-parameter --name "$SSM_PREFIX/DB_PASSWORD" --type SecureString \
    --value "$DB_PASSWORD" --overwrite >/dev/null
  # shellcheck disable=SC2046
  aws rds create-db-instance \
    --db-instance-identifier "$DB_ID" \
    --engine postgres --engine-version "$PG_VERSION" \
    --db-instance-class "$DB_CLASS" \
    --allocated-storage 20 \
    --master-username "$DB_USER" \
    --master-user-password "$DB_PASSWORD" \
    --db-name "$DB_NAME" \
    --vpc-security-group-ids "$RDS_SG" \
    --no-publicly-accessible \
    --no-multi-az \
    --backup-retention-period 0 \
    --no-deletion-protection \
    --storage-encrypted \
    --no-enable-performance-insights \
    --tags $(tags_kv) >/dev/null
  unset DB_PASSWORD
  state_set DB_ENGINE_VERSION "$PG_VERSION"
fi

# --- 5. IAM roles for ECS ------------------------------------------------------------
ensure_role() {  # name, trust-policy file
  if ! aws iam get-role --role-name "$1" >/dev/null 2>&1; then
    log "creating IAM role $1"
    # shellcheck disable=SC2046
    aws iam create-role --role-name "$1" \
      --assume-role-policy-document "$(render "$2")" --tags $(tags_kv) >/dev/null
  fi
  aws iam get-role --role-name "$1" --query Role.Arn --output text
}

EXEC_ROLE_ARN="$(ensure_role "$ROLE_EXEC" ecs-tasks-trust.json)"
aws iam attach-role-policy --role-name "$ROLE_EXEC" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name "$ROLE_EXEC" --policy-name tobs-read-ssm \
  --policy-document "$(render execution-ssm.json)"

INFRA_ROLE_ARN="$(ensure_role "$ROLE_INFRA" ecs-infrastructure-trust.json)"
aws iam attach-role-policy --role-name "$ROLE_INFRA" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices

# The app calls no AWS APIs, so the task role has no permissions at all. It
# exists so the execution role is never reused as the task role.
TASK_ROLE_ARN="$(ensure_role "$ROLE_TASK" ecs-tasks-trust.json)"
state_set EXEC_ROLE_ARN "$EXEC_ROLE_ARN"
state_set INFRA_ROLE_ARN "$INFRA_ROLE_ARN"
state_set TASK_ROLE_ARN "$TASK_ROLE_ARN"

# --- 6. S3 report bucket ---------------------------------------------------------------
if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  log "creating report bucket $BUCKET"
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" \
      --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
  fi
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration "file://$DEPLOY_DIR/policies/s3-lifecycle.json"
aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging \
  "TagSet=[{Key=project,Value=$PROJECT},{Key=teardown-by,Value=$TEARDOWN_BY}]"
state_set BUCKET "$BUCKET"

# --- 7. GitHub OIDC provider and deploy role --------------------------------------------
OIDC_ARN="arn:aws:iam::$ACCOUNT:oidc-provider/$OIDC_HOST"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  # Pre-existing providers may back other roles: never record them as ours.
  [ "$(state_get OIDC_CREATED_BY_UP)" = "true" ] || log "GitHub OIDC provider already exists; reusing it"
else
  log "creating GitHub OIDC provider"
  # VERIFY: older CLI versions require --thumbprint-list; IAM no longer uses it
  # for this provider.
  # shellcheck disable=SC2046
  aws iam create-open-id-connect-provider --url "https://$OIDC_HOST" \
    --client-id-list sts.amazonaws.com --tags $(tags_kv) >/dev/null
  state_set OIDC_CREATED_BY_UP true
fi

# U8: the sub claim must come from a decoded real token (oidc-debug workflow).
# The classic format is the default; pass OIDC_SUB to override it.
OIDC_SUB="${OIDC_SUB:-repo:$GITHUB_REPO:ref:refs/heads/main}"
DEPLOY_ROLE_ARN="$(ensure_role "$ROLE_DEPLOY" github-trust.json)"
aws iam update-assume-role-policy --role-name "$ROLE_DEPLOY" \
  --policy-document "$(render github-trust.json)"
aws iam put-role-policy --role-name "$ROLE_DEPLOY" --policy-name tobs-deploy \
  --policy-document "$(render github-deploy.json)"
state_set DEPLOY_ROLE_ARN "$DEPLOY_ROLE_ARN"
state_set OIDC_SUB "$OIDC_SUB"

# --- 8. Wait for RDS, store DATABASE_URL ------------------------------------------------
log "waiting for RDS $DB_ID to be available"
aws rds wait db-instance-available --db-instance-identifier "$DB_ID"
DB_HOST="$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query 'DBInstances[0].Endpoint.Address' --output text)"
state_set DB_HOST "$DB_HOST"
if ! ssm_exists "$SSM_PREFIX/DATABASE_URL"; then
  ssm_exists "$SSM_PREFIX/DB_PASSWORD" || die "$SSM_PREFIX/DB_PASSWORD missing; reset the master password with modify-db-instance and store it there"
  DB_PASSWORD="$(aws ssm get-parameter --name "$SSM_PREFIX/DB_PASSWORD" --with-decryption \
    --query Parameter.Value --output text)"
  # U3: sslmode=require works whether or not the parameter group forces SSL.
  # shellcheck disable=SC2046
  aws ssm put-parameter --name "$SSM_PREFIX/DATABASE_URL" --type SecureString \
    --value "postgresql+psycopg://$DB_USER:$DB_PASSWORD@$DB_HOST:5432/$DB_NAME?sslmode=require" \
    --tags $(tags_kv) >/dev/null
  unset DB_PASSWORD
  log "stored $SSM_PREFIX/DATABASE_URL (value not printed)"
fi

# --- 9. Express service ----------------------------------------------------------------
if ! aws ecr describe-images --repository-name "$ECR_REPO" --image-ids "imageTag=$IMAGE_TAG" >/dev/null 2>&1; then
  log "no image $ECR_REPO:$IMAGE_TAG yet. Push one (deploy-aws workflow, build_only=true), then rerun."
  log "infrastructure is ready; see $STATE_FILE"
  exit 0
fi

if [ -z "$(aws ecs describe-clusters --clusters "$CLUSTER" \
      --query "clusters[?status=='ACTIVE'].clusterName" --output text)" ]; then
  # shellcheck disable=SC2046
  aws ecs create-cluster --cluster-name "$CLUSTER" --tags $(tags_kv_lower) >/dev/null
fi

SCHEMA_FLAG="${CREATE_SCHEMA_ON_START:-true}"
CONTAINER_JSON="$STATE_DIR/primary-container.json"
cat > "$CONTAINER_JSON" <<JSON
{
  "image": "$REGISTRY/$ECR_REPO:$IMAGE_TAG",
  "containerPort": 8000,
  "awsLogsConfiguration": {"logGroup": "$LOG_GROUP", "logStreamPrefix": "api"},
  "environment": [
    {"name": "ENVIRONMENT", "value": "prod"},
    {"name": "LOG_JSON", "value": "true"},
    {"name": "ANALYSIS_MODE", "value": "heuristic"},
    {"name": "CORS_ORIGINS", "value": "[]"},
    {"name": "MAX_INGEST_BYTES", "value": "10485760"},
    {"name": "AUTO_ANALYZE_ON_INGEST", "value": "true"},
    {"name": "CREATE_SCHEMA_ON_START", "value": "$SCHEMA_FLAG"}
  ],
  "secrets": [
    {"name": "DATABASE_URL", "valueFrom": "$SSM_ARN/DATABASE_URL"},
    {"name": "API_KEY", "valueFrom": "$SSM_ARN/API_KEY"}
  ]
}
JSON

SERVICE_ARN="arn:aws:ecs:$AWS_REGION:$ACCOUNT:service/$CLUSTER/$SERVICE"
if [ -z "$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
      --query "services[?status=='ACTIVE'].serviceName" --output text 2>/dev/null)" ]; then
  log "creating Express service $SERVICE with image $IMAGE_TAG"
  # VERIFY: --cpu/--memory units. The action and CLI reference use CPU units
  # and MiB (512/1024); the CLI tutorial shows vCPU/GB (2/4).
  # shellcheck disable=SC2046
  aws ecs create-express-gateway-service \
    --service-name "$SERVICE" \
    --cluster "$CLUSTER" \
    --infrastructure-role-arn "$INFRA_ROLE_ARN" \
    --execution-role-arn "$EXEC_ROLE_ARN" \
    --task-role-arn "$TASK_ROLE_ARN" \
    --health-check-path /health \
    --cpu 512 --memory 1024 \
    --cpu-architecture X86_64 \
    --scaling-target '{"minTaskCount":1,"maxTaskCount":1}' \
    --primary-container "file://$CONTAINER_JSON" \
    --tags $(tags_kv_lower) >/dev/null
fi
state_set SERVICE_ARN "$SERVICE_ARN"

# --- 10. Let the service reach RDS (U5) --------------------------------------------------
log "waiting for the service's security group"
SERVICE_SG=""
for _ in $(seq 1 30); do
  SERVICE_SG="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].networkConfiguration.awsvpcConfiguration.securityGroups[0]' \
    --output text 2>/dev/null || true)"
  [ -n "$SERVICE_SG" ] && [ "$SERVICE_SG" != "None" ] && break
  sleep 10
done
[ -n "$SERVICE_SG" ] && [ "$SERVICE_SG" != "None" ] || die "could not discover the service security group"
state_set SERVICE_SG "$SERVICE_SG"

if [ -z "$(aws ec2 describe-security-groups --group-ids "$RDS_SG" \
      --query "SecurityGroups[0].IpPermissions[?FromPort==\`5432\`].UserIdGroupPairs[?GroupId=='$SERVICE_SG'][] | [0].GroupId" \
      --output text | grep -v None || true)" ]; then
  log "allowing $SERVICE_SG -> $RDS_SG on 5432"
  aws ec2 authorize-security-group-ingress --group-id "$RDS_SG" \
    --protocol tcp --port 5432 --source-group "$SERVICE_SG" >/dev/null
  # Tasks that started before the rule existed failed in create_all() and are
  # being replaced; force a fresh deployment rather than wait for backoff.
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --force-new-deployment >/dev/null
fi

# --- 11. Endpoint and summary --------------------------------------------------------------
ENDPOINT="$(aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" \
  --query 'service.activeConfigurations[0].ingressPaths[0].endpoint' --output text 2>/dev/null || true)"
state_set ENDPOINT "$ENDPOINT"

cat >&2 <<SUMMARY

tobs stack (account $ACCOUNT, $AWS_REGION)
  ECR          $REGISTRY/$ECR_REPO:$IMAGE_TAG
  RDS          $DB_ID ($DB_CLASS) at $DB_HOST, SG $RDS_SG
  Service      $SERVICE in cluster $CLUSTER, SG $SERVICE_SG
  Logs         $LOG_GROUP (3 day retention)
  SSM          $SSM_PREFIX/{API_KEY,DATABASE_URL,DB_PASSWORD}
  S3           s3://$BUCKET
  Deploy role  $DEPLOY_ROLE_ARN (sub: $OIDC_SUB)
  URL          ${ENDPOINT:-pending, rerun or check describe-express-gateway-service}

Next: deploy/smoke.sh once the URL answers /health.
State: $STATE_FILE
SUMMARY
