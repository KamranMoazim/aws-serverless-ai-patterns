"""
AI Gateway chat handler (AppSync Events onPublish, invoked async via EVENT).

  1. user_id  ← Cognito JWT sub (AppSync already verified it; we just read the claim)
  2. meter    ← DynamoDB atomic ADD on (user_id, period) + TTL; over limit → 429 event
  3. generate ← Bedrock converse_stream
  4. stream   ← publish token chunks back to /chat/{user_id}/stream over AppSync HTTP
  5. usage    ← Firehose → S3 (Parquet) → Glue → Athena
"""
import os
import json
import time
import datetime
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

bedrock = boto3.client("bedrock-runtime")
ddb = boto3.client("dynamodb")
firehose = boto3.client("firehose")
session = boto3.Session()

USAGE_TABLE = os.environ["USAGE_TABLE"]
STREAM_NAME = os.environ["FIREHOSE_STREAM"]
EVENTS_HTTP = os.environ["EVENTS_HTTP_ENDPOINT"]   # e.g. abc123.appsync-api.us-east-1.amazonaws.com
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")
MONTHLY_TOKEN_LIMIT = int(os.environ.get("MONTHLY_TOKEN_LIMIT", "100000"))

# Rough per-1k-token pricing for the usage record (adjust to your model).
PRICE_IN_PER_1K = float(os.environ.get("PRICE_IN_PER_1K", "0.0008"))
PRICE_OUT_PER_1K = float(os.environ.get("PRICE_OUT_PER_1K", "0.004"))


# ── AppSync Events publish (SigV4-signed HTTP) ────────────────────────────────
def publish(channel: str, events: list):
    """Publish up to 5 events per call — AppSync's batch limit."""
    for i in range(0, len(events), 5):
        batch = events[i:i + 5]
        body = json.dumps({
            "channel": channel,
            "events": [json.dumps(e) for e in batch],   # each event must be a JSON string
        })
        url = f"https://{EVENTS_HTTP}/event"
        req = AWSRequest(
            method="POST", 
            url=url, 
            data=body,
            headers={"Content-Type": "application/json"}
        )
        SigV4Auth(
            session.get_credentials(), 
            "appsync", 
            REGION
        ).add_auth(req)
        urllib.request.urlopen(
            urllib.request.Request(
                url, 
                data=body.encode(),
                headers=dict(req.headers), 
                method="POST"
            ),
            timeout=10,
        )


# ── Metering ──────────────────────────────────────────────────────────────────
def _period() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def current_usage(user_id: str) -> int:
    r = ddb.get_item(
        TableName=USAGE_TABLE,
        Key={
            "user_id": {
                "S": user_id
            }, 
            "period": {
                "S": _period()
            }
        },
        ConsistentRead=True,
    )
    item = r.get("Item")
    return int(item["total_tokens"]["N"]) if item and "total_tokens" in item else 0


def add_usage(user_id: str, tokens_in: int, tokens_out: int):
    """Atomic ADD — safe under concurrent requests from the same tenant."""
    # TTL ~90 days after the period starts.
    ttl = int(time.time()) + 90 * 24 * 3600
    ddb.update_item(
        TableName=USAGE_TABLE,
        Key={"user_id": {"S": user_id}, "period": {"S": _period()}},
        UpdateExpression=(
            "ADD total_tokens :t, tokens_in :i, tokens_out :o, requests :r "
            "SET expires_at = if_not_exists(expires_at, :ttl)"
        ),
        ExpressionAttributeValues={
            ":t": {"N": str(tokens_in + tokens_out)},
            ":i": {"N": str(tokens_in)},
            ":o": {"N": str(tokens_out)},
            ":r": {"N": "1"},
            ":ttl": {"N": str(ttl)},
        },
    )


def log_usage(rec: dict):
    firehose.put_record(
        DeliveryStreamName=STREAM_NAME,
        Record={"Data": (json.dumps(rec) + "\n").encode()},
    )


# ── Handler ───────────────────────────────────────────────────────────────────
def handler(event, context):
    # AppSync Events DIRECT integration passes channel info + the published events.
    info = event.get("info", {})
    channel_path = info.get("channel", {}).get("path", "")

    # GUARD: we publish back into this same namespace. Without this check the
    # /stream publishes would re-trigger onPublish → infinite loop.
    if not channel_path.endswith("/prompt"):
        return {"events": event.get("events", [])}

    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    user_id = claims.get("sub", "anonymous")          # Cognito JWT sub = tenant id

    for ev in event.get("events", []):
        payload = ev.get("payload") or {}
        prompt = payload.get("prompt", "")
        req_id = payload.get("request_id", context.aws_request_id)
        out_channel = f"/chat/{user_id}/stream"

        # ── limit check (before spending a cent on Bedrock) ────────────────────
        used = current_usage(user_id)
        if used >= MONTHLY_TOKEN_LIMIT:
            publish(out_channel, [{
                "request_id": req_id, 
                "type": "error", 
                "code": 429,
                "message": f"Monthly token limit reached ({used}/{MONTHLY_TOKEN_LIMIT}).",
            }])
            continue

        # ── generate + stream tokens back over AppSync ─────────────────────────
        started = time.time()
        buf, tokens_in, tokens_out = [], 0, 0
        resp = bedrock.converse_stream(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 1024},
        )
        for chunk in resp["stream"]:
            if "contentBlockDelta" in chunk:
                text = chunk["contentBlockDelta"]["delta"].get("text")
                if text:
                    buf.append({
                        "request_id": req_id, 
                        "type": "token", 
                        "text": text
                    })
                    if len(buf) == 5:          # respect the 5-events-per-publish limit
                        publish(out_channel, buf)
                        buf = []
            if "metadata" in chunk:
                u = chunk["metadata"].get("usage", {})
                tokens_in = u.get("inputTokens", 0)
                tokens_out = u.get("outputTokens", 0)
        if buf:
            publish(out_channel, buf)

        # ── meter + emit the usage record ─────────────────────────────────────
        add_usage(user_id, tokens_in, tokens_out)
        cost = (tokens_in / 1000 * PRICE_IN_PER_1K) + (tokens_out / 1000 * PRICE_OUT_PER_1K)
        now = datetime.datetime.now(datetime.timezone.utc)
        log_usage({
            "user_id": user_id,
            "request_id": req_id,
            "model_id": MODEL_ID,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "total_tokens": tokens_in + tokens_out,
            "cost_usd": round(cost, 6),
            "latency_ms": int((time.time() - started) * 1000),
            "ts": now.isoformat(),
        })

        publish(out_channel, [{
            "request_id": req_id, 
            "type": "done",
            "tokens_in": tokens_in, 
            "tokens_out": tokens_out,
            "cost_usd": round(cost, 6),
            "remaining": max(0, MONTHLY_TOKEN_LIMIT - (used + tokens_in + tokens_out)),
        }])

    return {"events": []}   # nothing further to broadcast on the prompt channel