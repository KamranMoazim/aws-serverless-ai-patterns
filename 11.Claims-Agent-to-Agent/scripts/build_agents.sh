#!/usr/bin/env bash
# Create the ECR repos and build/push the agent images (one repo per agent).
#
#   ./scripts/build_agents.sh                  # all five agents, tag "latest"
#   ./scripts/build_agents.sh all 3            # all five agents, tag 3
#   ./scripts/build_agents.sh fraud 3          # just the fraud agent, tag 3
#   ./scripts/build_agents.sh orchestrator 3
#
# Each agents/<name>/ folder is its own service, so it gets its own repo
# (claims-a2a-<name>) and its own release. Rebuilding one agent leaves the
# other four untouched - that is the point of splitting the image.
#
# The tag SHOULD change between pushes. AgentCore Runtime pins the container URI
# at create/update time, so re-pushing the same tag leaves CloudFormation seeing
# no property change and the old image keeps serving. Numbered tags force the roll.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS=(orchestrator intake policy fraud payout)
SLUG="claims-a2a"

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

# Resolve the targets before touching ECR, so a typo fails before any push.
if [[ "$WHAT" == "all" ]]; then
    TARGETS=("${AGENTS[@]}")
else
    TARGETS=("$WHAT")
    # shellcheck disable=SC2076
    if [[ ! " ${AGENTS[*]} " =~ " $WHAT " ]]; then
        echo "unknown agent '$WHAT' - expected one of: all ${AGENTS[*]}" >&2
        exit 1
    fi
fi

for agent in "${TARGETS[@]}"; do
    if [[ ! -f "$ROOT/agents/$agent/Dockerfile" ]]; then
        echo "missing $ROOT/agents/$agent/Dockerfile" >&2
        exit 1
    fi
done

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

echo "-> logging in to $REGISTRY"
aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "$REGISTRY"

# The Dockerfiles pull their base from public.ecr.aws, which rate-limits anonymous pulls.
echo "-> logging in to public.ecr.aws"
aws ecr-public get-login-password --region us-east-1 \
    | docker login --username AWS --password-stdin public.ecr.aws

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

ensure_repo() {
    local repo="$1"
    if aws ecr describe-repositories --repository-names "$repo" --region "$REGION" >/dev/null 2>&1; then
        return
    fi
    echo "-> creating repo $repo"
    aws ecr create-repository \
        --repository-name "$repo" \
        --region "$REGION" \
        --image-scanning-configuration scanOnPush=true >/dev/null
}

# AgentCore Runtime is arm64 only. --provenance/--sbom off and oci-mediatypes=false
# keep BuildKit from pushing a multi-arch index with an attestation manifest, which
# AgentCore rejects - it surfaces as a silent "image pull failed".
build_and_push() {
    local agent="$1" repo="$SLUG-$1"
    ensure_repo "$repo"
    echo "-> building $REGISTRY/$repo:$TAG  (from agents/$agent)"
    docker buildx build \
        --platform linux/arm64 \
        --provenance=false --sbom=false \
        --output "type=image,oci-mediatypes=false,push=true" \
        -t "$REGISTRY/$repo:$TAG" \
        "$ROOT/agents/$agent"
    echo "   pushed $REGISTRY/$repo:$TAG"
}

for agent in "${TARGETS[@]}"; do
    build_and_push "$agent"
    record_tag "$agent" "$TAG"
done

echo
echo "Pushed ${#TARGETS[@]} image(s) with tag '$TAG'."

if [[ "$TAG" == "latest" ]]; then
    echo
    echo "Note: you reused 'latest'. AgentCore pins the image URI at update time, so"
    echo "cdk deploy will see no change and the running agents keep the old image."
    echo "Use a fresh tag to actually roll them."
    exit 0
fi

echo
echo "Tag '$TAG' recorded in .image-tags, so a bare deploy picks it up:"
echo "  SENDER_EMAIL=you@verified.example.com cdk deploy"
