#!/usr/bin/env python3
import aws_cdk as cdk
import os

from bedrock_microvm_code_sandbox.bedrock_microvm_code_sandbox import BedrockMicrovmCodeSandboxStack


app = cdk.App()

BedrockMicrovmCodeSandboxStack(
    app,
    "BedrockMicrovmCodeSandboxStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()