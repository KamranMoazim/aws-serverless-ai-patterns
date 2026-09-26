from aws_cdk import (
    Stack, Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3vectors as s3vectors,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_events as events,
    aws_events_targets as targets,
    aws_stepfunctions as sfn,
    aws_apigateway as apigw,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)
from constructs import Construct

PROJECT_SLUG = "video-kb"
DIMENSION = 1024      # Titan Text Embeddings V2
THUMB_INTERVAL_SECONDS = 30
THUMB_MAX_CAPTURES = 20


class VideoKbStack(Stack):
    """
    S3 (video) → EventBridge → Step Functions
        ├→ MediaConvert (transcode + thumbnails) → S3 → CloudFront
        ├→ Transcribe (transcript + speaker labels) → S3
        ├→ Distributed Map (chunks) → Titan Embed → S3 Vectors
        └→ Bedrock (chapters + summary) → DynamoDB

    Query: Client → API GW → Lambda → S3 Vectors → timestamped clip + CloudFront URL
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG
        index_name = f"{slug}-index"
        vector_bucket_name = f"{slug}-{self.account}-{self.region}"

        # ── Buckets ───────────────────────────────────────────────────────────
        media = s3.Bucket(
            self, "Media", 
            event_bridge_enabled=True,
            removal_policy=RemovalPolicy.DESTROY, 
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL
        )
        output = s3.Bucket(
            self, "Output",
            removal_policy=RemovalPolicy.DESTROY, 
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.HEAD],
                    allowed_origins=["*"], 
                    allowed_headers=["*"],
                    max_age=3600
                )
            ]
        )
        transcripts = s3.Bucket(
            self, "Transcripts",
            removal_policy=RemovalPolicy.DESTROY, 
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL
        )

        # ── CloudFront in front of the rendered output ────────────────────────
        # hls.js fetches the manifest and segments with XHR, so the CDN has to answer
        # CORS. The bucket answers it, not a CloudFront response-headers policy: the
        # managed SimpleCORS policy silently drops Access-Control-Allow-Origin when the
        # request carries an HTTP "priority" header, which Chrome sends on every fetch.
        # Origin is forwarded to S3 and kept in the cache key so the variants stay honest.
        media_cache = cloudfront.CachePolicy(
            self, "MediaCache",
            cache_policy_name=f"{slug}-media-cache",
            default_ttl=Duration.days(1), 
            max_ttl=Duration.days(365),
            min_ttl=Duration.seconds(1),
            header_behavior=cloudfront.CacheHeaderBehavior.allow_list("Origin"),
            enable_accept_encoding_gzip=True, 
            enable_accept_encoding_brotli=True
        )

        cdn = cloudfront.Distribution(
            self, "Cdn",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(output),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=media_cache,
                origin_request_policy=cloudfront.OriginRequestPolicy.CORS_S3_ORIGIN,
            ),
            comment=f"{slug} media delivery",
        )

        # ── S3 Vectors: transcript chunks WITH timestamps ─────────────────────
        # The name is set explicitly: CfnVectorBucket exposes only an ARN attribute,
        # and the s3vectors API takes a bucket name.
        vbucket = s3vectors.CfnVectorBucket(
            self, "VectorBucket",
            vector_bucket_name=vector_bucket_name
        )
        vbucket.apply_removal_policy(RemovalPolicy.DESTROY)
        index = s3vectors.CfnIndex(
            self, "Index",
            vector_bucket_arn=vbucket.attr_vector_bucket_arn,
            index_name=index_name,
            data_type="float32", 
            dimension=DIMENSION, 
            distance_metric="cosine",
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["raw_text"]
            )
        )
        index.add_dependency(vbucket)
        index.apply_removal_policy(RemovalPolicy.DESTROY)

        videos = dynamodb.Table(
            self, "Videos",
            partition_key=dynamodb.Attribute(name="video_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY
        )

        s3v_rw = iam.PolicyStatement(
            actions=["s3vectors:PutVectors", "s3vectors:QueryVectors", "s3vectors:GetVectors", "s3vectors:GetIndex"],
            resources=[vbucket.attr_vector_bucket_arn, index.attr_index_arn]
        )
        bedrock_invoke = iam.PolicyStatement(
            actions=["bedrock:InvokeModel"], 
            resources=["*"]
        )

        # ── Lambdas ───────────────────────────────────────────────────────────
        common = {
            "MEDIA_BUCKET": media.bucket_name,
            "TRANSCRIPTS_BUCKET": transcripts.bucket_name
        }
        vectors_env = {
            "VECTOR_BUCKET": vector_bucket_name, 
            "INDEX_NAME": index_name
        }

        start_transcribe = lambda_.Function(
            self, "StartTranscribe",
            function_name=f"{slug}-start-transcribe",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="start_jobs.start_transcribe",
            code=lambda_.Code.from_asset("lambdas/start_jobs"),
            timeout=Duration.seconds(60), 
            environment=common,
            log_retention=logs.RetentionDays.ONE_WEEK
        )
        media.grant_read(start_transcribe)
        transcripts.grant_read_write(start_transcribe)

        start_transcribe.add_to_role_policy(
            iam.PolicyStatement(
                actions=["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"], 
                resources=["*"]
            )
        )

        build_chunks = lambda_.Function(
            self, "BuildChunks",
            function_name=f"{slug}-build-chunks",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="start_jobs.build_chunks",
            code=lambda_.Code.from_asset("lambdas/start_jobs"),
            timeout=Duration.minutes(2), 
            memory_size=512, 
            environment=common,
            log_retention=logs.RetentionDays.ONE_WEEK
        )
        transcripts.grant_read_write(build_chunks)

        embed_chunk = lambda_.Function(
            self, "EmbedChunk",
            function_name=f"{slug}-embed-chunk",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="embed_chunk.handler",
            code=lambda_.Code.from_asset("lambdas/embed_chunk"),
            timeout=Duration.minutes(2), 
            memory_size=512,
            environment=vectors_env,
            log_retention=logs.RetentionDays.ONE_WEEK
        )
        embed_chunk.add_to_role_policy(s3v_rw)
        embed_chunk.add_to_role_policy(bedrock_invoke)

        summarize = lambda_.Function(
            self, "Summarize",
            function_name=f"{slug}-summarize",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="summarize.handler",
            code=lambda_.Code.from_asset("lambdas/summarize"),
            timeout=Duration.minutes(5), 
            memory_size=512,
            environment={**common, "VIDEOS_TABLE": videos.table_name},
            log_retention=logs.RetentionDays.ONE_WEEK
        )
        transcripts.grant_read(summarize)
        videos.grant_read_write_data(summarize)
        summarize.add_to_role_policy(bedrock_invoke)

        query = lambda_.Function(
            self, "Query",
            function_name=f"{slug}-query",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="query.handler",
            code=lambda_.Code.from_asset("lambdas/query"),
            timeout=Duration.seconds(60), 
            memory_size=512,
            environment={
                **vectors_env,
                "CDN_DOMAIN": cdn.distribution_domain_name,
                "THUMB_INTERVAL": str(THUMB_INTERVAL_SECONDS),
                "THUMB_MAX": str(THUMB_MAX_CAPTURES)
            },
            log_retention=logs.RetentionDays.ONE_WEEK
        )
        query.add_to_role_policy(s3v_rw)
        query.add_to_role_policy(bedrock_invoke)

        # ── MediaConvert role ─────────────────────────────────────────────────
        mc_role = iam.Role(
            self, "MediaConvertRole",
            assumed_by=iam.ServicePrincipal("mediaconvert.amazonaws.com")
        )
        media.grant_read(mc_role)
        output.grant_read_write(mc_role)

        # ── Step Functions: transcode ∥ transcribe → chunk → Map → summarize ──
        definition = {
            "StartAt": "Prepare",
            "States": {
                # "videos/talk.mp4" becomes "talk". Both branches key off this one value,
                # so the CloudFront path and the transcript path cannot drift apart.
                "Prepare": {
                    "Type": "Pass",
                    "Parameters": {
                        "key.$": "$.key",
                        "video_id.$": "States.ArrayGetItem(States.StringSplit($.key, '/.'), 1)",
                    },
                    "Next": "Process",
                },
                "Process": {
                    "Type": "Parallel",
                    "Branches": [
                        # Branch 1: MediaConvert - HLS + thumbnails
                        {
                            "StartAt": "Transcode",
                            "States": {
                                "Transcode": {
                                    "Type": "Task",
                                    "Resource": "arn:aws:states:::mediaconvert:createJob.sync",
                                    "Parameters": {
                                        "Role": mc_role.role_arn,
                                        "Settings": {
                                            "TimecodeConfig": {"Source": "ZEROBASED"},
                                            "Inputs": [{
                                                "FileInput.$":
                                                    "States.Format('s3://{}/{}', "
                                                    f"'{media.bucket_name}', $.key)",
                                                "TimecodeSource": "ZEROBASED",
                                                "AudioSelectors": {
                                                    "Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
                                            }],
                                            "OutputGroups": [
                                                {
                                                    "Name": "HLS",
                                                    "OutputGroupSettings": {
                                                        "Type": "HLS_GROUP_SETTINGS",
                                                        "HlsGroupSettings": {
                                                            "Destination.$":
                                                                "States.Format('s3://{}/{}/hls', "
                                                                f"'{output.bucket_name}', $.video_id)",
                                                            "SegmentLength": 6,
                                                            "MinSegmentLength": 0,
                                                        },
                                                    },
                                                    # The master manifest takes its name from the
                                                    # destination (hls.m3u8); NameModifier names the
                                                    # variant and is mandatory for HLS outputs.
                                                    "Outputs": [{
                                                        "NameModifier": "_720p",
                                                        "ContainerSettings": {"Container": "M3U8"},
                                                        "VideoDescription": {
                                                            "CodecSettings": {
                                                                "Codec": "H_264",
                                                                "H264Settings": {
                                                                    "RateControlMode": "QVBR",
                                                                    "MaxBitrate": 3000000},
                                                            }},
                                                        "AudioDescriptions": [{
                                                            "AudioSourceName": "Audio Selector 1",
                                                            "CodecSettings": {
                                                                "Codec": "AAC",
                                                                "AacSettings": {
                                                                    "Bitrate": 96000,
                                                                    "CodingMode": "CODING_MODE_2_0",
                                                                    "SampleRate": 48000}}}],
                                                    }],
                                                },
                                                {
                                                    "Name": "Thumbnails",
                                                    "OutputGroupSettings": {
                                                        "Type": "FILE_GROUP_SETTINGS",
                                                        "FileGroupSettings": {
                                                            "Destination.$":
                                                                "States.Format('s3://{}/{}/thumb', "
                                                                f"'{output.bucket_name}', $.video_id)",
                                                        },
                                                    },
                                                    "Outputs": [{
                                                        "ContainerSettings": {"Container": "RAW"},
                                                        "VideoDescription": {
                                                            "CodecSettings": {
                                                                "Codec": "FRAME_CAPTURE",
                                                                "FrameCaptureSettings": {
                                                                    "FramerateNumerator": 1,
                                                                    "FramerateDenominator":
                                                                        THUMB_INTERVAL_SECONDS,
                                                                    "MaxCaptures": THUMB_MAX_CAPTURES,
                                                                    "Quality": 80}}},
                                                    }],
                                                },
                                            ],
                                        },
                                    },
                                    # ResultPath cannot be null here: jsii drops None from the
                                    # definition dict, so the field would vanish from the template.
                                    "ResultSelector": {"job_id.$": "$.Job.Id"},
                                    "ResultPath": "$.transcode",
                                    "End": True,
                                }
                            },
                        },
                        # Branch 2: Transcribe → chunks → embed (Distributed Map) → summarize
                        {
                            "StartAt": "StartTranscribe",
                            "States": {
                                "StartTranscribe": {
                                    "Type": "Task",
                                    "Resource": "arn:aws:states:::lambda:invoke",
                                    "Parameters": {
                                        "FunctionName": start_transcribe.function_arn,
                                        "Payload.$": "$"},
                                    "ResultSelector": {"job_name.$": "$.Payload.job_name",
                                                       "video_id.$": "$.Payload.video_id"},
                                    "Next": "WaitForTranscribe",
                                },
                                "WaitForTranscribe": {
                                    "Type": "Wait", "Seconds": 30, "Next": "CheckTranscribe"},
                                "CheckTranscribe": {
                                    "Type": "Task",
                                    "Resource": "arn:aws:states:::aws-sdk:transcribe:getTranscriptionJob",
                                    "Parameters": {"TranscriptionJobName.$": "$.job_name"},
                                    "ResultSelector": {
                                        "status.$": "$.TranscriptionJob.TranscriptionJobStatus"},
                                    "ResultPath": "$.check",
                                    "Next": "TranscribeDone?",
                                },
                                "TranscribeDone?": {
                                    "Type": "Choice",
                                    "Choices": [
                                        {"Variable": "$.check.status",
                                         "StringEquals": "COMPLETED", "Next": "BuildChunks"},
                                        {"Variable": "$.check.status",
                                         "StringEquals": "FAILED", "Next": "Failed"},
                                    ],
                                    "Default": "WaitForTranscribe",
                                },
                                "Failed": {"Type": "Fail", "Error": "TranscribeFailed"},
                                "BuildChunks": {
                                    "Type": "Task",
                                    "Resource": "arn:aws:states:::lambda:invoke",
                                    "Parameters": {
                                        "FunctionName": build_chunks.function_arn,
                                        "Payload": {"video_id.$": "$.video_id"}},
                                    "ResultSelector": {
                                        "video_id.$": "$.Payload.video_id",
                                        "chunks_key.$": "$.Payload.chunks_key",
                                        "count.$": "$.Payload.count"},
                                    "Next": "EmbedChunks",
                                },
                                # Distributed Map reads the chunk list FROM S3 -
                                # no 256KB state-payload limit, 1000-way fan-out.
                                "EmbedChunks": {
                                    "Type": "Map",
                                    "ItemReader": {
                                        "Resource": "arn:aws:states:::s3:getObject",
                                        "ReaderConfig": {"InputType": "JSON"},
                                        "Parameters": {
                                            "Bucket": transcripts.bucket_name,
                                            "Key.$": "$.chunks_key"},
                                    },
                                    "ItemProcessor": {
                                        "ProcessorConfig": {"Mode": "DISTRIBUTED",
                                                            "ExecutionType": "STANDARD"},
                                        "StartAt": "Embed",
                                        "States": {
                                            "Embed": {
                                                "Type": "Task",
                                                "Resource": "arn:aws:states:::lambda:invoke",
                                                "Parameters": {
                                                    "FunctionName": embed_chunk.function_arn,
                                                    "Payload.$": "$"},
                                                "Retry": [{"ErrorEquals": ["States.ALL"],
                                                           "MaxAttempts": 3, "BackoffRate": 2,
                                                           "IntervalSeconds": 2}],
                                                "End": True,
                                            }
                                        },
                                    },
                                    # Without a ResultWriter the per-item results come back
                                    # inline and a long video overruns the 256KB output cap.
                                    "ResultWriter": {
                                        "Resource": "arn:aws:states:::s3:putObject",
                                        "Parameters": {
                                            "Bucket": transcripts.bucket_name,
                                            "Prefix": "embed-runs/"},
                                    },
                                    "MaxConcurrency": 200,
                                    "ToleratedFailurePercentage": 5,
                                    "ResultPath": "$.embed",
                                    "Next": "Summarize",
                                },
                                "Summarize": {
                                    "Type": "Task",
                                    "Resource": "arn:aws:states:::lambda:invoke",
                                    "Parameters": {
                                        "FunctionName": summarize.function_arn,
                                        "Payload": {"video_id.$": "$.video_id",
                                                    "chunks_key.$": "$.chunks_key"}},
                                    "End": True,
                                },
                            },
                        },
                    ],
                    "End": True,
                },
            },
        }

        sm_role = iam.Role(self, "SmRole",
                           assumed_by=iam.ServicePrincipal("states.amazonaws.com"))
        for fn in (start_transcribe, build_chunks, embed_chunk, summarize):
            fn.grant_invoke(sm_role)
        transcripts.grant_read_write(sm_role)
        sm_role.add_to_policy(
            iam.PolicyStatement(
                actions=["mediaconvert:CreateJob", "mediaconvert:GetJob", "mediaconvert:DescribeEndpoints"], 
                resources=["*"]
            )
        )
        sm_role.add_to_policy(
            iam.PolicyStatement(
                actions=["transcribe:GetTranscriptionJob"], 
                resources=["*"]
            )
        )
        sm_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"], 
                resources=[mc_role.role_arn]
            )
        )
        sm_role.add_to_policy(
            iam.PolicyStatement(   # Distributed Map children
                actions=["states:StartExecution", "states:DescribeExecution", "states:StopExecution"], 
                resources=["*"]
            )
        )
        sm_role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutTargets", "events:PutRule", "events:DescribeRule"], 
                resources=["*"]
            )
        )

        pipeline = sfn.CfnStateMachine(
            self, "Pipeline",
            state_machine_name=f"{slug}-pipeline",
            role_arn=sm_role.role_arn,
            definition=definition
        )

        # ── EventBridge: a new video starts the pipeline ──────────────────────
        rule = events.Rule(
            self, "OnUpload",
            event_pattern=events.EventPattern(
                source=["aws.s3"], 
                detail_type=["Object Created"],
                detail={
                    "bucket": {
                        "name": [media.bucket_name]
                    },
                    "object": {
                        "key": [{"prefix": "videos/"}]
                    }
                }
            )
        )
        rule.add_target(
            targets.SfnStateMachine(
                sfn.StateMachine.from_state_machine_arn(self, "SmRef", pipeline.attr_arn),
                input=events.RuleTargetInput.from_object({
                    "key": events.EventField.from_path("$.detail.object.key"),
                })
            )
        )

        # ── Query API ─────────────────────────────────────────────────────────
        api = apigw.RestApi(
            self, "Api", 
            rest_api_name=f"{slug}-api",
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.REGIONAL]
            ),
            deploy_options=apigw.StageOptions(stage_name="prod"),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS, 
                allow_methods=["POST", "OPTIONS"]
            )
        )
        api.root.add_resource("search").add_method(
            "POST", apigw.LambdaIntegration(query, proxy=True))

        CfnOutput(self, "MediaBucket", value=media.bucket_name)
        CfnOutput(self, "SearchUrl", value=f"{api.url}search")
        CfnOutput(self, "CdnDomain", value=cdn.distribution_domain_name)
        CfnOutput(self, "VideosTable", value=videos.table_name)
        CfnOutput(self, "VectorBucketName", value=vector_bucket_name)
        CfnOutput(self, "PipelineArn", value=pipeline.attr_arn)
