"""Distributed Map worker: one S3 document → chunks → Titan embeddings → S3 Vectors.

A DynamoDB ledger (key → ETag) makes re-runs cheap: unchanged objects are skipped
before any embedding call, so a full prefix re-scan costs one GetItem per file
instead of a Bedrock invocation per chunk.
"""
import os
import json
import time
import boto3

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")
s3v = boto3.client("s3vectors")
ddb = boto3.client("dynamodb")

VECTOR_BUCKET = os.environ["VECTOR_BUCKET"]
INDEX_NAME = os.environ["INDEX_NAME"]
LEDGER_TABLE = os.environ["LEDGER_TABLE"]
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
CHUNK = int(os.environ.get("CHUNK_CHARS", "1000"))
OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "150"))


def chunk_text(text: str):
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + CHUNK])
        i += CHUNK - OVERLAP
    return [c for c in out if c.strip()]


def embed(text: str):
    r = bedrock.invoke_model(modelId=EMBED_MODEL,
                             body=json.dumps({"inputText": text}))
    # Titan v2 returns 1024 floats. S3 Vectors requires float32.
    return [float(x) for x in json.loads(r["body"].read())["embedding"]]


def ledger_get(key: str):
    item = ddb.get_item(TableName=LEDGER_TABLE,
                        Key={"docKey": {"S": key}},
                        ConsistentRead=True).get("Item")
    if not item:
        return None, 0
    return item["etag"]["S"], int(item.get("chunks", {}).get("N", "0"))


def ledger_put(key: str, etag: str, chunks: int):
    ddb.put_item(TableName=LEDGER_TABLE, Item={
        "docKey": {"S": key},
        "etag": {"S": etag},
        "chunks": {"N": str(chunks)},
        "updatedAt": {"N": str(int(time.time()))},
    })


def handler(event, context):
    key = event.get("Key") or event["key"]
    bucket = event.get("Bucket") or event.get("BucketName") or os.environ["DOCS_BUCKET"]

    obj = s3.get_object(Bucket=bucket, Key=key)
    etag = obj["ETag"].strip('"')

    prev_etag, prev_chunks = ledger_get(key)
    if prev_etag == etag:
        return {"key": key, "status": "skipped", "chunks": prev_chunks}

    text = obj["Body"].read().decode("utf-8", errors="replace")
    chunks = chunk_text(text)

    vectors = [{
        "key": f"{key}#{i}",                      # deterministic: re-ingest overwrites
        "data": {"float32": embed(c)},
        "metadata": {"source": key, "chunk": i, "raw_text": c},
    } for i, c in enumerate(chunks)]

    for i in range(0, len(vectors), 100):          # PutVectors is batched
        s3v.put_vectors(vectorBucketName=VECTOR_BUCKET, indexName=INDEX_NAME,
                        vectors=vectors[i:i + 100])

    # A shrunken document leaves orphan chunks behind - delete the tail.
    if prev_chunks > len(chunks):
        stale = [f"{key}#{i}" for i in range(len(chunks), prev_chunks)]
        for i in range(0, len(stale), 100):
            s3v.delete_vectors(vectorBucketName=VECTOR_BUCKET, indexName=INDEX_NAME,
                               keys=stale[i:i + 100])

    ledger_put(key, etag, len(chunks))
    return {"key": key, "status": "ingested", "chunks": len(chunks)}