"""Pre-token-generation trigger (V2_0).

Cognito does not put custom attributes in the ACCESS token by default — only the
ID token. AgentCore's Cedar principal is built from the access token, so
`principal.hasTag("custom:tenant_id")` sees nothing without this.
"""


def lambda_handler(event, context):
    attrs = event["request"].get("userAttributes", {})
    groups = event["request"].get("groupConfiguration", {}).get("groupsToOverride", []) or []
    tenant = attrs.get("custom:tenant_id")

    claims = {}
    if tenant:
        claims["custom:tenant_id"] = tenant
    if groups:
        claims["role"] = "admin" if "admin" in groups else "user"

    if claims:
        event["response"]["claimsAndScopeOverrideDetails"] = {
            "accessTokenGeneration": {"claimsToAddOrOverride": claims}
        }

    return event