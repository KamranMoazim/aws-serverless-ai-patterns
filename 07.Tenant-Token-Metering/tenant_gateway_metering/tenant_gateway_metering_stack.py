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
    aws_cognito as cognito,
    aws_appsync as appsync,
    aws_glue as glue,
    aws_athena as athena,
    aws_kinesisfirehose as firehose,
)
from constructs import Construct

PROJECT_SLUG = "tenant-gateway-metering"


class TenantGatewayMeteringStack(Stack):
    """
    Client ⇄ AppSync Events (WebSocket, managed fan-out)
              → Lambda (chat handler)
                 ├→ Cognito JWT (sub → user_id)
                 ├⇄ DynamoDB (atomic ADD token counters, TTL) → limit check / 429
                 ├→ Bedrock Converse (stream tokens back over AppSync)
                 └→ Firehose → S3 (Parquet) → Glue Catalog → Athena
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG

        # ── Cognito: identity = tenant ────────────────────────────────────────
        user_pool = cognito.UserPool(
            self, "Users",
            self_sign_up_enabled=True,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            removal_policy=RemovalPolicy.DESTROY,
        )
        client = user_pool.add_client(
            "WebClient",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
        )

        # ── DynamoDB: per-tenant token counters (user_id + period) ────────────
        usage = dynamodb.Table(
            self, "Usage",
            partition_key=dynamodb.Attribute(name="user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="period", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Analytics sink: S3 + Glue + Firehose (JSON → Parquet) + Athena ────
        lake = s3.Bucket(
            self, "UsageLake",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )
        results = s3.Bucket(
            self, "AthenaResults",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        glue_db = f"{slug.replace('-', '_')}_db"
        db = glue.CfnDatabase(
            self, "GlueDb",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(name=glue_db),
        )
        # Firehose needs a Glue table to convert JSON → Parquet.
        cols = [
            ("user_id", "string"),
            ("request_id", "string"),
            ("model_id", "string"),
            ("tokens_in", "int"),
            ("tokens_out", "int"),
            ("total_tokens", "int"),
            ("cost_usd", "double"),
            ("latency_ms", "int"),
            ("ts", "string"),
        ]
        table = glue.CfnTable(
            self, "GlueTable",
            catalog_id=self.account,
            database_name=glue_db,
            table_input=glue.CfnTable.TableInputProperty(
                name="usage",
                table_type="EXTERNAL_TABLE",
                parameters={
                    "classification": "parquet"
                },
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    columns=[glue.CfnTable.ColumnProperty(name=n, type=t) for n, t in cols],
                    location=f"s3://{lake.bucket_name}/usage/",
                    input_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(serialization_library="org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"),
                ),
            ),
        )
        table.add_dependency(db)

        fh_role = iam.Role(
            self, "FirehoseRole",
            assumed_by=iam.ServicePrincipal("firehose.amazonaws.com")
        )
        lake.grant_read_write(fh_role)
        fh_role.add_to_policy(
            iam.PolicyStatement(
                actions=["glue:GetTable", "glue:GetTableVersion", "glue:GetTableVersions"],
                resources=["*"]
            )
        )
        fh_log = logs.LogGroup(
            self, 
            "FirehoseLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY
        )
        fh_log.grant_write(fh_role)

        stream = firehose.CfnDeliveryStream(
            self, "UsageStream",
            delivery_stream_name=f"{slug}-usage",
            extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                bucket_arn=lake.bucket_arn,
                role_arn=fh_role.role_arn,
                # Partitioned by day for cheap Athena scans.
                prefix="usage/dt=!{timestamp:yyyy-MM-dd}/",
                error_output_prefix="errors/!{firehose:error-output-type}/",
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                    interval_in_seconds=60, 
                    size_in_m_bs=64
                ),
                cloud_watch_logging_options=firehose.CfnDeliveryStream.CloudWatchLoggingOptionsProperty(
                    enabled=True, 
                    log_group_name=fh_log.log_group_name,
                    log_stream_name="s3"
                ),
                # JSON in → Parquet out, using the Glue table as the schema.
                data_format_conversion_configuration=firehose.CfnDeliveryStream.DataFormatConversionConfigurationProperty(
                    enabled=True,
                    input_format_configuration=firehose.CfnDeliveryStream.InputFormatConfigurationProperty(
                        deserializer=firehose.CfnDeliveryStream.DeserializerProperty(
                            open_x_json_ser_de=firehose.CfnDeliveryStream.OpenXJsonSerDeProperty()
                        )
                    ),
                    output_format_configuration=firehose.CfnDeliveryStream.OutputFormatConfigurationProperty(
                        serializer=firehose.CfnDeliveryStream.SerializerProperty(
                            parquet_ser_de=firehose.CfnDeliveryStream.ParquetSerDeProperty()
                        )
                    ),
                    schema_configuration=firehose.CfnDeliveryStream.SchemaConfigurationProperty(
                        catalog_id=self.account,
                        database_name=glue_db,
                        table_name="usage",
                        role_arn=fh_role.role_arn,
                        region=self.region
                    ),
                ),
            ),
        )
        stream.add_dependency(table)

        athena.CfnWorkGroup(
            self, "WorkGroup",
            name=f"{slug}-wg",
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{results.bucket_name}/results/"
                )
            ),
        )

        # ── AppSync Events API (Cognito for clients, IAM for the Lambda) ──────
        api = appsync.CfnApi(
            self, "EventsApi",
            name=f"{slug}-events",
            event_config=appsync.CfnApi.EventConfigProperty(
                auth_providers=[
                    appsync.CfnApi.AuthProviderProperty(
                        auth_type="AMAZON_COGNITO_USER_POOLS",
                        cognito_config=appsync.CfnApi.CognitoConfigProperty(
                            user_pool_id=user_pool.user_pool_id, 
                            aws_region=self.region
                        ),
                    ),
                    appsync.CfnApi.AuthProviderProperty(auth_type="AWS_IAM"),
                ],
                connection_auth_modes=[
                    appsync.CfnApi.AuthModeProperty(auth_type="AMAZON_COGNITO_USER_POOLS")
                ],
                default_publish_auth_modes=[
                    appsync.CfnApi.AuthModeProperty(auth_type="AMAZON_COGNITO_USER_POOLS"),
                    appsync.CfnApi.AuthModeProperty(auth_type="AWS_IAM")
                ],  # Lambda publishes via IAM
                default_subscribe_auth_modes=[
                    appsync.CfnApi.AuthModeProperty(auth_type="AMAZON_COGNITO_USER_POOLS")
                ],
            ),
        )
        events_http = api.attr_dns_http          # e.g. abc.appsync-api.us-east-1.amazonaws.com

        # ── Chat Lambda ───────────────────────────────────────────────────────
        chat = lambda_.Function(
            self, "Chat",
            function_name=f"{slug}-chat",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="chat.handler",
            code=lambda_.Code.from_asset("lambdas/chat"),
            timeout=Duration.seconds(120),
            memory_size=512,
            environment={
                "USAGE_TABLE": usage.table_name,
                "FIREHOSE_STREAM": stream.ref,
                "EVENTS_HTTP_ENDPOINT": events_http,
                "MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "MONTHLY_TOKEN_LIMIT": "100",
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        usage.grant_read_write_data(chat)
        chat.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"]
            )
        )
        chat.add_to_role_policy(
            iam.PolicyStatement(
                actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
                resources=[stream.attr_arn]
            )
        )
        # Publish streamed tokens back into the Event API (SigV4 / IAM auth).
        chat.add_to_role_policy(
            iam.PolicyStatement(
                actions=["appsync:EventPublish"],
                resources=[f"{api.attr_api_arn}/*"]
            )
        )

        # ── Namespace: Lambda as the onPublish handler, invoked ASYNC (EVENT) ─
        ds_role = iam.Role(
            self, "DsRole",
            assumed_by=iam.ServicePrincipal("appsync.amazonaws.com")
        )
        chat.grant_invoke(ds_role)

        ds = appsync.CfnDataSource(
            self, "ChatDs",
            api_id=api.attr_api_id,
            name="chat_lambda",
            type="AWS_LAMBDA",
            service_role_arn=ds_role.role_arn,
            lambda_config=appsync.CfnDataSource.LambdaConfigProperty(lambda_function_arn=chat.function_arn),
        )

        ns = appsync.CfnChannelNamespace(
            self, "ChatNs",
            api_id=api.attr_api_id,
            name="chat",
            handler_configs=appsync.CfnChannelNamespace.HandlerConfigsProperty(
                on_publish=appsync.CfnChannelNamespace.HandlerConfigProperty(
                    behavior="DIRECT",
                    integration=appsync.CfnChannelNamespace.IntegrationProperty(
                        data_source_name="chat_lambda",
                        # EVENT = async: the publish returns instantly while the
                        # handler streams tokens back. REQUEST_RESPONSE would block.
                        lambda_config=appsync.CfnChannelNamespace.LambdaConfigProperty(
                            invoke_type="EVENT"
                        ),
                    ),
                ),
            ),
        )
        ns.add_dependency(ds)

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "EventsHttpEndpoint", value=events_http)
        CfnOutput(self, "EventsRealtimeEndpoint", value=api.attr_dns_realtime)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "UsageTable", value=usage.table_name)
        CfnOutput(self, "UsageLakeBucket", value=lake.bucket_name)
        CfnOutput(self, "AthenaWorkGroup", value=f"{slug}-wg")