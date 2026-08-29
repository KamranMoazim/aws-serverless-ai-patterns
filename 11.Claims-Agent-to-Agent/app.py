#!/usr/bin/env python3
import aws_cdk as cdk
import os

from claims_a2a.claims_a2a_stack import ClaimsA2AStack


app = cdk.App()

ClaimsA2AStack(
    app,
    "ClaimsA2AStack",
    sender_email=os.environ.get("SENDER_EMAIL", "kamrannaseer765@gmail.com"),
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()