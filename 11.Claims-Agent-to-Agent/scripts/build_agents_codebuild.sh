#!/usr/bin/env bash
# Build and push the agent images on CodeBuild ARM_CONTAINER (Graviton) instead
# of locally. Same interface as build_agents.sh:
#
#   ./scripts/build_agents_codebuild.sh                  # all five, tag "latest"
#   ./scripts/build_agents_codebuild.sh all 3            # all five, tag 3
#   ./scripts/build_agents_codebuild.sh fraud 3          # just fraud, tag 3
#
# Why this exists: AgentCore Runtime is arm64 only. On an x86 host, buildx has to
# emulate arm64 through QEMU, which turns a pip install into a 40-minute step.
# CodeBuild's ARM_CONTAINER runs on Graviton, so the build is native, and the
# push to ECR happens inside AWS rather than over your uplink.
#
# First run creates the S3 source bucket, the IAM role and the CodeBuild project;
# later runs reuse them. Teardown is at the bottom of the file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS=(orchestrator intake policy fraud payout)
SLUG="claims-a2a"

PROJECT="$SLUG-agent-builder"
ROLE_NAME="$SLUG-codebuild-role"
POLICY_NAME="$SLUG-codebuild-policy"
# Graviton standard image. `aws codebuild list-curated-environment-images` lists these.
BUILD_IMAGE="aws/codebuild/amazonlinux-aarch64-standard:3.0"
COMPUTE_TYPE="BUILD_GENERAL1_SMALL"
SOURCE_KEY="source.zip"

WHAT="${1:-all}"
TAG="${2:-latest}"
REGION="${AWS_REGION:-us-east-1}"

if [[ "$WHAT" == "-h" || "$WHAT" == "--help" ]]; then
    echo "usage: $0 [all|$(IFS='|'; echo "${AGENTS[*]}")] [tag]"
    exit 0
fi

