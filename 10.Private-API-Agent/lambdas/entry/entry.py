"""Private API Gateway → here → AgentCore Runtime. Lambda is in the VPC too."""
import os
import json
import uuid

import boto3

agentcore = boto3.client("bedrock-agentcore")
RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]


def handler(event, context):
    body = json.loads(event.get("body") or "{}")
    session_id = body.get("session_id") or str(uuid.uuid4())
    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        qualifier="DEFAULT",
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": body.get("prompt", "")}).encode(),
    )
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"session_id": session_id, "answer": resp["response"].read().decode()})
    }
