"""Policy Agent - an independent A2A service.

Semantic search over the policy documents in S3 Vectors. Its IAM role grants
s3vectors:QueryVectors and nothing else - a prompt injection cannot make it pay.
"""
import json
import os

import boto3
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent.a2a import A2AServer

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
VECTOR_BUCKET = os.environ["VECTOR_BUCKET"]
INDEX_NAME = os.environ["INDEX_NAME"]
SYSTEM_PROMPT = "You find the policy clauses that apply to a claim and quote them."

bedrock = boto3.client("bedrock-runtime")
s3vectors = boto3.client("s3vectors")


@tool
def search_policy_documents(question: str) -> str:
    """Semantic search over policy documents to find the clauses that apply."""
    embedding_response = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps({"inputText": question}),
    )
    query_vector = [float(value) for value in json.loads(embedding_response["body"].read())["embedding"]]
    results = s3vectors.query_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=INDEX_NAME,
        queryVector={"float32": query_vector},
        topK=4,
        returnMetadata=True,
    )
    return json.dumps([
        {
            "source": vector["metadata"].get("source"), 
            "text": vector["metadata"].get("raw_text", "")
        }
        for vector in results.get("vectors", [])
    ])


agent = Agent(
    name="Policy Agent",
    description=SYSTEM_PROMPT,
    system_prompt=SYSTEM_PROMPT,
    model=BedrockModel(model_id=MODEL_ID, temperature=0),
    tools=[search_policy_documents],
)

# A2A contract: stateless HTTP on 0.0.0.0:9000, agent card at
# /.well-known/agent-card.json - AgentCore proxies JSON-RPC straight through.
server = A2AServer(agent=agent, host="0.0.0.0", port=9000)

if __name__ == "__main__":
    server.serve()
