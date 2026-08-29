"""Deterministic fraud rules + claimant history. Rules, not vibes.

Scoring is a pure read. It used to increment claim_count on every call, which made
the score a function of how many times you had asked - an agent that retried a tool
call inflated the very risk it was measuring. History accrues elsewhere; asking the
same question twice must give the same answer.
"""
import os
from decimal import Decimal

import boto3

table = boto3.resource("dynamodb").Table(os.environ["HISTORY_TABLE"])

AMOUNT_THRESHOLD = Decimal("10000")
PRIOR_CLAIM_THRESHOLD = 3


def handler(event, context):
    claimant_id = event["claimant_id"]
    amount = Decimal(str(event.get("amount", 0)))

    record = table.get_item(Key={"claimant_id": claimant_id}).get("Item") or {}
    prior_claims = int(record.get("claim_count", 0))
    flagged_claims = int(record.get("flagged_count", 0))

    score, signals = 0, []
    if amount > AMOUNT_THRESHOLD:
        score += 40
        signals.append(f"amount over ${AMOUNT_THRESHOLD:,.0f}")
    if prior_claims >= PRIOR_CLAIM_THRESHOLD:
        score += 30
        signals.append(f"{prior_claims} prior claims")
    if flagged_claims > 0:
        score += 40
        signals.append(f"{flagged_claims} previously flagged claims")

    level = "HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW"
    return {
        "risk_level": level,
        "score": score,
        "signals": signals,
        # Echo the inputs so the agent explains a score it can actually justify.
        "evaluated": {
            "claimant_id": claimant_id,
            "amount": float(amount),
            "prior_claims": prior_claims,
            "flagged_claims": flagged_claims,
        },
    }
