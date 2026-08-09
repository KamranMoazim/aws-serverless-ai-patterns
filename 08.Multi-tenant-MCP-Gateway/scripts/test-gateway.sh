#!/usr/bin/env bash
#
# End-to-end test for the governed multi-tenant MCP gateway.
#
#   ./scripts/test-gateway.sh
#   VERBOSE=1 ./scripts/test-gateway.sh    # dump every raw response
#
# Creates two users (admin/t1, user/t2), fetches access tokens, and asserts the
# full allow/deny matrix. Idempotent — safe to re-run.
#
set -uo pipefail

STACK="${STACK:-MultiTenantMCPGatewayStack}"
PASSWORD="${TEST_PASSWORD:-SomePassw0rd!}"
VERBOSE="${VERBOSE:-1}"

PASS=0
FAIL=0

green() { printf '\033[32m%s\033[0m\n' "$1"; }
red()   { printf '\033[31m%s\033[0m\n' "$1"; }
dim()   { printf '\033[2m%s\033[0m\n' "$1"; }

# Pretty-print a response, falling back to raw for SSE-framed / non-JSON bodies.
show() {
  [ -n "$VERBOSE" ] || return 0
  local body=$1 pretty
  if pretty=$(echo "$body" | jq . 2>/dev/null) && [ -n "$pretty" ]; then
    echo "$pretty" | sed 's/^/          /'
  else
    echo "$body" | head -c 800 | sed 's/^/          /'
    echo
  fi
  echo
}

# ── Stack outputs ─────────────────────────────────────────────────────────────
dim "Reading stack outputs from $STACK ..."
OUTPUTS=$(aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].Outputs' --output json) || { red "stack not found"; exit 1; }

get_out() { echo "$OUTPUTS" | jq -r --arg k "$1" '.[] | select(.OutputKey==$k) | .OutputValue'; }

GW=$(get_out GatewayUrl)
POOL=$(get_out UserPoolId)
CID=$(get_out PublicClientId)
ENGINE=$(get_out PolicyEngineId)

[ -n "$GW" ] || { red "GatewayUrl missing from outputs"; exit 1; }

CSEC=$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$POOL" --client-id "$CID" \
  --query 'UserPoolClient.ClientSecret' --output text)

GW_ID="${GW#https://}"; GW_ID="${GW_ID%%.*}"

echo "  gateway  $GW_ID"
echo "  pool     $POOL"
echo "  engine   $ENGINE"
echo

# ── Preflight: is the engine actually attached, and enforcing? ────────────────
dim "Preflight ..."
PEC=$(aws bedrock-agentcore-control get-gateway \
  --gateway-identifier "$GW_ID" --query 'policyEngineConfiguration' --output json 2>/dev/null)

if [ "$PEC" = "null" ] || [ -z "$PEC" ]; then
  red "  policyEngineConfiguration is null — the engine is NOT wired to the gateway."
  red "  Nothing below is being enforced. Check the L1 escape hatch in the stack."
  exit 1
fi

MODE=$(echo "$PEC" | jq -r '.mode')
echo "  policy engine mode: $MODE"
[ "$MODE" = "LOG_ONLY" ] && red "  WARNING: LOG_ONLY — deny assertions will fail. Flip to ENFORCE."

# Policy status. Note the key is `policies`, not `items`.
aws bedrock-agentcore-control list-policies --policy-engine-id "$ENGINE" \
  --query 'policies[].{name:name,status:status}' --output text | while read -r n s; do
    if [ "$s" = "ACTIVE" ]; then echo "  policy $n: $s"; else red "  policy $n: $s"; fi
  done
echo

# ── User setup ────────────────────────────────────────────────────────────────
setup_user() {
  local username=$1 tenant=$2 group=${3:-}

  aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$username" \
    --message-action SUPPRESS >/dev/null 2>&1

  # --permanent clears FORCE_CHANGE_PASSWORD, which otherwise blocks initiate-auth
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$username" \
    --password "$PASSWORD" --permanent >/dev/null

  aws cognito-idp admin-update-user-attributes --user-pool-id "$POOL" --username "$username" \
    --user-attributes Name=custom:tenant_id,Value="$tenant" >/dev/null

  if [ -n "$group" ]; then
    aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" \
      --username "$username" --group-name "$group" >/dev/null
  fi
}

get_token() {
  local username=$1
  local sh
  sh=$(printf '%s%s' "$username" "$CID" | openssl dgst -sha256 -hmac "$CSEC" -binary | base64)
  aws cognito-idp initiate-auth --client-id "$CID" --auth-flow USER_PASSWORD_AUTH \
    --auth-parameters USERNAME="$username",PASSWORD="$PASSWORD",SECRET_HASH="$sh" \
    --query 'AuthenticationResult.AccessToken' --output text
}

dim "Setting up users ..."
setup_user kamran t1 admin
setup_user bob    t2
ADMIN_TOKEN=$(get_token kamran)
USER_TOKEN=$(get_token bob)

