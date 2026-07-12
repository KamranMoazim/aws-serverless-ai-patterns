#!/usr/bin/env python3
import aws_cdk as cdk
import os

from agentcore_support_agent.agentcore_support_agent_stack import AgentCoreSupportAgentStack


app = cdk.App()

AgentCoreSupportAgentStack(
    app,
    "AgentCoreSupportAgentStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()