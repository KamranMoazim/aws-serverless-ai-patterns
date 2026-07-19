"""
Orchestrator: API Gateway -> here.
  1. Bedrock writes Python from the user's prompt.
  2. Get-or-run a MicroVM for this session (stored in DynamoDB).
  3. Mint a short-lived auth token, POST the code to the MicroVM's endpoint.
  4. Return {code, result}.

Uses urllib (not requests) so the Lambda needs no extra dependency.
"""
import os
import json
import time
import uuid
import urllib.request
import urllib.error

import boto3

bedrock = boto3.client("bedrock-runtime")
micro = boto3.client("lambda-microvms")   # NOTE: needs a recent boto3 (see NOTES)
table = boto3.resource("dynamodb").Table(os.environ["SESSIONS_TABLE"])

IMAGE_ARN = os.environ.get("MICROVM_IMAGE_ARN", "")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

CODE_SYSTEM = (
    "You write short, self-contained Python 3 scripts that print their result to "
    "stdout. Output ONLY the code — no markdown fences, no prose, no explanation."
)


def _generate_code(prompt: str) -> str:
    r = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": CODE_SYSTEM}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 800, "temperature": 0},
    )
    text = r["output"]["message"]["content"][0]["text"]
    # Strip accidental markdown fences just in case.
    return text.replace("```python", "").replace("```", "").strip()


def _get_or_run_microvm(session_id: str):
    item = table.get_item(Key={"session_id": session_id}).get("Item")
    if item and item.get("microvm_id"):
        return item["microvm_id"], item["endpoint"]
    run = micro.run_microvm(
        imageIdentifier=IMAGE_ARN,
        ingressNetworkConnectors=["arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS"],
        egressNetworkConnectors=["arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS"],
        idlePolicy={
            "autoResumeEnabled": True,     # a request wakes a suspended VM
            "maxIdleDurationSeconds": 300,  # suspend after 5 min idle
            "suspendedDurationSeconds": 1800,  # terminate after 30 min suspended
        },
    )
    mid, endpoint = run["microvmId"], run["endpoint"]
    table.put_item(Item={"session_id": session_id, "microvm_id": mid, "endpoint": endpoint})
    return mid, endpoint


def _execute(microvm_id: str, endpoint: str, code: str, retries: int = 5) -> dict:
    token = micro.create_microvm_auth_token(
        microvmIdentifier=microvm_id,
        expirationInMinutes=10,
        allowedPorts=[{"allPorts": {}}],
    )["authToken"]["X-aws-proxy-auth"]

    req = urllib.request.Request(
        f"https://{endpoint}/execute",
        data=json.dumps({"code": code}).encode(),
        headers={"Content-Type": "application/json", "X-aws-proxy-auth": token},
        method="POST",
    )

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 502 and attempt < retries - 1:
                time.sleep(2 ** attempt)  # 1, 2, 4, 8...
                continue
            raise


def handler(event, context):
    print("boto3 version:", boto3.__version__)
    body = json.loads(event.get("body") or "{}")
    prompt = body.get("prompt", "")
    session_id = body.get("session_id") or str(uuid.uuid4())

    code = _generate_code(prompt)
    microvm_id, endpoint = _get_or_run_microvm(session_id)

    try:
        result = _execute(microvm_id, endpoint, code)
    except (urllib.error.URLError, KeyError):
        # Stored VM was terminated — drop it and run a fresh one once.
        table.delete_item(Key={"session_id": session_id})
        microvm_id, endpoint = _get_or_run_microvm(session_id)
        result = _execute(microvm_id, endpoint, code)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"session_id": session_id, "code": code, "result": result}),
    }