#!/usr/bin/env python3
import aws_cdk as cdk
import os

from private_api_agent.private_api_agent_stack import PrivateApiAgentStack

app = cdk.App()

PrivateApiAgentStack(
    app,
    "PrivateApiAgentStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()