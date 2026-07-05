from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_ecr as ecr,
    aws_apigateway as apigw,
)
from constructs import Construct

PROJECT_SLUG = "response-streaming-ai"


class ResponseStreamingAIAnswerStack(Stack):
    """
    Client → API Gateway REST (responseTransferMode=STREAM)
          → Lambda (FastAPI + Web Adapter)
          → Bedrock (converse_stream) → tokens streamed back

    API Gateway REST added response streaming in Nov 2025: a Lambda *proxy*
    integration with responseTransferMode=STREAM, invoked via InvokeWithResponseStream.
    The Web Adapter streams the same way it does behind a Function URL, so the
    container/env are unchanged — only the front door swaps from Function URL to API GW.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG

        # ── Lambda from the image you built + pushed to ECR ───────────────────
        repository = ecr.Repository.from_repository_name(
            self, "StreamingApiRepo", "streaming-lambda-api",
        )

        fn = lambda_.DockerImageFunction(
            self, "StreamingApi",
            function_name=f"{slug}-api",
            code=lambda_.DockerImageCode.from_ecr(
                repository=repository,
                tag_or_digest="latest",   # or a specific tag / image digest
            ),
            memory_size=512,
            timeout=Duration.seconds(120),   # streaming answers can run a while
            environment={
                # Unchanged: LWA streams via the same InvokeWithResponseStream path
                # that API Gateway uses. Must be RESPONSE_STREAM.
                "AWS_LWA_INVOKE_MODE": "RESPONSE_STREAM",
                "MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            },
        )

        # Bedrock streaming permission (Converse maps onto these actions).
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:InvokeModel",
            ],
            resources=["*"],   # demo; scope to specific model ARNs in production
        ))

        # ── API Gateway REST with response streaming ──────────────────────────
        # REGIONAL endpoint → 5-min idle timeout for streams (edge-optimized is
        # only 30s, which can cut long generations).
        api = apigw.RestApi(
            self, "StreamingApi_Api",
            rest_api_name=f"{slug}-api",
            description="Streaming Bedrock answers via API Gateway REST",
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.REGIONAL],
            ),
            deploy_options=apigw.StageOptions(stage_name="prod"),
        )

        # Streaming REQUIRES a Lambda PROXY integration + responseTransferMode=STREAM.
        integration = apigw.LambdaIntegration(
            fn,
            proxy=True,
            response_transfer_mode=apigw.ResponseTransferMode.STREAM,
            # For long generations, raise the integration timeout (streaming allows
            # up to 15 min). Default 29s is fine when tokens start quickly.
            # timeout=Duration.minutes(2),
        )

        # POST /ask  → matches the FastAPI route in app/main.py
        ask = api.root.add_resource("ask")
        ask.add_method("POST", integration)

        CfnOutput(self, "ApiBaseUrl", value=api.url)
        CfnOutput(self, "AskUrl", value=f"{api.url}ask")
