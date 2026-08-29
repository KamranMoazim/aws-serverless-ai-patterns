"""Durable payout: small claims auto-pay; large ones suspend for human approval."""
import os, json
import boto3
from aws_durable_execution_sdk_python import (
    DurableContext, StepContext, durable_execution, durable_step)
from aws_durable_execution_sdk_python.config import Duration, CallbackConfig

ses = boto3.client("ses")
ddb = boto3.resource("dynamodb").Table(os.environ["CLAIMS_TABLE"])
SENDER = os.environ["SENDER_EMAIL"]
APPROVE_URL = os.environ.get("APPROVE_URL", "")
AUTO_APPROVE_UNDER = float(os.environ.get("AUTO_APPROVE_UNDER", "5000"))


@durable_step
def email_approver(ctx: StepContext, claim: dict, cb: str) -> str:
    ses.send_email(
        Source=SENDER, Destination={"ToAddresses": [SENDER]},
        Message={
            "Subject": {"Data": f"Approve payout {claim['claim_id']}?"},
            "Body": {"Html": {"Data":
                     f"<p>Claim {claim['claim_id']} - ${claim['amount']}</p>"
                     f"<p><a href='{APPROVE_URL}?cb={cb}&decision=approve'>Approve</a> | "
                     f"<a href='{APPROVE_URL}?cb={cb}&decision=reject'>Reject</a></p>"}}})
    return cb


@durable_step
def record(ctx: StepContext, claim_id: str, status: str) -> dict:
    ddb.put_item(Item={"claim_id": claim_id, "status": status})
    return {"claim_id": claim_id, "status": status}


def _decision(result) -> str:
    """The callback result arrives as the JSON text that was sent, not a dict."""
    if isinstance(result, (bytes, bytearray)):
        result = result.decode()
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return "reject"
    if not isinstance(result, dict):
        return "reject"
    return result.get("decision", "reject")


@durable_execution
def handler(event, context: DurableContext) -> dict:
    amount = float(event.get("amount", 0))

    if amount < AUTO_APPROVE_UNDER:
        return context.step(record(event["claim_id"], "PAID"))

    cb = context.create_callback(name="payout-approval", config=CallbackConfig(timeout=Duration.from_days(3)))
    context.step(email_approver(event, cb.callback_id))
    result = cb.result()          # suspends - no compute billed while waiting
    decision = _decision(result)
    return context.step(record(event["claim_id"], "PAID" if decision == "approve" else "REJECTED"))
