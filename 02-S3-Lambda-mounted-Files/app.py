#!/usr/bin/env python3
import aws_cdk as cdk
import os

from s3_lambda_mounted_files.s3_lambda_mounted_files_stack import S3LambdaMountedFilesStack


app = cdk.App()

S3LambdaMountedFilesStack(
    app,
    "S3LambdaMountedFilesStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()