"""Two MCP tools behind the gateway. Deliberately returns PII so the response
interceptor has something to redact."""
import json

ORDERS = {
    "t1":  [
        {"order_id": "A-1", "item": "Desk", "amount": 420, "customer_email": "jane@acme.com", "customer_phone": "+1-555-0142"}
    ],
    "t2":[
        {"order_id": "G-9", "item": "Chair", "amount": 180, "customer_email": "bob@globex.com", "customer_phone": "+1-555-0199"}
    ],
}


def _tool_name(event, context):
    cc = getattr(context, "client_context", None)
    name = (cc.custom or {}).get("bedrockAgentCoreToolName") if cc else None
    name = name or event.get("tool_name", "")
    return name.split("___")[-1]


def lambda_handler(event, context):
    tool = _tool_name(event, context)
    # tenant_id was injected by the REQUEST interceptor (never trusted from the client)
    tenant = event.get("tenant_id")
    if not tenant:
        raise Exception("no tenant_id in token")

    if tool == "list_orders":
        return {"statusCode": 200, "body": json.dumps({"orders": ORDERS.get(tenant, [])})}

    if tool == "issue_refund":                       # the privileged tool Cedar guards
        return {"statusCode": 200, "body": json.dumps({"refunded": True, "order_id": event.get("order_id"), "amount": event.get("amount"), "tenant": tenant})}

    return {"statusCode": 400, "body": json.dumps({"error": f"unknown tool {tool}"})}
