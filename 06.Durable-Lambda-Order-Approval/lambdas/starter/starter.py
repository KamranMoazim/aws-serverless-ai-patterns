"""POST /orders -> generate an order id, async-invoke the durable fn, return 202."""
import os
import json
import uuid

import boto3

lam = boto3.client("lambda")
ORDER_FN = os.environ["ORDER_FN_ARN"]


def handler(event, context):
    body = json.loads(event.get("body") or "{}")
    order_id = str(uuid.uuid4())           # generated HERE, not inside the durable handler
    payload = {
        "order_id": order_id,
        "item": body.get("item", "(unspecified)"),
        "amount": body.get("amount", 0),
        "customer_email": body.get("customer_email", ""),
    }
    # Event = async: the durable execution runs in the background and suspends for approval.
    lam.invoke(FunctionName=ORDER_FN, InvocationType="Event",
               Payload=json.dumps(payload).encode())
    return {
        "statusCode": 202,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"order_id": order_id, "status": "PENDING_APPROVAL"}),
    }
