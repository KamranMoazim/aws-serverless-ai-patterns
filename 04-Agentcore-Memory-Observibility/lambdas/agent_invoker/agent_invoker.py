import os
import json
import uuid

import boto3

# Data-plane client for AgentCore Runtime invocation.
client = boto3.client("bedrock-agentcore")

RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
QUALIFIER = os.environ.get("ENDPOINT_QUALIFIER", "DEFAULT")


def handler(event, context):
    body = json.loads(event.get("body") or "{}")
    prompt = body.get("prompt", "")
    actor_id = body.get("actor_id", "anonymous")
    # Reuse the client's session_id across turns to keep memory continuity.
    # AgentCore requires a reasonably long session id; a uuid4 string (36 chars) is safe.
    session_id = body.get("session_id") or str(uuid.uuid4())

    resp = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        qualifier=QUALIFIER,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt, "actor_id": actor_id}).encode(),
    )

    # The agent streams; read the full body for this (buffered) demo endpoint.
    raw = resp["response"].read()
    answer = raw.decode("utf-8", errors="replace")

    return {
        "statusCode": 200,
        "headers": {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "*",
            "Content-Type": "application/json"
        },
        "body": json.dumps({"session_id": session_id, "answer": answer}),
    }