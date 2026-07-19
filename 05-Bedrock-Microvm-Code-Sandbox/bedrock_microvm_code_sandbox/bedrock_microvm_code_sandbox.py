from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_iam as iam,
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_apigateway as apigw,
)
from constructs import Construct

PROJECT_SLUG = "microvm-code-sandbox"


class BedrockMicrovmCodeSandboxStack(Stack):
    """
    User → API Gateway → Orchestrator Lambda
                           ├→ Bedrock (writes Python from the prompt)
                           └→ run-microvm → MicroVM sandbox (executes code, isolated)

    CDK owns the durable infra. The MicroVM image is built by scripts/build_image.sh
    (no CloudFormation resource for it yet), and the orchestrator drives the MicroVM
    lifecycle (run / token / terminate) at request time.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG

        # ── S3 bucket for the MicroVM image bundle (Dockerfile + app zip) ─────
        bundle_bucket = s3.Bucket(
            self, "BundleBucket",
            bucket_name=f"{slug}-micro-vm-code-bucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        # ── DynamoDB: session_id → MicroVM (id, endpoint) ─────────────────────
        sessions = dynamodb.Table(
            self, "Sessions",
            table_name=f"{slug}-sessions",
            partition_key=dynamodb.Attribute(name="session_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Execution role the MicroVM runs as (what the sandboxed code can do) ─
        # Kept minimal for the demo — no extra AWS access from inside the sandbox.
        microvm_exec_role = iam.Role(
            self, "MicrovmExecutionRole",
            role_name=f"{slug}-MicrovmExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),  # verify principal (see NOTES)
            description="Execution role assumed by the MicroVM sandbox",
        )
        bundle_bucket.grant_read(microvm_exec_role)

        microvm_exec_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=["arn:aws:logs:*:*:*"]
            )
        )
        boto3_1_44_43_layer = lambda_.LayerVersion(self, "Boto3-1.44.43", code=lambda_.Code.from_asset("lambda_layers/boto3_1_44_43/boto3_1_44_43.zip"))

        # ── Orchestrator Lambda ───────────────────────────────────────────────
        orchestrator = lambda_.Function(
            self, "Orchestrator",
            function_name=f"{slug}-orchestrator",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="orchestrator.handler",
            code=lambda_.Code.from_asset("lambdas/orchestrator"),
            timeout=Duration.seconds(90),
            environment={
                "SESSIONS_TABLE": sessions.table_name,
                "MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                # Filled in by scripts/build_image.sh after the image is built:
                "MICROVM_IMAGE_ARN": "",
                # "MICROVM_IMAGE_ARN": "arn:aws:lambda:us-east-1:767398137682:microvm-image:code-sandbox",
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
            layers=[boto3_1_44_43_layer]
        )
        sessions.grant_read_write_data(orchestrator)
        orchestrator.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=["*"],
        ))
        # MicroVM lifecycle permissions
        orchestrator.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "lambda:RunMicrovm",
                "lambda:CreateMicrovmAuthToken",
                "lambda:GetMicrovm",
                "lambda:SuspendMicrovm",
                "lambda:ResumeMicrovm",
                "lambda:TerminateMicrovm",
            ],
            resources=["*"],   # demo; scope to image/microvm ARNs in prod
        ))
        # Let the orchestrator pass the MicroVM execution role at run time.
        orchestrator.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[microvm_exec_role.role_arn],
        ))

        orchestrator.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:PassNetworkConnector"],
            resources=[
                "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS",
                "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS",
            ],
        ))

        # ── API Gateway (open, per the demo) → orchestrator ───────────────────
        api = apigw.RestApi(
            self, "Api",
            rest_api_name=f"{slug}-api",
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.REGIONAL]),
            deploy_options=apigw.StageOptions(stage_name="prod"),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
            ),
        )
        api.root.add_resource("run").add_method("POST", apigw.LambdaIntegration(orchestrator, proxy=True))

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "RunUrl", value=f"{api.url}run")
        CfnOutput(self, "BundleBucketName", value=bundle_bucket.bucket_name)
        CfnOutput(self, "OrchestratorFunction", value=orchestrator.function_name)
        CfnOutput(self, "MicrovmExecutionRoleArn", value=microvm_exec_role.role_arn)