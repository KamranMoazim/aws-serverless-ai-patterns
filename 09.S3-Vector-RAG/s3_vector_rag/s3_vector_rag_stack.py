from aws_cdk import (
    Stack, Duration, RemovalPolicy, CfnOutput,
    aws_s3 as s3,
    aws_s3vectors as s3vectors,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_events as events,
    aws_dynamodb as dynamodb,
    aws_events_targets as targets,
    aws_stepfunctions as sfn,
    aws_ecr as ecr,
)
from constructs import Construct

PROJECT_SLUG = "s3-vectors-rag"
DIMENSION = 1024          # Titan Text Embeddings V2


class S3VectorsRagStack(Stack):
    """
    Ingest:  S3 (docs) → EventBridge (ObjectCreated)
                       → Step Functions Distributed Map (reads the S3 prefix directly)
                          → Lambda (chunk) → Titan Embed → S3 Vectors (PutVectors)

    Query:   Client → Function URL (stream) → Lambda (FastAPI + Web Adapter)
                    → S3 Vectors (kNN) → Bedrock (answer, streamed)
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG
        VBUCKET_NAME = f"{slug}-vectors"

        ledger = dynamodb.Table(
            self, "Ledger",
            table_name=f"{slug}-ledger",
            partition_key=dynamodb.Attribute(name="docKey", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Source documents ──────────────────────────────────────────────────
        docs = s3.Bucket(
            self, "Docs",
            bucket_name=f"{slug}-docs",
            event_bridge_enabled=True,            # emit ObjectCreated to EventBridge
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        # ── S3 Vectors: the whole "vector database" (native CFN - no OpenSearch) ─
        vbucket = s3vectors.CfnVectorBucket(self, "VectorBucket", vector_bucket_name=VBUCKET_NAME)
        index = s3vectors.CfnIndex(
            self, "Index",
            vector_bucket_arn=vbucket.attr_vector_bucket_arn,
            index_name=f"{slug}-index",
            data_type="float32",
            dimension=DIMENSION,
            distance_metric="cosine",
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                # raw_text is stored + returned, but NOT filterable (keeps the
                # filterable metadata small and cheap).
                non_filterable_metadata_keys=["raw_text"],
            ),
        )
        index.add_dependency(vbucket)

        # vector_bucket_name = vbucket.ref     # physical name of the vector bucket
        vector_bucket_name = VBUCKET_NAME
        index_name = f"{slug}-index"

        s3v_policy = iam.PolicyStatement(
            actions=[
                "s3vectors:PutVectors",
                "s3vectors:QueryVectors",
                "s3vectors:GetVectors",
                "s3vectors:ListVectors",
                "s3vectors:DeleteVectors",
                "s3vectors:GetIndex"
            ],
            resources=[
                vbucket.attr_vector_bucket_arn, 
                index.attr_index_arn
            ],
        )

        # ── Ingest worker: chunk → embed → put_vectors ─────────────────────────
        ingest = lambda_.Function(
            self, "Ingest",
            function_name=f"{slug}-ingest",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="ingest.handler",
            code=lambda_.Code.from_asset("lambdas/ingest"),
            timeout=Duration.minutes(5),
            memory_size=1024,
            environment={
                "DOCS_BUCKET": docs.bucket_name,
                "VECTOR_BUCKET": vector_bucket_name,
                "INDEX_NAME": index_name,
                "LEDGER_TABLE": ledger.table_name
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        docs.grant_read(ingest)
        ingest.add_to_role_policy(s3v_policy)
        ledger.grant_read_write_data(ingest)
        ingest.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"], 
                resources=["*"]
            )
        )

        # ── Step Functions Distributed Map over the S3 prefix ─────────────────
        # The Map reads the object list from S3 itself (ItemReader) - no glue Lambda
        # listing keys, and no 256KB state payload ceiling. 10k concurrent children.
        definition = {
            "Comment": "Distributed Map: embed every doc into S3 Vectors",
            "StartAt": "EmbedAll",
            "States": {
                "EmbedAll": {
                    "Type": "Map",
                    "ItemReader": {
                        "Resource": "arn:aws:states:::s3:listObjectsV2",
                        "Parameters": {
                            "Bucket": docs.bucket_name,
                            "Prefix.$": "$.prefix",
                        },
                    },
                    "ItemProcessor": {
                        "ProcessorConfig": {
                            "Mode": "DISTRIBUTED", 
                            "ExecutionType": "STANDARD"
                        },
                        "StartAt": "Embed",
                        "States": {
                            "Embed": {
                                "Type": "Task",
                                "Resource": "arn:aws:states:::lambda:invoke",
                                "Parameters": {
                                    "FunctionName": ingest.function_arn,
                                    "Payload": {
                                        "Key.$": "$.Key",
                                        "Bucket": docs.bucket_name,
                                    },
                                },
                                "Retry": [
                                    {
                                        "ErrorEquals": ["States.ALL"], 
                                        "MaxAttempts": 3,
                                        "BackoffRate": 2, 
                                        "IntervalSeconds": 2
                                    }
                                ],
                                "End": True,
                            }
                        },
                    },
                    "MaxConcurrency": 1000,
                    "ToleratedFailurePercentage": 5,
                    "End": True,
                }
            },
        }

        sm_role = iam.Role(
            self, 
            "SmRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com")
        )
        ingest.grant_invoke(sm_role)
        docs.grant_read(sm_role)                       # ItemReader lists the bucket
        sm_role.add_to_policy(iam.PolicyStatement(     # Distributed Map spawns children
                actions=[
                    "states:StartExecution",
                    "states:DescribeExecution",
                    "states:StopExecution"
                ],
                resources=["*"]
            )
        )

        state_machine = sfn.CfnStateMachine(
            self, "Embedder",
            state_machine_name=f"{slug}-embedder",
            role_arn=sm_role.role_arn,
            definition=definition,
        )

        # ── EventBridge: new object → start the Map ───────────────────────────
        rule = events.Rule(
            self, "OnUpload",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [docs.bucket_name]}},
            ),
        )
        rule.add_target(
            targets.SfnStateMachine(
                sfn.StateMachine.from_state_machine_arn(
                    self,
                    "SmRef",
                    state_machine.attr_arn
                ),
                input=events.RuleTargetInput.from_object({"prefix": "docs/"}),
            )
        )

        # ── Query API: container Lambda (FastAPI + Web Adapter), streams ──────
        # Build + push the image first (see README), same flow as the streaming project.
        repo = ecr.Repository.from_repository_name(self, "QueryRepo", f"{slug}-query")
        query = lambda_.DockerImageFunction(
            self, "Query",
            function_name=f"{slug}-query",
            code=lambda_.DockerImageCode.from_ecr(repo, tag_or_digest="latest"),
            timeout=Duration.seconds(120),
            memory_size=512,
            environment={
                "VECTOR_BUCKET": vector_bucket_name,
                "INDEX_NAME": index_name,
                "AWS_LWA_INVOKE_MODE": "RESPONSE_STREAM",
            },
        )
        query.add_to_role_policy(s3v_policy)
        query.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=["*"]))

        url = query.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,     # demo only
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_headers=["*"],
                allowed_methods=[lambda_.HttpMethod.ALL]
            ),
        )

        CfnOutput(self, "DocsBucket", value=docs.bucket_name)
        CfnOutput(self, "VectorBucketName", value=vector_bucket_name)
        CfnOutput(self, "IndexName", value=index_name)
        CfnOutput(self, "QueryUrl", value=url.url)
        CfnOutput(self, "StateMachineArn", value=state_machine.attr_arn)