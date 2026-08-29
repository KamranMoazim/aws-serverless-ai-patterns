import os

from aws_cdk import (
    Stack, Duration, RemovalPolicy, CfnOutput, Fn,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3vectors as s3vectors,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_apigateway as apigw,
    aws_ecr as ecr,
    aws_bedrockagentcore as agentcore,
)
from constructs import Construct

PROJECT_SLUG = "claims-a2a"
SPECIALISTS = ["intake", "policy", "fraud", "payout"]
# Shared so the payout agent describes the same threshold the durable function enforces.
AUTO_APPROVE_UNDER = "5000"

# Written by scripts/build_agents*.sh after a successful push, so `cdk deploy`
# defaults to the tag that was actually built instead of a "latest" nobody pushed.
TAG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".image-tags")


def _recorded_tags() -> dict:
    tags = {}
    try:
        with open(TAG_FILE) as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    name, _, value = line.partition("=")
                    tags[name.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return tags


RECORDED_TAGS = _recorded_tags()


class ClaimsA2AStack(Stack):
    """
    User → API GW → Entry Lambda ──(202 job_id)──> client polls GET /claim/{job_id}
                     │
                     └─async→ Worker → Orchestrator Agent (AgentCore Runtime)
                                        ├─A2A→ Intake Agent  → Textract → S3
                                        ├─A2A→ Policy Agent  → S3 Vectors (policy docs)
                                        ├─A2A→ Fraud Agent   → Lambda → DynamoDB (rules/history)
                                        └─A2A→ Payout Agent  → Lambda (durable fn) → approval → SES
                                      → AgentCore Evaluations → CloudWatch

    Five runtimes, five images: agents/<name> builds claims-a2a-<name>. Each agent
    is independently deployable, with its own IAM role, env, and release cadence.
    """

    def __init__(self, scope: Construct, construct_id: str, sender_email: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG
        STAGE = "prod"

        # ── Data ──────────────────────────────────────────────────────────────
        docs = s3.Bucket(self, "Docs", 
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL
        )
        history = dynamodb.Table(
            self, "History",
            partition_key=dynamodb.Attribute(name="claimant_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY
        )
        claims = dynamodb.Table(
            self, "Claims",
            partition_key=dynamodb.Attribute(name="claim_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY
        )

        jobs = dynamodb.Table(
            self, "Jobs",
            partition_key=dynamodb.Attribute(name="job_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY
        )

        # Policy documents live in S3 Vectors (from the RAG build).
        vbucket = s3vectors.CfnVectorBucket(self, "PolicyVectors")
        index = s3vectors.CfnIndex(
            self, "PolicyIndex",
            vector_bucket_arn=vbucket.attr_vector_bucket_arn,
            index_name=f"{slug}-policies",
            data_type="float32", 
            dimension=1024, 
            distance_metric="cosine",
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(non_filterable_metadata_keys=["raw_text"])
        )
        index.add_dependency(vbucket)
        vector_bucket_name = Fn.select(1, Fn.split("bucket/", vbucket.attr_vector_bucket_arn))

        # ── API for the durable payout approval links ─────────────────────────
        api = apigw.RestApi(
            self, "Api", 
            rest_api_name=f"{slug}-api",
            endpoint_configuration=apigw.EndpointConfiguration(types=[apigw.EndpointType.REGIONAL]),
            deploy_options=apigw.StageOptions(stage_name=STAGE)
        )
        approve_url = (f"https://{api.rest_api_id}.execute-api.{self.region}.amazonaws.com/{STAGE}/approve")

        # ── Fraud Lambda (deterministic rules) ────────────────────────────────
        fraud_fn = lambda_.Function(
            self, "Fraud",
            function_name=f"{slug}-fraud",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="fraud.handler",
            code=lambda_.Code.from_asset("lambdas/fraud"),
            timeout=Duration.seconds(30),
            environment={
                "HISTORY_TABLE": history.table_name
            },
            log_retention=logs.RetentionDays.ONE_WEEK
        )
        history.grant_read_write_data(fraud_fn)

        # ── Payout: a DURABLE function (suspends for human approval) ──────────
        durable_sdk_layer = lambda_.LayerVersion(
            self, "DurableSdkLayer",
            layer_version_name=f"{slug}-durable-sdk",
            code=lambda_.Code.from_asset("layers/durable-sdk-layer.zip"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
            compatible_architectures=[lambda_.Architecture.X86_64],
            description="aws-durable-execution-sdk-python for the payout function"
        )

        payout_fn = lambda_.Function(
            self, "Payout",
            function_name=f"{slug}-payout",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="payout.handler",
            code=lambda_.Code.from_asset("lambdas/payout"),
            timeout=Duration.seconds(60),
            durable_config=lambda_.DurableConfig(execution_timeout=Duration.days(3),retention_period=Duration.days(7)),
            layers=[durable_sdk_layer],
            environment={
                "CLAIMS_TABLE": claims.table_name,
                "SENDER_EMAIL": sender_email,
                "APPROVE_URL": approve_url,
                "AUTO_APPROVE_UNDER": AUTO_APPROVE_UNDER
            },
            log_retention=logs.RetentionDays.ONE_WEEK
        )
        claims.grant_read_write_data(payout_fn)
        payout_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail"], 
                resources=["*"]
            )
        )
        # Durable functions must be invoked via a QUALIFIED arn (alias).
        payout_alias = payout_fn.add_alias("live")

        # ── One image per agent: agents/<name> → claims-a2a-<name> ────────────
        # AgentCore pins the image URI at update time, so rolling an agent means
        # deploying a NEW tag - re-pushing one leaves the old image serving.
        # IMAGE_TAG_FRAUD=3 rolls just fraud; IMAGE_TAG=3 rolls the whole team.
        def artifact_for(agent_name: str) -> agentcore.AgentRuntimeArtifact:
            tag = (
                os.environ.get(f"IMAGE_TAG_{agent_name.upper()}")
                or os.environ.get("IMAGE_TAG")
                or RECORDED_TAGS.get(agent_name, "latest")
            )
            repo = ecr.Repository.from_repository_name(
                self, f"Repo{agent_name.capitalize()}", f"{slug}-{agent_name}"
            )
            return agentcore.AgentRuntimeArtifact.from_ecr_repository(repo, tag)

        # Each agent gets only the config it needs - no shared fat env block.
        OBSERVABILITY = {
            "AGENT_OBSERVABILITY_ENABLED": "true"
        }
        specialist_env = {
            "intake": {
                "DOCS_BUCKET": docs.bucket_name
            },
            "policy": {
                "VECTOR_BUCKET": vector_bucket_name, 
                "INDEX_NAME": f"{slug}-policies"
            },
            "fraud": {
                "FRAUD_FN": fraud_fn.function_arn
            },
            "payout": {
                "PAYOUT_FN": payout_alias.function_arn,
                "AUTO_APPROVE_UNDER": AUTO_APPROVE_UNDER,
                "CLAIMS_TABLE": claims.table_name,
            },
        }

        specialists = {}
        for role in SPECIALISTS:
            rt = agentcore.Runtime(
                self, f"Agent{role.capitalize()}",
                # A2A: AgentCore proxies JSON-RPC to a server on :9000
                protocol_configuration=agentcore.ProtocolType.A2_A,
                agent_runtime_artifact=artifact_for(role),
                network_configuration=(agentcore.RuntimeNetworkConfiguration.using_public_network()),
                environment_variables={**OBSERVABILITY, **specialist_env[role]},
            )
            rt.add_to_role_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],
                resources=["*"])
            )
            specialists[role] = rt

        # Per-role permissions - least privilege per agent.
        docs.grant_read(specialists["intake"])
        specialists["intake"].add_to_role_policy(
            iam.PolicyStatement(
                actions=["textract:AnalyzeDocument", "textract:DetectDocumentText"],
                resources=["*"]
            )
        )
        specialists["policy"].add_to_role_policy(
            iam.PolicyStatement(
                # GetVectors is what returnMetadata=True needs; QueryVectors alone is not enough.
                actions=["s3vectors:QueryVectors", "s3vectors:GetVectors", "s3vectors:GetIndex"],
                resources=[vbucket.attr_vector_bucket_arn, index.attr_index_arn]
            )
        )
        fraud_fn.grant_invoke(specialists["fraud"])
        payout_alias.grant_invoke(specialists["payout"])
        # Read-only: the agent reports the recorded decision, it never writes one.
        claims.grant_read_data(specialists["payout"])

        # ── Orchestrator: HTTP protocol, calls the peers over A2A ─────────────
        # Raw ARNs: the orchestrator sends A2A JSON-RPC through InvokeAgentRuntime,
        # which signs with SigV4. Building a runtime URL here would need the ARN
        # percent-encoded, and CDK tokens cannot be encoded at synth time.
        peer_arns = ",".join(f"{r}={specialists[r].agent_runtime_arn}" for r in SPECIALISTS)

        orchestrator = agentcore.Runtime(
            self, "Orchestrator",
            protocol_configuration=agentcore.ProtocolType.HTTP,
            agent_runtime_artifact=artifact_for("orchestrator"),
            network_configuration=(agentcore.RuntimeNetworkConfiguration.using_public_network()),
            environment_variables={
                **OBSERVABILITY, 
                "PEER_ARNS": peer_arns
            },
        )
        orchestrator.add_to_role_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"]
            )
        )
        # The orchestrator invokes each specialist runtime over A2A.
        for rt in specialists.values():
            rt.grant_invoke(orchestrator)

        # ── Entry + worker Lambdas ────────────────────────────────────────────
        # The agent flow takes ~45s, past API Gateway's 29s integration ceiling,
        # so entry dispatches and the client polls.
        worker = lambda_.Function(
            self, "Worker",
            function_name=f"{slug}-worker",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="worker.handler",
            code=lambda_.Code.from_asset("lambdas/worker"),
            timeout=Duration.minutes(10),
            environment={
                "ORCHESTRATOR_ARN": orchestrator.agent_runtime_arn,
                "JOBS_TABLE": jobs.table_name,
            },
            log_retention=logs.RetentionDays.ONE_WEEK
        )
        orchestrator.grant_invoke(worker)
        jobs.grant_read_write_data(worker)

        entry = lambda_.Function(
            self, "Entry",
            function_name=f"{slug}-entry",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="entry.handler",
            code=lambda_.Code.from_asset("lambdas/entry"),
            timeout=Duration.seconds(30),
            environment={
                "WORKER_FN": worker.function_name,
                "JOBS_TABLE": jobs.table_name,
            },
            log_retention=logs.RetentionDays.ONE_WEEK
        )
        worker.grant_invoke(entry)
        jobs.grant_read_write_data(entry)

        approve = lambda_.Function(
            self, "Approve",
            function_name=f"{slug}-approve",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="approve.handler",
            code=lambda_.Code.from_asset("lambdas/approve"),
            timeout=Duration.seconds(30),
            log_retention=logs.RetentionDays.ONE_WEEK
        )
        approve.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:SendDurableExecutionCallbackSuccess", "lambda:SendDurableExecutionCallbackFailure"],
                resources=["*"]
            )
        )

        claim_resource = api.root.add_resource(
            "claim",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type"],
            ),
        )
        claim_resource.add_method("POST", apigw.LambdaIntegration(entry, proxy=True))
        claim_resource.add_resource("{job_id}").add_method(
            "GET", apigw.LambdaIntegration(entry, proxy=True)
        )
        api.root.add_resource("approve").add_method("GET", apigw.LambdaIntegration(approve, proxy=True))

        # ── Evaluations ───────────────────────────────────────────────────────
        agentcore.OnlineEvaluationConfig(
            self, "Evals",
            online_evaluation_config_name=f"{slug.replace('-', '_')}_evals",
            description="Goal success, tool selection, and safety across the agent team",
            data_source=agentcore.DataSourceConfig.from_cloud_watch_logs(
                log_group_names=["aws/spans"],
                service_names=[f"{slug}-orchestrator"]
            ),
            evaluators=[
                agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.GOAL_SUCCESS_RATE),
                agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.TOOL_SELECTION_ACCURACY),
                agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.CORRECTNESS),
                agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.HARMFULNESS),
            ]
        )

        CfnOutput(self, "ClaimUrl", value=f"{api.url}claim")
        CfnOutput(self, "DocsBucket", value=docs.bucket_name)
        # Needed by scripts/seed_demo_data.py.
        CfnOutput(self, "VectorBucket", value=vector_bucket_name)
        CfnOutput(self, "PolicyIndexName", value=f"{slug}-policies")
        CfnOutput(self, "HistoryTable", value=history.table_name)
        CfnOutput(self, "ClaimsTable", value=claims.table_name)
        CfnOutput(self, "OrchestratorArn", value=orchestrator.agent_runtime_arn)
        for r, rt in specialists.items(): 
            CfnOutput(self, f"{r.capitalize()}Arn", value=rt.agent_runtime_arn)
