from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_iam as iam,
    aws_ecr as ecr,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_apigateway as apigw,
    aws_bedrockagentcore as agentcore,
)
from constructs import Construct

PROJECT_SLUG = "agentcore-support-agent"


class AgentCoreSupportAgentStack(Stack):
    """
    User → API Gateway → Lambda (invoker) → AgentCore Runtime (Strands agent)
                                               ├→ Bedrock (model)
                                               ├⇄ AgentCore Memory (short + long term)
                                               └→ Observability (ADOT → CloudWatch)

    Runtime uses default IAM auth (no Cognito) — the Lambda's role invokes it.
    Memory built-in strategies (semantic/summary/preference) give the agent
    cross-session recall keyed by actor_id + session_id.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG
        model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

        # ── AgentCore Memory (short-term events + 3 long-term strategies) ─────
        # expiration_duration takes a Duration; the L2 converts to the seconds
        # that AWS::BedrockAgentCore::Memory expects. The memory execution role
        # (for the built-in strategies' Bedrock extraction) is created for us.
        memory = agentcore.Memory(
            self, "SupportMemory",
            memory_name="support_agent_memory",
            description="Support agent memory: facts, preferences, summaries.",
            expiration_duration=Duration.days(7),   # short-term event expiry
            memory_strategies=[
                agentcore.MemoryStrategy.using_built_in_semantic(),
                agentcore.MemoryStrategy.using_built_in_summarization(),
                agentcore.MemoryStrategy.using_built_in_user_preference(),
            ],
        )

        # ── AgentCore Runtime (the Strands agent container from ECR) ──────────
        # Build + push the arm64 image to this repo first (see README).
        repo = ecr.Repository.from_repository_name(self, "AgentRepo", "support-agent")
        artifact = agentcore.AgentRuntimeArtifact.from_ecr_repository(repo, "latest")

        runtime = agentcore.Runtime(
            self, "AgentRuntime",
            agent_runtime_artifact=artifact,
            # no authorizer_configuration -> default IAM (SigV4) auth
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            environment_variables={
                "MEMORY_ID": memory.memory_id,
                "MODEL_ID": model_id,
                # Activates the ADOT observability pipeline. CLI-deployed runtimes
                # get this automatically; IaC-deployed ones must set it explicitly.
                "AGENT_OBSERVABILITY_ENABLED": "true",
            },
        )

        # The agent (inside the Runtime) needs Bedrock + Memory + tracing perms.
        runtime.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=["*"],   # demo; scope to model ARNs in prod
        ))
        memory.grant_read(runtime)
        memory.grant_write(runtime)
        runtime.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "xray:PutTraceSegments", "xray:PutTelemetryRecords",
                "cloudwatch:PutMetricData",
            ],
            resources=["*"],
        ))

        # ── Lambda invoker: API Gateway → InvokeAgentRuntime ──────────────────
        agent_invoker = lambda_.Function(
            self, "Invoker",
            function_name=f"{slug}-invoker",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="agent_invoker.handler",
            code=lambda_.Code.from_asset("lambdas/agent_invoker"),
            timeout=Duration.seconds(60),
            environment={"AGENT_RUNTIME_ARN": runtime.agent_runtime_arn},
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        # Let the Lambda's role invoke the runtime (this is the only "auth").
        runtime.grant_invoke(agent_invoker)

        # ── API Gateway (open, per the demo) → Lambda proxy ───────────────────
        api = apigw.RestApi(
            self, "Api",
            rest_api_name=f"{slug}-api",
            description="Support agent — API Gateway → Lambda → AgentCore Runtime",
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.REGIONAL],
            ),
            deploy_options=apigw.StageOptions(stage_name="prod"),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=apigw.Cors.DEFAULT_HEADERS + [
                    "*",
                ],
                allow_credentials=False,  # Must be False when using ALL_ORIGINS
                max_age=Duration.days(10),
            ),
        )
        api.root.add_resource("chat").add_method(
            "POST", apigw.LambdaIntegration(agent_invoker, proxy=True),
        )

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "ChatUrl", value=f"{api.url}chat")
        CfnOutput(self, "AgentRuntimeArn", value=runtime.agent_runtime_arn)
        CfnOutput(self, "MemoryId", value=memory.memory_id)