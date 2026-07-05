#!/usr/bin/env python3
import aws_cdk as cdk
import os

# from response_streaming_ai_answer.response_streaming_api_gw_ai_answer_stack import ResponseStreamingAPIGWAIAnswerStack
from response_streaming_ai_answer_stack.response_streaming_ai_answer_stack import ResponseStreamingAIAnswerStack


app = cdk.App()

ResponseStreamingAIAnswerStack(
    app,
    "ResponseStreamingAIAnswerStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()