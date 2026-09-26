#!/usr/bin/env python3
import aws_cdk as cdk
import os

from video_kb.video_kb_stack import VideoKbStack


app = cdk.App()

VideoKbStack(
    app,
    "VideoKbStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()