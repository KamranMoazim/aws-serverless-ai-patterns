#!/usr/bin/env python3
import aws_cdk as cdk
import os

from multi_tenant_mcp_gateway.multi_tenant_mcp_gateway_stack import MultiTenantMCPGatewayStack


app = cdk.App()

MultiTenantMCPGatewayStack(
    app,
    "MultiTenantMCPGatewayStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()