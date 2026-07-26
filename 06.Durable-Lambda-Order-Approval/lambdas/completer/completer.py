"""GET /approve?cb=<callback_id>&decision=approve|reject -> resume the durable execution."""
import json

import boto3

lam = boto3.client("lambda")


def handler(event, context):
    q = event.get("queryStringParameters") or {}
    cb = q.get("cb")
    decision = q.get("decision", "reject")
    if not cb:
        return {"statusCode": 400, "body": "missing cb"}

    # Completes the callback the durable fn is suspended on -> it resumes and finalizes.
    lam.send_durable_execution_callback_success(
        CallbackId=cb,
        Result=json.dumps({"decision": decision}).encode(),
    )
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": f"<h2>Order {decision}d</h2><p>You can close this tab.</p>",
    }
