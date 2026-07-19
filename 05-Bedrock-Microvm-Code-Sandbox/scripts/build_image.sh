#!/usr/bin/env bash
# Build the MicroVM image from ./sandbox and wire its ARN into the orchestrator.
# Usage: MicrovmExecutionRoleArn=<arn> BUNDLE_BUCKET=<bucket> ORCH_FN=<fn-name> ./scripts/build_image.sh
# Usage: MicrovmExecutionRoleArn=arn:aws:iam::1234567890:role/microvm-code-sandbox-MicrovmExecutionRole BUNDLE_BUCKET=microvm-code-sandbox-micro-vm-code-bucket ORCH_FN=microvm-code-sandbox-orchestrator ./scripts/build_image.sh
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
MicrovmExecutionRoleArn="${MicrovmExecutionRoleArn:-arn:aws:iam::1234567890:role/microvm-code-sandbox-MicrovmExecutionRole}"
BUNDLE_BUCKET="${BUNDLE_BUCKET:?set BUNDLE_BUCKET (from stack output)}"
ORCH_FN="${ORCH_FN:?set ORCH_FN (orchestrator function name)}"
IMAGE_NAME="${IMAGE_NAME:-code-sandbox}"
KEY="microvm/sandbox-$(date +%s).zip"

# 1. Zip the sandbox (Dockerfile + app) and upload to S3
( cd micro_vm && zip -qr /tmp/sandbox.zip . )
aws s3 cp /tmp/sandbox.zip "s3://${BUNDLE_BUCKET}/${KEY}" --region "$REGION"

# 2. Find the Lambda-published managed base image ARN for MicroVMs.
BASE_IMAGE_ARN="arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1"

# 3. Create or update the image depending on whether it already exists.
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
IMAGE_ARN_ID="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:microvm-image:${IMAGE_NAME}"

if aws lambda-microvms get-microvm-image --image-identifier "$IMAGE_ARN_ID" \
    --region "$REGION" >/dev/null 2>&1; then
  echo "Image '$IMAGE_NAME' exists — updating."
  IMAGE_ARN=$(aws lambda-microvms update-microvm-image \
    --image-identifier "$IMAGE_ARN_ID" \
    --code-artifact uri="s3://${BUNDLE_BUCKET}/${KEY}" \
    --base-image-arn "$BASE_IMAGE_ARN" \
    --build-role-arn "$MicrovmExecutionRoleArn" \
    --region "$REGION" \
    --output text)
else
  echo "Image '$IMAGE_NAME' does not exist — creating."
  IMAGE_ARN=$(aws lambda-microvms create-microvm-image \
    --name "$IMAGE_NAME" \
    --code-artifact uri="s3://${BUNDLE_BUCKET}/${KEY}" \
    --base-image-arn "$BASE_IMAGE_ARN" \
    --build-role-arn "$MicrovmExecutionRoleArn" \
    --region "$REGION" \
    --output text)
fi

# 4. Wait for the build to reach SUCCESSFUL (2–3 min).
echo "Waiting for image build (2-3 min)…"
while :; do
  STATE=$(aws lambda-microvms get-microvm-image --image-identifier "$IMAGE_ARN_ID" \
    --region "$REGION" --query 'state' --output text 2>/dev/null || echo PENDING)
  echo "  state: $STATE"
  [ "$STATE" = "CREATED" ] || [ "$STATE" = "UPDATED" ] && break
  [ "$STATE" = "CREATE_FAILED" ] || [ "$STATE" = "UPDATE_FAILED" ] && { echo "image build FAILED"; exit 1; }
  sleep 15
done

# 5. Point the orchestrator at the new image.
aws lambda update-function-configuration \
  --function-name "$ORCH_FN" \
  --environment "Variables={SESSIONS_TABLE=$(aws lambda get-function-configuration --function-name "$ORCH_FN" --region "$REGION" --query 'Environment.Variables.SESSIONS_TABLE' --output text),MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0,MICROVM_IMAGE_ARN=$IMAGE_ARN}" \
  --region "$REGION" >/dev/null
echo "Done. Orchestrator now uses $IMAGE_ARN"