"""Orchestrator Agent - the only runtime on the HTTP protocol.

It owns no domain tools. Each specialist is reached over A2A: a JSON-RPC
message/send envelope handed to InvokeAgentRuntime, which AgentCore passes
through to the peer's A2A server unmodified.

Why not point an A2A HTTP client at the peers directly? Two reasons. The runtime
path needs the ARN percent-encoded (a raw ARN 404s), and AgentCore authenticates
with SigV4 - a plain client sends nothing to sign with. Going through the API
gets both for free.
"""
import contextvars
import json
import os
import uuid

import boto3
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
SYSTEM_PROMPT = (
    "You coordinate a claims team. Delegate to the specialist agents: "
    "intake (read the document), policy (what's covered), fraud (risk), "
    "payout (submit it, and report a claim's recorded outcome). "
    "Gather their findings, then decide. "
    "Never pay out a claim the fraud agent flags as high risk. "
    "Report the payout exactly as the payout agent reports it: if it is pending human "
    "approval, say the payout is awaiting approval and that nothing has been paid. "
    "Never describe a claim as approved, paid or settled on your own authority."
)

# "intake=arn:...,policy=arn:..." - set by the stack.
PEER_ARNS = dict(
    pair.split("=", 1)
    for pair in os.environ.get("PEER_ARNS", "").split(",")
    if "=" in pair
)

agentcore = boto3.client("bedrock-agentcore")
app = BedrockAgentCoreApp()

# Peer calls for one claim share the orchestrator's session, without a global.
CURRENT_SESSION = contextvars.ContextVar("current_session", default="")


def _peer_session(base: str) -> str:
    """AgentCore requires a runtime session id of at least 33 characters."""
    session_id = base or uuid.uuid4().hex
    return session_id if len(session_id) >= 33 else f"{session_id}-{uuid.uuid4().hex}"


def _answer_text(body: dict) -> str:
    """Pull the text out of an A2A result, which may be a task or a message."""
    result = body.get("result") or {}
    if "error" in body:
        return f"peer error: {json.dumps(body['error'])}"
    parts = []
    for artifact in result.get("artifacts") or []:
        parts.extend(artifact.get("parts") or [])
    if not parts:
        parts = result.get("parts") or []
    text = "\n".join(part.get("text", "") for part in parts if part.get("kind") == "text")
    return text.strip() or json.dumps(result)[:2000]


def ask_peer(role: str, request: str) -> str:
    arn = PEER_ARNS.get(role)
    if not arn:
        return f"no runtime ARN configured for the {role} agent"
    envelope = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": uuid.uuid4().hex,
                "parts": [{"kind": "text", "text": request}],
            }
        },
    }
    response = agentcore.invoke_agent_runtime(
        agentRuntimeArn=arn,
        qualifier="DEFAULT",
        runtimeSessionId=_peer_session(CURRENT_SESSION.get()),
        payload=json.dumps(envelope).encode(),
    )
    return _answer_text(json.loads(response["response"].read()))



@tool
def ask_intake_agent(request: str) -> str:
    """Ask the Intake Agent to extract fields from a claim document in S3. Give it the S3 key."""
    return ask_peer("intake", request)


@tool
def ask_policy_agent(request: str) -> str:
    """Ask the Policy Agent which policy clauses apply to a claim, and to quote them."""
    return ask_peer("policy", request)


@tool
def ask_fraud_agent(request: str) -> str:
    """Ask the Fraud Agent to score a claim's risk. Give it the claimant id and the amount."""
    return ask_peer("fraud", request)


@tool
def ask_payout_agent(request: str) -> str:
    """Ask the Payout Agent to start a payout, or to report a claim's recorded outcome.

    For a payout give it the claim id, claimant email and amount. To check status, ask for
    the recorded outcome of a claim id such as CLM-2026-0077.
    """
    return ask_peer("payout", request)


@app.entrypoint
def invoke(payload, context):
    session_id = getattr(context, "session_id", "") or ""
    CURRENT_SESSION.set(session_id)
    agent = Agent(
        name="Claims Orchestrator",
        system_prompt=SYSTEM_PROMPT,
        model=BedrockModel(model_id=MODEL_ID, temperature=0),
        tools=[ask_intake_agent, ask_policy_agent, ask_fraud_agent, ask_payout_agent],
        trace_attributes={"session.id": session_id or "none"},
    )
    return {"answer": str(agent(payload.get("prompt", "")))}


if __name__ == "__main__":
    app.run()
