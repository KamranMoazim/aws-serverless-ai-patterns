"""Records a human decision on a suspended payout.

The approver reaches this from the link the durable payout emails. A callback is
single-use and time-boxed, so a replayed or expired link renders a readable page
rather than surfacing an API error as a 500.
"""
import json

import boto3

lambda_client = boto3.client("lambda")
DECISIONS = {"approve": "approved", "reject": "rejected"}

PAGE = (
    "<!doctype html><meta charset=utf-8><title>Claim decision</title>"
    "<body style=\"font:16px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;"
    "max-width:34rem;margin:14vh auto;padding:0 1.5rem;color:#16191d\">"
    "<h1 style=\"font-size:1.2rem;margin:0 0 .5rem\">{heading}</h1>"
    "<p style=\"margin:0;color:#5c6470\">{detail}</p></body>"
)


def page(status, heading, detail):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": PAGE.format(heading=heading, detail=detail),
    }


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    callback_id = params.get("cb")
    decision = params.get("decision", "reject")

    if not callback_id:
        return page(400, "Incomplete link", "The cb parameter is missing from this URL.")
    if decision not in DECISIONS:
        return page(400, "Unknown decision", "Expected decision=approve or decision=reject.")

    try:
        lambda_client.send_durable_execution_callback_success(
            CallbackId=callback_id,
            Result=json.dumps({"decision": decision}).encode(),
        )
    except (lambda_client.exceptions.ResourceNotFoundException, lambda_client.exceptions.CallbackTimeoutException):
        return page(410, "Nothing to record", "This claim was already decided, or the approval window has closed.")
    except lambda_client.exceptions.InvalidParameterValueException as error:
        return page(400, "Invalid callback", str(error))

    return page(200, f"Payout {DECISIONS[decision]}.", "The durable function has resumed. You can close this tab.")