# The pre-token-generation trigger must put custom:tenant_id in the ACCESS token.
# Without it, principal.hasTag("custom:tenant_id") is false and everything denies.
decode_claims() { echo "$1" | cut -d. -f2 | tr '_-' '/+' | base64 -d 2>/dev/null; }
claim_of() { decode_claims "$1" | jq -r ".\"$2\" // empty"; }

for pair in "kamran:$ADMIN_TOKEN:t1" "bob:$USER_TOKEN:t2"; do
  IFS=: read -r who tok want <<<"$pair"
  got=$(claim_of "$tok" 'custom:tenant_id')
  if [ "$got" = "$want" ]; then
    echo "  $who: custom:tenant_id=$got in access token"
  else
    red "  $who: custom:tenant_id missing from access token (got '${got:-none}')"
    red "  → pre-token-generation trigger (V2_0) not working, or pool is on the Lite plan."
    exit 1
  fi
  # Full claim set — this is what Cedar builds the principal from.
  [ -n "$VERBOSE" ] && { dim "  $who access token claims:"; show "$(decode_claims "$tok")"; }
done
echo

# ── MCP helpers ───────────────────────────────────────────────────────────────
mcp() {
  local token=$1 payload=$2
  curl -s -X POST "$GW" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "$payload"
}

call_tool() {
  local token=$1 name=$2 args=$3
  mcp "$token" "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$name\",\"arguments\":$args}}"
}

assert() {
  local label=$1 expected=$2 resp=$3
  local actual

  if echo "$resp" | jq -e '.error' >/dev/null 2>&1; then
    actual="deny"
  elif echo "$resp" | jq -e '.result' >/dev/null 2>&1; then
    actual="allow"
  else
    actual="malformed"
  fi

  if [ "$actual" = "$expected" ]; then
    green "  PASS  $label ($actual)"
    PASS=$((PASS+1))
  else
    red   "  FAIL  $label — expected $expected, got $actual"
    FAIL=$((FAIL+1))
  fi

  show "$resp"

  # A Cedar *evaluation error* is a deny for the wrong reason — flag it even on PASS.
  if echo "$resp" | grep -q 'policy evaluation errors'; then
    red "        ^ denied by evaluation ERROR, not by policy logic:"
    echo "$resp" | grep -o 'Parameter format error[^"]*' | sed 's/^/          /'
  fi
}

list_tools() {
  local token=$1 raw
  raw=$(mcp "$token" '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
  show "$raw" >&2
  echo "$raw" | jq -r '.result.tools[].name' 2>/dev/null | tr '\n' ' '
}

# ── Tests ─────────────────────────────────────────────────────────────────────
echo "ADMIN (kamran, group=admin, tenant=t1)"

TOOLS=$(list_tools "$ADMIN_TOKEN")
if echo "$TOOLS" | grep -q issue_refund; then
  green "  PASS  tools/list includes issue_refund"; PASS=$((PASS+1))
else
  red   "  FAIL  tools/list should include issue_refund — got: $TOOLS"; FAIL=$((FAIL+1))
fi

assert "list_orders"        allow "$(call_tool "$ADMIN_TOKEN" orders___list_orders '{}')"
assert "issue_refund \$100"  allow "$(call_tool "$ADMIN_TOKEN" orders___issue_refund '{"order_id":"A-1","amount":100}')"
assert "issue_refund \$900"  deny  "$(call_tool "$ADMIN_TOKEN" orders___issue_refund '{"order_id":"A-1","amount":900}')"

# Tenant should be the JWT value, not anything the client sent.
SPOOF=$(call_tool "$ADMIN_TOKEN" orders___issue_refund \
  '{"order_id":"A-1","amount":100,"tenant_id":"SPOOFED"}')
show "$SPOOF"
TENANT=$(echo "$SPOOF" | jq -r '.result.content[0].text' 2>/dev/null \
  | jq -r '.body' 2>/dev/null | jq -r '.tenant' 2>/dev/null)
if [ "$TENANT" = "t1" ]; then
  green "  PASS  client-supplied tenant_id overwritten by interceptor (t1)"; PASS=$((PASS+1))
else
  red   "  FAIL  expected tenant=t1, got '${TENANT:-none}' — interceptor not enriching"; FAIL=$((FAIL+1))
fi

echo
echo "NON-ADMIN (bob, no group, tenant=t2)"

TOOLS=$(list_tools "$USER_TOKEN")
if echo "$TOOLS" | grep -q issue_refund; then
  red   "  FAIL  tools/list leaked issue_refund to a non-admin — got: $TOOLS"; FAIL=$((FAIL+1))
else
  green "  PASS  tools/list hides issue_refund"; PASS=$((PASS+1))
fi

assert "list_orders"        allow "$(call_tool "$USER_TOKEN" orders___list_orders '{}')"
assert "issue_refund \$100"  deny  "$(call_tool "$USER_TOKEN" orders___issue_refund '{"order_id":"A-1","amount":100}')"

echo
if [ "$FAIL" -eq 0 ]; then
  green "$PASS passed, 0 failed"
else
  red   "$PASS passed, $FAIL failed"
  exit 1
fi