if [[ ! "$TAG" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "invalid tag '$TAG' - ECR allows letters, digits, '.', '_' and '-'" >&2
    exit 1
fi

# shellcheck disable=SC2076
if [[ "$WHAT" != "all" && ! " ${AGENTS[*]} " =~ " $WHAT " ]]; then
    echo "unknown agent '$WHAT' - expected one of: all ${AGENTS[*]}" >&2
    exit 1
fi

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="$SLUG-codebuild-src-$ACCOUNT-$REGION"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The stack reads .image-tags so a bare `cdk deploy` uses the tag just pushed,
# instead of a "latest" that may not exist in the repo.
record_tag() {
    local agent="$1" tag="$2" file="$ROOT/.image-tags"
    touch "$file"
    { grep -v "^${agent}=" "$file" || true; } > "$file.tmp"
    echo "${agent}=${tag}" >> "$file.tmp"
    sort -o "$file" "$file.tmp"
    rm -f "$file.tmp"
}

# ── Source bucket ─────────────────────────────────────────────────────────────
if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    echo "-> creating source bucket $BUCKET"
    if [[ "$REGION" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
    else
        aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
            --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    fi
    aws s3api put-public-access-block --bucket "$BUCKET" \
        --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null
fi

# ── IAM role ──────────────────────────────────────────────────────────────────
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "-> creating role $ROLE_NAME"
    cat > "$WORK/trust.json" <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
    aws iam create-role --role-name "$ROLE_NAME" \
        --assume-role-policy-document "file://$WORK/trust.json" >/dev/null
fi

cat > "$WORK/policy.json" <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
  "Resource":"arn:aws:logs:$REGION:$ACCOUNT:log-group:/aws/codebuild/$PROJECT*"},
 {"Effect":"Allow","Action":["s3:GetObject","s3:GetObjectVersion"],
  "Resource":"arn:aws:s3:::$BUCKET/*"},
 {"Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":"*"},
 {"Effect":"Allow","Action":["ecr-public:GetAuthorizationToken","sts:GetServiceBearerToken"],"Resource":"*"},
 {"Effect":"Allow","Action":["ecr:DescribeRepositories","ecr:CreateRepository",
   "ecr:BatchCheckLayerAvailability","ecr:InitiateLayerUpload","ecr:UploadLayerPart",
   "ecr:CompleteLayerUpload","ecr:PutImage","ecr:BatchGetImage","ecr:PutImageScanningConfiguration"],
  "Resource":"arn:aws:ecr:$REGION:$ACCOUNT:repository/$SLUG-*"}]}
JSON
aws iam put-role-policy --role-name "$ROLE_NAME" \
    --policy-name "$POLICY_NAME" --policy-document "file://$WORK/policy.json"
ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$ROLE_NAME"

# ── Source zip ────────────────────────────────────────────────────────────────
echo "-> packaging source"
( cd "$ROOT" && zip -qr "$WORK/$SOURCE_KEY" buildspec.yml agents \
    -x '*/__pycache__/*' -x '*.pyc' )
aws s3 cp "$WORK/$SOURCE_KEY" "s3://$BUCKET/$SOURCE_KEY" --region "$REGION" >/dev/null
echo "   uploaded s3://$BUCKET/$SOURCE_KEY ($(du -h "$WORK/$SOURCE_KEY" | cut -f1))"

# ── CodeBuild project ─────────────────────────────────────────────────────────
ENV_JSON="{\"type\":\"ARM_CONTAINER\",\"image\":\"$BUILD_IMAGE\",\"computeType\":\"$COMPUTE_TYPE\",\"privilegedMode\":true}"
SOURCE_JSON="{\"type\":\"S3\",\"location\":\"$BUCKET/$SOURCE_KEY\",\"buildspec\":\"buildspec.yml\"}"

if aws codebuild batch-get-projects --names "$PROJECT" --region "$REGION" \
        --query 'projects[0].name' --output text 2>/dev/null | grep -q "^$PROJECT$"; then
    aws codebuild update-project --name "$PROJECT" --region "$REGION" \
        --source "$SOURCE_JSON" --environment "$ENV_JSON" \
        --service-role "$ROLE_ARN" --artifacts '{"type":"NO_ARTIFACTS"}' \
        --timeout-in-minutes 60 >/dev/null
else
    echo "-> creating CodeBuild project $PROJECT"
    # IAM is eventually consistent: CodeBuild may not see the new role for a few seconds.
    for attempt in 1 2 3 4 5 6; do
        if aws codebuild create-project --name "$PROJECT" --region "$REGION" \
                --source "$SOURCE_JSON" --environment "$ENV_JSON" \
                --service-role "$ROLE_ARN" --artifacts '{"type":"NO_ARTIFACTS"}' \
                --timeout-in-minutes 60 >/dev/null 2>"$WORK/err"; then
            break
        fi
        if [[ $attempt -eq 6 ]]; then
            cat "$WORK/err" >&2
            exit 1
        fi
        echo "   waiting for IAM role to propagate (attempt $attempt)"
        sleep 10
    done
fi

# ── Run it ────────────────────────────────────────────────────────────────────
echo "-> starting build (agent=$WHAT tag=$TAG, $COMPUTE_TYPE on Graviton)"
BUILD_ID="$(aws codebuild start-build --project-name "$PROJECT" --region "$REGION" \
    --environment-variables-override \
        "name=AGENT,value=$WHAT,type=PLAINTEXT" \
        "name=IMAGE_TAG,value=$TAG,type=PLAINTEXT" \
    --query 'build.id' --output text)"
echo "   build id: $BUILD_ID"

LAST_PHASE=""
while :; do
    read -r STATUS PHASE < <(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
        --query 'builds[0].[buildStatus,currentPhase]' --output text)
    if [[ "$PHASE" != "$LAST_PHASE" ]]; then
        echo "   phase: $PHASE"
        LAST_PHASE="$PHASE"
    fi
    [[ "$STATUS" != "IN_PROGRESS" ]] && break
    sleep 10
done

read -r LOG_GROUP LOG_STREAM < <(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
    --query 'builds[0].logs.[groupName,streamName]' --output text)

if [[ "$STATUS" == "SUCCEEDED" ]]; then
    if [[ "$WHAT" == "all" ]]; then
        for agent in "${AGENTS[@]}"; do record_tag "$agent" "$TAG"; done
    else
        record_tag "$WHAT" "$TAG"
    fi
    echo
    echo "Build SUCCEEDED. Tag '$TAG' pushed and recorded in .image-tags:"
    echo "  SENDER_EMAIL=you@verified.example.com cdk deploy"
    exit 0
fi

echo
echo "Build $STATUS. Last 60 log lines:" >&2
aws logs get-log-events --log-group-name "$LOG_GROUP" --log-stream-name "$LOG_STREAM" \
    --region "$REGION" --limit 60 --query 'events[*].message' --output text 2>/dev/null \
    | sed 's/^/  /' >&2 || echo "  (no logs yet)" >&2
exit 1

# Teardown:
#   aws codebuild delete-project --name claims-a2a-agent-builder --region us-east-1
#   aws iam delete-role-policy --role-name claims-a2a-codebuild-role --policy-name claims-a2a-codebuild-policy
#   aws iam delete-role --role-name claims-a2a-codebuild-role
#   aws s3 rb "s3://claims-a2a-codebuild-src-$(aws sts get-caller-identity --query Account --output text)-us-east-1" --force
