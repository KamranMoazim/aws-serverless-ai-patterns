"""Query path: prompt → embed → S3 Vectors kNN → Bedrock answer (streamed).

Packaged as a FastAPI app behind the Lambda Web Adapter so the answer can stream
(same pattern as the response-streaming project).
"""
import os
import json

import boto3
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel

bedrock = boto3.client("bedrock-runtime")
s3v = boto3.client("s3vectors")

VECTOR_BUCKET = os.environ["VECTOR_BUCKET"]
INDEX_NAME = os.environ["INDEX_NAME"]
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
TOP_K = int(os.environ.get("TOP_K", "5"))

app = FastAPI()


class Ask(BaseModel):
    question: str


@app.get("/")
def root():
    return PlainTextResponse('POST /ask {"question": "..."} - RAG over S3 Vectors')


def retrieve(question: str):
    r = bedrock.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": question})
    )
    qvec = [float(x) for x in json.loads(r["body"].read())["embedding"]]

    res = s3v.query_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=INDEX_NAME,
        queryVector={"float32": qvec},
        topK=TOP_K,
        returnMetadata=True,
        returnDistance=True,
    )
    return res.get("vectors", [])


def answer_stream(question: str):
    hits = retrieve(question)
    context = "\n\n".join(
        f"[{h['metadata'].get('source')}#{h['metadata'].get('chunk')}] "
        f"{h['metadata'].get('raw_text', '')}"
        for h in hits
    )
    prompt = (
        "Answer the question using ONLY the context below. Cite sources like "
        "[file#chunk]. If the answer isn't in the context, say so.\n\n"
        f"Context:\n{context}\n\nQuestion: {question}"
    )
    resp = bedrock.converse_stream(
        modelId=CHAT_MODEL,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 1024},
    )
    for ev in resp["stream"]:
        d = ev.get("contentBlockDelta")
        if d and d["delta"].get("text"):
            yield d["delta"]["text"]


@app.post("/ask")
def ask(body: Ask):
    return StreamingResponse(
        answer_stream(body.question),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )