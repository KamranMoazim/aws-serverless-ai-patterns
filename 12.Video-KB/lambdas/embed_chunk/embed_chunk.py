"""Distributed Map worker: one transcript chunk → embedding → S3 Vectors.

THE key idea: every vector carries its start/end TIMESTAMP as metadata, so a
semantic hit resolves to a moment in the video - not just to some text.
"""
import os
import json
import boto3

bedrock = boto3.client("bedrock-runtime")
s3v = boto3.client("s3vectors")

VECTOR_BUCKET = os.environ["VECTOR_BUCKET"]
INDEX_NAME = os.environ["INDEX_NAME"]
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")


def embed(text: str):
    r = bedrock.invoke_model(modelId=EMBED_MODEL, body=json.dumps({"inputText": text}))
    return [float(x) for x in json.loads(r["body"].read())["embedding"]]


def handler(event, context):
    chunk = event.get("chunk", event)
    s3v.put_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=INDEX_NAME,
        vectors=[{
            "key": f"{chunk['video_id']}#{chunk['idx']}",   # idempotent re-ingest
            "data": {"float32": embed(chunk["text"])},
            "metadata": {
                "video_id": chunk["video_id"],
                "start": float(chunk["start"]),   # ← the deep-link timestamp
                "end": float(chunk["end"]),
                "speaker": chunk.get("speaker", ""),
                "raw_text": chunk["text"],
            },
        }],
    )
    return {"key": f"{chunk['video_id']}#{chunk['idx']}"}
