"""RESPONSE interceptor — runs after the target, before the agent sees anything.

Two jobs:
  1. tools/list  → strip tools this tenant/role may not see (they never reach the LLM)
  2. tools/call  → redact PII from tool output
"""
import re
import json
import base64

# Tools only admins may even KNOW about.
ADMIN_ONLY = {"issue_refund"}

EMAIL = re.compile(r"[\w\.\-\+]+@[\w\-]+\.[\w\.\-]+")
PHONE = re.compile(r"\+?\d[\d\-\s\(\)]{7,}\d")


def _role(headers):
    for k, v in (headers or {}).items():
        if k.lower() == "authorization":
            token = v.replace("Bearer ", "").strip()
            if token.count(".") == 2:
                p = token.split(".")[1]
                p += "=" * (-len(p) % 4)
                try:
                    claims = json.loads(base64.urlsafe_b64decode(p))
                    return "admin" if "admin" in (claims.get("cognito:groups") or []) else "user"
                except Exception:
                    pass
    return "user"


def _redact(obj):
    if isinstance(obj, str):
        return PHONE.sub("[REDACTED_PHONE]", EMAIL.sub("[REDACTED_EMAIL]", obj))
    if isinstance(obj, list):
        return [_redact(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    return obj


def lambda_handler(event, context):
    mcp = event["mcp"]
    headers = mcp.get("gatewayRequest", {}).get("headers", {})
    gw_resp = mcp.get("gatewayResponse", {})
    body = gw_resp.get("body", {})
    result = body.get("result") or {}
    role = _role(headers)

    if "tools" in result and role != "admin":
        result["tools"] = [
            t for t in result["tools"]
            if t.get("name", "").split("___")[-1] not in ADMIN_ONLY
        ]
        body["result"] = result

    if "content" in result:
        result["content"] = _redact(result["content"])
        body["result"] = result

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayResponse": {"body": body}},
    }