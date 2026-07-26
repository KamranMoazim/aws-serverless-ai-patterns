#!/usr/bin/env python3
import aws_cdk as cdk
import os

from durable_lambda_order_approval_stack.durable_lambda_order_approval_stack import DurableLambdaOrderApprovalStack


app = cdk.App()

DurableLambdaOrderApprovalStack(
    app,
    "DurableLambdaOrderApprovalStack",
    sender_email=os.environ.get("SENDER_EMAIL", "you@example.com"),  # must be SES-verified
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()