#!/usr/bin/env bash
# Revert everything this project created.
#   1. terminate running/suspended MicroVMs   (runtime resources — not in CFN)
#   2. delete the MicroVM image                (built by build_image.sh — not in CFN)
#   3. cdk destroy                             (Lambda, API GW, DynamoDB, S3, IAM)
#
# Usage: [IMAGE_NAME=code-sandbox] ./scripts/teardown.sh
# Note: NOT using `set -e` — we want cleanup to continue past individual failures.
set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
IMAGE_NAME="${IMAGE_NAME:-code-sandbox}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
IMAGE_ARN_ID="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:microvm-image:${IMAGE_NAME}"

echo "==> 1/3 Terminating MicroVMs…"
IDS=$(aws lambda-microvms list-microvms --region "$REGION" \
        --query 'microvms[].microvmId' --output text 2>/dev/null || true)
if [ -z "${IDS:-}" ]; then
  echo "    (none running)"
else
  for id in $IDS; do
    echo "    terminating $id"
    aws lambda-microvms terminate-microvm --microvm-identifier "$id" \
      --region "$REGION" >/dev/null 2>&1 || echo "      (failed / already gone)"
  done
fi

echo "==> 2/3 Deleting MicroVM image '$IMAGE_NAME'…"
if aws lambda-microvms delete-microvm-image --image-identifier "$IMAGE_ARN_ID" \
     --region "$REGION" >/dev/null 2>&1; then
  echo "    deleted"
else
  echo "    (not found / already gone — if it refuses, ensure no MicroVM still references it)"
fi

echo "==> 3/3 cdk destroy (Lambda, API GW, DynamoDB, S3, IAM)…"
cdk destroy --force

echo "Done."