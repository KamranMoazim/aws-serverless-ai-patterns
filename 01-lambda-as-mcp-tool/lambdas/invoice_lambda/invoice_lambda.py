import os
import json
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ["INVOICES_TABLE"]
table = boto3.resource("dynamodb").Table(TABLE_NAME)


def _resolve_tool_name(event, context):
    """
    AgentCore Gateway delivers the tool name in the Lambda client context as
    'bedrockAgentCoreToolName', usually prefixed with the target name, e.g.
    'invoice-tools___get_invoice_by_id'. Fall back to the event for local tests.
    """
    name = None
    cc = getattr(context, "client_context", None)
    if cc is not None and getattr(cc, "custom", None):
        name = cc.custom.get("bedrockAgentCoreToolName")
    if not name:
        name = event.get("tool_name") or event.get("toolName")
    if name and "___" in name:
        name = name.split("___")[-1]
    return name


def _args(event):
    """Tool arguments arrive as the event body (or nested under 'arguments')."""
    if isinstance(event.get("arguments"), dict):
        return event["arguments"]
    return event


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj) if obj % 1 else int(obj)
        return super().default(obj)


def _ok(payload):
    return {
        "statusCode": 200,
        "body": json.dumps(payload, cls=_DecimalEncoder),
    }


# ── Tools ─────────────────────────────────────────────────────────────────────

def get_invoice_by_id(args):
    invoice_id = args["invoice_id"]
    resp = table.get_item(Key={"invoice_id": invoice_id})
    item = resp.get("Item")
    if not item:
        return {"found": False, "invoice_id": invoice_id}
    return {"found": True, "invoice": item}


def get_invoices_by_customer(args):
    customer = args["customer_name"]
    cond = Key("customer_name").eq(customer)

    start = args.get("start_date")
    end = args.get("end_date")
    if start and end:
        cond = cond & Key("invoice_date").between(start, end)
    elif start:
        cond = cond & Key("invoice_date").gte(start)
    elif end:
        cond = cond & Key("invoice_date").lte(end)

    resp = table.query(IndexName="CustomerIndex", KeyConditionExpression=cond)
    items = resp.get("Items", [])
    total = sum(Decimal(str(i.get("amount", 0))) for i in items)
    return {
        "customer_name": customer,
        "count": len(items),
        "total_amount": total,
        "invoices": items,
    }


def list_recent_invoices(args):
    limit = int(args.get("limit", 10))
    resp = table.scan(Limit=limit)
    items = resp.get("Items", [])
    items.sort(key=lambda i: i.get("invoice_date", ""), reverse=True)
    return {"count": len(items), "invoices": items[:limit]}


TOOLS = {
    "get_invoice_by_id": get_invoice_by_id,
    "get_invoices_by_customer": get_invoices_by_customer,
    "list_recent_invoices": list_recent_invoices,
}


def lambda_handler(event, context):
    tool_name = _resolve_tool_name(event, context)
    handler = TOOLS.get(tool_name)
    if handler is None:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Unknown tool: {tool_name}"}),
        }
    try:
        return _ok(handler(_args(event)))
    except KeyError as e:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Missing required argument: {e}"}),
        }