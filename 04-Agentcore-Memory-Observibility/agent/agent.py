"""
Customer-support agent: Strands + AgentCore Runtime + AgentCore Memory + Observability.

- BedrockAgentCoreApp is the Runtime entrypoint (serves /invocations + /ping on 8080).
- AgentCoreMemorySessionManager gives the agent short-term (raw turns) and long-term
  (facts / preferences / summaries) memory, keyed by actor_id + session_id.
- trace_attributes put session.id on every OTEL span, so the CloudWatch GenAI
  Observability dashboard can group a whole conversation by session.
"""
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from strands import Agent, tool
from strands.models import BedrockModel

app = BedrockAgentCoreApp()

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
MEMORY_ID = os.environ.get("MEMORY_ID")               # injected by the stack
REGION = os.environ.get("AWS_REGION", "us-east-1")

SYSTEM_PROMPT = (
    "You are a friendly customer-support agent. Use the customer's remembered "
    "preferences and past context when helpful. If you need an order's status, "
    "use the get_order_status tool. Keep answers concise."
)


@tool
def get_order_status(order_id: str) -> str:
    """Look up the current status of a customer order by its order ID."""
    # Demo stub — replace with a real lookup (DynamoDB, an API, etc.).
    demo = {
        "A1001": "shipped — arriving tomorrow",
        "A1002": "processing — leaves the warehouse today",
        "A1003": "delivered on 2026-06-30",
    }
    return demo.get(order_id, f"No order found with ID {order_id}.")


def _session_manager(session_id: str, actor_id: str):
    """Wire AgentCore Memory into Strands. Returns None if no memory is configured."""
    if not MEMORY_ID:
        return None
    # Pull the three long-term namespaces this actor/session; top-k semantic recall.
    retrieval = {
        f"/facts/{actor_id}": RetrievalConfig(top_k=3, relevance_score=0.5),
        f"/preferences/{actor_id}": RetrievalConfig(top_k=3, relevance_score=0.5),
        f"/summaries/{actor_id}/{session_id}": RetrievalConfig(top_k=3, relevance_score=0.5),
    }
    return AgentCoreMemorySessionManager(
        AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            session_id=session_id,
            actor_id=actor_id,
            retrieval_config=retrieval,
        ),
        REGION,
    )


def _ctx_value(context, key, default):
    """AgentCore passes context as an object or dict depending on SDK version."""
    if context is None:
        return default
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default)


@app.entrypoint
async def invoke(payload, context):
    session_id = _ctx_value(context, "session_id", "no-session")
    request_id = _ctx_value(context, "request_id", "no-request")
    actor_id = payload.get("actor_id", "anonymous")
    prompt = payload.get("prompt", "")

    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=SYSTEM_PROMPT,
        tools=[get_order_status],
        session_manager=_session_manager(session_id, actor_id),
        # These land on every span → filterable in the GenAI Observability dashboard.
        trace_attributes={
            "session.id": session_id,       # OTEL semantic convention (dot, not underscore)
            "request.id": request_id,
            "actor.id": actor_id,
            "agent.name": "support-agent",
            "agent.version": "1.0.0",
        },
    )

    # Stream tokens back through the Runtime to the caller.
    async for event in agent.stream_async(prompt):
        if "data" in event and isinstance(event["data"], str):
            yield event["data"]


if __name__ == "__main__":
    app.run()   # serves on 0.0.0.0:8080 for AgentCore Runtime
