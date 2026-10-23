#!/usr/bin/env bash
# Tear down the tobs stack, in reverse dependency order.
#
#   deploy/down.sh            # dry run (default): print what would be deleted
#   deploy/down.sh --yes      # delete it
#
# Deletes only resources named in lib.sh, recorded in deploy/.state/ids.env, or
# tagged project=tobs. Never touches the default VPC, its subnets, or its
# default security group. The GitHub OIDC provider is deleted only if up.sh
# created it and no other role trusts it.

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MODE=dry-run
case "${1:-}" in
  --yes) MODE=execute ;;
  --dry-run|"") MODE=dry-run ;;
  *) die "usage: $0 [--dry-run|--yes]" ;;
esac

require_tools aws
ACCOUNT="$(check_account)"
BUCKET="$(state_get BUCKET)"
BUCKET="${BUCKET:-$(report_bucket "$ACCOUNT")}"
log "teardown ($MODE) in account $ACCOUNT, $AWS_REGION"

run() {  # print in dry-run, execute otherwise
  if [ "$MODE" = dry-run ]; then
    printf 'would run: %s\n' "$*"
  else
    log "$*"
    "$@"
  fi
}

retry() {  # for deletes that wait on ENIs or eventual consistency
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if "$@"; then return 0; fi
    log "attempt $attempt failed, retrying in 30s"
    sleep 30
  done
  return 1
}

# --- 1. Express service and cluster -------------------------------------------------
SERVICE_ARN="$(state_get SERVICE_ARN)"
SERVICE_ARN="${SERVICE_ARN:-arn:aws:ecs:$AWS_REGION:$ACCOUNT:service/$CLUSTER/$SERVICE}"
if aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" >/dev/null 2>&1; then
  # VERIFY: command name (aws ecs help | grep -i express).
  run aws ecs delete-express-gateway-service --service-arn "$SERVICE_ARN" --monitor-resources
  if [ "$MODE" = execute ]; then
    log "waiting for $SERVICE to drain"
    aws ecs wait services-inactive --cluster "$CLUSTER" --services "$SERVICE" || true
  fi
fi
if [ -n "$(aws ecs describe-clusters --clusters "$CLUSTER" \
      --query "clusters[?status=='ACTIVE'].clusterName" --output text 2>/dev/null)" ]; then
  run retry aws ecs delete-cluster --cluster "$CLUSTER"
fi

# --- 2. Leftover load balancers and target groups (U6) --------------------------------
for arn in $(aws resourcegroupstaggingapi get-resources --tag-filters "Key=project,Values=$PROJECT" \
    --resource-type-filters elasticloadbalancing:loadbalancer \
    --query 'ResourceTagMappingList[].ResourceARN' --output text); do
  run aws elbv2 delete-load-balancer --load-balancer-arn "$arn"
done
for arn in $(aws resourcegroupstaggingapi get-resources --tag-filters "Key=project,Values=$PROJECT" \
    --resource-type-filters elasticloadbalancing:targetgroup \
    --query 'ResourceTagMappingList[].ResourceARN' --output text); do
  run retry aws elbv2 delete-target-group --target-group-arn "$arn"
done

# --- 3. RDS (delete, never stop: a stopped instance restarts after 7 days) -------------
if aws rds describe-db-instances --db-instance-identifier "$DB_ID" >/dev/null 2>&1; then
  run aws rds delete-db-instance --db-instance-identifier "$DB_ID" \
    --skip-final-snapshot --delete-automated-backups
  [ "$MODE" = dry-run ] || aws rds wait db-instance-deleted --db-instance-identifier "$DB_ID"
fi

# --- 4. Security groups (after RDS and the service ENIs are gone) ---------------------
VPC_ID="$(state_get VPC_ID)"
SG_FILTERS=("Name=tag:project,Values=$PROJECT")
[ -z "$VPC_ID" ] || SG_FILTERS+=("Name=vpc-id,Values=$VPC_ID")
for sg in $(aws ec2 describe-security-groups --filters "${SG_FILTERS[@]}" \
    --query "SecurityGroups[?GroupName!='default'].GroupId" --output text) $(state_get SERVICE_SG); do
  if aws ec2 describe-security-groups --group-ids "$sg" \
      --query "SecurityGroups[?GroupName!='default'].GroupId" --output text 2>/dev/null | grep -q .; then
    run retry aws ec2 delete-security-group --group-id "$sg"
  fi
done

# --- 5. ECR ------------------------------------------------------------------------
if aws ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1; then
  run aws ecr delete-repository --repository-name "$ECR_REPO" --force
fi

# --- 6. Logs (ours, plus any group Express created for the service) -----------------
for group in $(aws logs describe-log-groups --log-group-name-prefix /tobs \
      --query 'logGroups[].logGroupName' --output text) \
    $(aws logs describe-log-groups --log-group-name-prefix /aws/ecs \
      --query "logGroups[?contains(logGroupName, '$SERVICE')].logGroupName" --output text); do
  run aws logs delete-log-group --log-group-name "$group"
done

# --- 7. SSM ------------------------------------------------------------------------
params="$(aws ssm get-parameters-by-path --path "$SSM_PREFIX" --query 'Parameters[].Name' --output text)"
if [ -n "$params" ]; then
  # shellcheck disable=SC2086  # names are meant to split
  run aws ssm delete-parameters --names $params
fi

# --- 8. S3 -------------------------------------------------------------------------
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  run aws s3 rm "s3://$BUCKET" --recursive
  run aws s3api delete-bucket --bucket "$BUCKET"
fi

# --- 9. IAM --------------------------------------------------------------------------
delete_role() {
  aws iam get-role --role-name "$1" >/dev/null 2>&1 || return 0
  local policy
  for policy in $(aws iam list-attached-role-policies --role-name "$1" \
      --query 'AttachedPolicies[].PolicyArn' --output text); do
    run aws iam detach-role-policy --role-name "$1" --policy-arn "$policy"
  done
  for policy in $(aws iam list-role-policies --role-name "$1" --query 'PolicyNames[]' --output text); do
    run aws iam delete-role-policy --role-name "$1" --policy-name "$policy"
  done
  run aws iam delete-role --role-name "$1"
}
for role in "$ROLE_EXEC" "$ROLE_INFRA" "$ROLE_TASK" "$ROLE_DEPLOY"; do
  delete_role "$role"
done

OIDC_ARN="arn:aws:iam::$ACCOUNT:oidc-provider/$OIDC_HOST"
if [ "$(state_get OIDC_CREATED_BY_UP)" = "true" ] \
    && aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  others="$(aws iam list-roles --query 'Roles[].[RoleName,AssumeRolePolicyDocument]' --output json \
    | grep -c "$OIDC_HOST" || true)"
  if [ "$MODE" = execute ] && [ "$others" != "0" ]; then
    log "keeping the OIDC provider: $others other role(s) still trust it"
  else
    run aws iam delete-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN"
  fi
fi

# --- 10. What is left ------------------------------------------------------------------
if [ "$MODE" = execute ]; then
  log "remaining resources tagged project=$PROJECT (expect none):"
  aws resourcegroupstaggingapi get-resources --tag-filters "Key=project,Values=$PROJECT" \
    --query 'ResourceTagMappingList[].ResourceARN' --output text >&2
  log "teardown done. Run the clean-state checks in AWS_DEPLOY_INSTRUCTIONS.md section 8."
else
  log "dry run only. Rerun with --yes to delete."
fi
