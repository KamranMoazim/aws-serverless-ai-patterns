"""Semantic search → timestamped moments + CloudFront deep links."""
import os
import json
import boto3

bedrock = boto3.client("bedrock-runtime")
s3v = boto3.client("s3vectors")

VECTOR_BUCKET = os.environ["VECTOR_BUCKET"]
INDEX_NAME = os.environ["INDEX_NAME"]
CDN = os.environ["CDN_DOMAIN"]
THUMB_INTERVAL = int(os.environ.get("THUMB_INTERVAL", "30"))
THUMB_MAX = int(os.environ.get("THUMB_MAX", "20"))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

HEADERS = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def thumbnail(video_id: str, start: int) -> str:
    # MediaConvert numbers frame captures from 0, one every THUMB_INTERVAL seconds,
    # so the capture nearest a moment is a plain division.
    n = min(THUMB_MAX - 1, start // THUMB_INTERVAL)
    return f"https://{CDN}/{video_id}/thumb.{n:07d}.jpg"


def handler(event, context):
    question = json.loads(event.get("body") or "{}").get("question", "").strip()
    if not question:
        return {"statusCode": 400, "headers": HEADERS, "body": json.dumps({"error": "question is required"})}

    e = bedrock.invoke_model(modelId=EMBED_MODEL, body=json.dumps({"inputText": question}))
    vec = [float(x) for x in json.loads(e["body"].read())["embedding"]]

    res = s3v.query_vectors(
        vectorBucketName=VECTOR_BUCKET, 
        indexName=INDEX_NAME,
        queryVector={"float32": vec}, 
        topK=5,
        returnMetadata=True, 
        returnDistance=True
    )

    moments = []
    for v in res.get("vectors", []):
        m = v["metadata"]
        start = int(float(m["start"]))
        moments.append({
            "video_id": m["video_id"],
            "start": start,
            "end": int(float(m["end"])),
            "text": m.get("raw_text", ""),
            "distance": v.get("distance"),
            # THE payoff: opens the video at the exact moment.
            "clip_url": f"https://{CDN}/{m['video_id']}/hls.m3u8#t={start}",
            "thumbnail": thumbnail(m["video_id"], start),
        })

    if not moments:
        return {"statusCode": 200, "headers": HEADERS,
                "body": json.dumps({"answer": "No indexed video covers that yet.",
                                    "moments": []})}

    ctx = "\n\n".join(f"[{h['video_id']} @{h['start']}s] {h['text']}" for h in moments)
    r = bedrock.converse(
        modelId=CHAT_MODEL,
        messages=[{"role": "user", "content": [{"text":
            "Answer using ONLY the transcript excerpts. Cite [video @time].\n\n"
            f"{ctx}\n\nQuestion: {question}"}]}],
        inferenceConfig={"maxTokens": 600})

    return {
        "statusCode": 200, 
        "headers": HEADERS,
        "body": json.dumps({"answer": r["output"]["message"]["content"][0]["text"],"moments": moments})
    }
