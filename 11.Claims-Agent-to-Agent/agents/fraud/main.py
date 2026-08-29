"""Fraud Agent - an independent A2A service.

The score comes from deterministic Lambda rules, not from the model. This agent
explains the signals it was handed; it does not decide the risk level.
"""
import json
import os

import boto3
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent.a2a import A2AServer

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
FRAUD_FN = os.environ["FRAUD_FN"]
SYSTEM_PROMPT = (
    "You assess fraud risk and explain the signals you used. "
    "The risk level comes from the scoring tool - never override it."
)

lambda_client = boto3.client("lambda")


@tool
def score_fraud_risk(claimant_id: str, amount: float) -> str:
    """Score a claim's fraud risk against rules and the claimant's history."""
    response = lambda_client.invoke(
        FunctionName=FRAUD_FN,
        Payload=json.dumps({"claimant_id": claimant_id, "amount": amount}).encode(),
    )
    return response["Payload"].read().decode()


agent = Agent(
    name="Fraud Agent",
    description=SYSTEM_PROMPT,
    system_prompt=SYSTEM_PROMPT,
    model=BedrockModel(model_id=MODEL_ID, temperature=0),
    tools=[score_fraud_risk],
)

# A2A contract: stateless HTTP on 0.0.0.0:9000, agent card at
# /.well-known/agent-card.json - AgentCore proxies JSON-RPC straight through.
server = A2AServer(agent=agent, host="0.0.0.0", port=9000)

if __name__ == "__main__":
    server.serve()
