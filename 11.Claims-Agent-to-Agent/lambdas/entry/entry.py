"""API Gateway front door.

POST /claim            start a claim, get a job id back
GET  /claim/{job_id}   poll for the result

The work is dispatched to a worker Lambda because the agent flow outlives
API Gateway's 29 second integration timeout.
"""
import json
import os
import time
import uuid

import boto3

lambda_client = boto3.client("lambda")
jobs = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
WORKER_FN = os.environ["WORKER_FN"]
TTL_HOURS = 24

CORS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


def reply(status, body):
    return {
        "statusCode": status, 
        "headers": CORS, 
        "body": json.dumps(body, default=str)
    }


def start(event):
    body = json.loads(event.get("body") or "{}")
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return reply(400, {"error": "prompt is required"})

    job_id = str(uuid.uuid4())
    # AgentCore requires a runtime session id of at least 33 characters.
    session_id = body.get("session_id") or f"{job_id}-{uuid.uuid4().hex[:8]}"
    jobs.put_item(Item={
        "job_id": job_id,
        "status": "RUNNING",
        "prompt": prompt,
        "session_id": session_id,
        "expires_at": int(time.time()) + TTL_HOURS * 3600,
    })
    lambda_client.invoke(
        FunctionName=WORKER_FN,
        InvocationType="Event",
        Payload=json.dumps({"job_id": job_id, "prompt": prompt, "session_id": session_id}).encode(),
    )
    return reply(202, {"job_id": job_id, "status": "RUNNING", "session_id": session_id})


def poll(job_id):
    item = jobs.get_item(Key={"job_id": job_id}).get("Item")
    if not item:
        return reply(404, {"error": "unknown job_id"})
    item.pop("expires_at", None)
    return reply(200, item)


def handler(event, context):
    method = event.get("httpMethod", "POST")
    job_id = (event.get("pathParameters") or {}).get("job_id")
    if method == "GET" and job_id:
        return poll(job_id)
    if method == "POST":
        return start(event)
    return reply(405, {"error": f"{method} not supported"})
