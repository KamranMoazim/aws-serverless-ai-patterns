"""REQUEST interceptor — runs BEFORE the Cedar policy.

Its job is enrichment, not enforcement: decode the caller's JWT, pull the tenant
and role, and inject them into the request. Cedar then evaluates that enriched
context deterministically. Never trust a tenant_id sent by the client.
"""
import json
import base64


def _claims(headers):
    auth = ""
    for k, v in (headers or {}).items():
        if k.lower() == "authorization":
            auth = v
            break
    token = auth.replace("Bearer ", "").strip()
    if not token or token.count(".") != 2:
        return {}
    p = token.split(".")[1]
    p += "=" * (-len(p) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def lambda_handler(event, context):
    gw_req = event["mcp"]["gatewayRequest"]
    headers = gw_req.get("headers", {})
    body = gw_req.get("body", {})

    claims = _claims(headers)
    tenant = claims.get("custom:tenant_id")
    groups = claims.get("cognito:groups", []) or []
    role = claims.get("role", "user")

    params = body.get("params") or {}
    if body.get("method") == "tools/call":
        if not tenant:
            raise Exception("no tenant_id in token")
        args = params.get("arguments") or {}
        args["tenant_id"] = tenant
        params["arguments"] = args
        body["params"] = params
        args["_groups"] = groups
        args["_role"] = role

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }