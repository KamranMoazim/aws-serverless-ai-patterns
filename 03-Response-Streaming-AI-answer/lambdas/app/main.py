import os
import json

import boto3
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel

# Model access must be enabled in the Bedrock console for this ID/region.
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

app = FastAPI()
bedrock = boto3.client("bedrock-runtime")


class Ask(BaseModel):
    prompt: str


@app.get("/")
def root():
    return PlainTextResponse(
        "POST /ask  {\"prompt\": \"...\"}  — streams a Bedrock answer token by token"
    )


def token_stream(prompt: str):
    """Yield text deltas from Bedrock as they arrive (model-agnostic Converse API)."""
    resp = bedrock.converse_stream(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.7},
    )
    for event in resp["stream"]:
        delta = event.get("contentBlockDelta")
        if delta:
            text = delta["delta"].get("text")
            if text:
                yield text


@app.post("/ask")
def ask(body: Ask):
    # text/plain + a sync generator = a chunked HTTP stream the adapter forwards.
    return StreamingResponse(token_stream(body.prompt), media_type="text/plain")