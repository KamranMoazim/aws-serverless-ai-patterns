"""Bedrock: transcript → chapter titles + summary → DynamoDB."""
import os
import json
import boto3

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")
table = boto3.resource("dynamodb").Table(os.environ["VIDEOS_TABLE"])

TRANSCRIPTS_BUCKET = os.environ["TRANSCRIPTS_BUCKET"]
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

SYSTEM = ("You summarize videos from their transcript. Return ONLY JSON: "
          '{"summary": "...", "chapters": [{"title": "...", "start": <seconds>}]} '
          "with 3-8 chapters. Use the timestamps given.")


def handler(event, context):
    video_id = event["video_id"]
    chunks = json.loads(s3.get_object(
        Bucket=TRANSCRIPTS_BUCKET, Key=event["chunks_key"])["Body"].read())

    # Timestamped text in = real chapter marks out.
    transcript = "\n".join(f"[{int(c['start'])}s] {c['text']}" for c in chunks)[:60000]

    r = bedrock.converse(
        modelId=MODEL_ID, system=[{"text": SYSTEM}],
        messages=[{"role": "user", "content": [{"text": transcript}]}],
        inferenceConfig={"maxTokens": 1500, "temperature": 0.2})
    text = r["output"]["message"]["content"][0]["text"]
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"summary": text, "chapters": []}

    table.put_item(Item={
        "video_id": video_id,
        "summary": parsed.get("summary", ""),
        "chapters": json.dumps(parsed.get("chapters", [])),
        "chunk_count": len(chunks),
        "status": "READY",
    })
    return {"video_id": video_id, "chapters": len(parsed.get("chapters", []))}
