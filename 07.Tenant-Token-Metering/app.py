#!/usr/bin/env python3
import aws_cdk as cdk
import os

from tenant_gateway_metering.tenant_gateway_metering_stack import TenantGatewayMeteringStack


app = cdk.App()

TenantGatewayMeteringStack(
    app,
    "TenantGatewayMeteringStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()