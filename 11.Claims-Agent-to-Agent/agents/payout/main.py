"""Payout Agent - an independent A2A service.

The only agent that can touch money, and even it only *proposes*: the durable
payout function suspends over the auto-approve threshold and waits for a human.
"""
import json
import os
import re

import boto3
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent.a2a import A2AServer

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
PAYOUT_FN = os.environ["PAYOUT_FN"]
CLAIMS_TABLE = os.environ.get("CLAIMS_TABLE", "")
AUTO_APPROVE_UNDER = float(os.environ.get("AUTO_APPROVE_UNDER", "5000"))
SYSTEM_PROMPT = (
    "You initiate payouts and report their recorded outcome. You do not decide them. "
    f"Amounts at or above {AUTO_APPROVE_UNDER:.0f} suspend for human approval, and the "
    "decision can take days, so you never know the outcome during this conversation. "
    "Report such a payout as PENDING human approval - never as approved, paid, settled or "
    "processing. Never start a payout for a claim flagged as high fraud risk."
)

lambda_client = boto3.client("lambda")
claims = boto3.resource("dynamodb").Table(CLAIMS_TABLE) if CLAIMS_TABLE else None


@tool
def request_payout(claim_id: str, claimant_email: str, amount: float) -> str:
    """Submit a claim to the payout workflow.

    Returns whether the payout auto-pays or is suspended awaiting a human decision.
    It never returns the final outcome - that arrives later, out of band.
    """
    lambda_client.invoke(
        FunctionName=PAYOUT_FN,
        InvocationType="Event",
        Payload=json.dumps({
            "claim_id": claim_id,
            "amount": amount,
            "claimant_email": claimant_email,
        }).encode(),
    )
    # The invoke is asynchronous and the durable function may suspend for days, so
    # the only honest thing to report is which branch the amount selects.
    needs_human = float(amount) >= AUTO_APPROVE_UNDER
    return json.dumps({
        "submitted": True,
        "claim_id": claim_id,
        "amount": amount,
        "outcome": "PENDING_HUMAN_APPROVAL" if needs_human else "AUTO_PAYING",
        "detail": (
            f"{amount} is at or above the {AUTO_APPROVE_UNDER:.0f} threshold. The durable payout "
            "has suspended and emailed an approver. Nothing has been paid, and the final "
            "decision is not known yet."
            if needs_human else
            f"{amount} is under the {AUTO_APPROVE_UNDER:.0f} threshold, so it pays automatically."
        ),
    })


def _trailing_number(value: str):
    """Last run of digits in a string, as an int. 'claim-77' -> 77, 'CLM-2026-0077' -> 77."""
    digits = re.findall(r"\d+", value or "")
    return int(digits[-1]) if digits else None


def _resolve_claim_id(requested: str) -> str:
    """Map a loose reference onto a recorded claim id, in code rather than by guesswork.

    The model was resolving 'claim/claim-77' to CLM-2026-0077 on some runs and not
    others. Matching on the trailing number makes it the same every time, and an
    ambiguous reference is left alone so the caller sees the miss.
    """
    if claims is None or not requested:
        return requested
    recorded = [row.get("claim_id") for row in claims.scan(Limit=50).get("Items", [])]
    if requested in recorded:
        return requested
    wanted = _trailing_number(requested)
    if wanted is None:
        return requested
    matches = [known for known in recorded if _trailing_number(known) == wanted]
    return matches[0] if len(matches) == 1 else requested


@tool
def check_payout_status(claim_id: str) -> str:
    """Report the most recently recorded outcome of a claim: PAID, REJECTED, or none yet.

    A claim sitting with an approver has no record at all, which is different from being
    rejected. The record is the last decision written, not necessarily a final one. Pass the
    claim id from the document, for example CLM-2026-0077.
    """
    if claims is None:
        return json.dumps({"error": "no claims table configured"})

    claim_id = _resolve_claim_id(claim_id)
    item = claims.get_item(Key={"claim_id": claim_id}).get("Item")
    if item:
        return json.dumps({
            "claim_id": claim_id,
            "recorded_status": item.get("status"),
            # Claim id is the partition key, so a re-submission overwrites this row.
            # The record is the last decision, which is not the same as the final one.
            "detail": ("This is the most recently recorded decision for that claim id. "
                       "A later submission for the same id may still be awaiting approval, "
                       "so do not describe this as final."),
        })

    # Small demo table: listing the known ids lets the caller resolve a loose
    # reference like "claim-77" without a second round trip.
    known = [row.get("claim_id") for row in claims.scan(Limit=25).get("Items", [])]
    return json.dumps({
        "claim_id": claim_id,
        "recorded_status": None,
        "detail": ("No decision recorded for that claim id. It is either still waiting on a human approver, or the id is wrong."),
        "recorded_claim_ids": known,
    })


agent = Agent(
    name="Payout Agent",
    description=SYSTEM_PROMPT,
    system_prompt=SYSTEM_PROMPT,
    model=BedrockModel(model_id=MODEL_ID, temperature=0),
    tools=[request_payout, check_payout_status],
)

# A2A contract: stateless HTTP on 0.0.0.0:9000, agent card at
# /.well-known/agent-card.json - AgentCore proxies JSON-RPC straight through.
server = A2AServer(agent=agent, host="0.0.0.0", port=9000)

if __name__ == "__main__":
    server.serve()
