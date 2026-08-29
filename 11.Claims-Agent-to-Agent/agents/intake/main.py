"""Intake Agent - an independent A2A service.

Reads claim documents with Textract. Its IAM role grants Textract and the docs
bucket, and nothing else - it cannot query policies or move money.
"""
import json
import os

import boto3
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent.a2a import A2AServer

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
DOCS_BUCKET = os.environ["DOCS_BUCKET"]
SYSTEM_PROMPT = "You extract structured data from claim documents."

textract = boto3.client("textract")


@tool
def extract_claim_document(s3_key: str) -> str:
    """Extract text and form fields from a claim document stored in S3."""
    response = textract.analyze_document(
        Document={"S3Object": {"Bucket": DOCS_BUCKET, "Name": s3_key}},
        FeatureTypes=["FORMS"],
    )
    lines = [block["Text"] for block in response["Blocks"] if block["BlockType"] == "LINE"]
    return json.dumps({"s3_key": s3_key, "text": "\n".join(lines)})


agent = Agent(
    name="Intake Agent",
    description=SYSTEM_PROMPT,
    system_prompt=SYSTEM_PROMPT,
    model=BedrockModel(model_id=MODEL_ID, temperature=0),
    tools=[extract_claim_document],
)

# A2A contract: stateless HTTP on 0.0.0.0:9000, agent card at
# /.well-known/agent-card.json - AgentCore proxies JSON-RPC straight through.
server = A2AServer(agent=agent, host="0.0.0.0", port=9000)

if __name__ == "__main__":
    server.serve()
