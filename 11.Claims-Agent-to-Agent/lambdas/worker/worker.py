"""Runs one claim through the orchestrator and records the result.

A four-agent flow takes ~45 seconds, well past API Gateway's 29 second
integration ceiling, so the HTTP request cannot wait for it. This runs behind an
async invoke and writes the answer to the jobs table for the client to poll.
"""
import json
import os
import time

import boto3

agentcore = boto3.client("bedrock-agentcore")
jobs = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
RUNTIME_ARN = os.environ["ORCHESTRATOR_ARN"]


def handler(event, context):
    job_id = event["job_id"]
    started = time.time()
    try:
        response = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            qualifier="DEFAULT",
            runtimeSessionId=event["session_id"],
            payload=json.dumps({"prompt": event["prompt"]}).encode(),
        )
        body = json.loads(response["response"].read())
        update = {
            "status": "DONE",
            "answer": body.get("answer", ""),
        }
    except Exception as error:
        update = {"status": "FAILED", "answer": f"{type(error).__name__}: {error}"}

    jobs.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, answer = :a, elapsed_seconds = :e",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": update["status"],
            ":a": update["answer"],
            ":e": int(time.time() - started),
        },
    )
    return {
        "job_id": job_id, 
        "status": update["status"]
    }
