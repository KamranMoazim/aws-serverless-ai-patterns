#!/usr/bin/env python3
import aws_cdk as cdk
import os

from s3_vector_rag.s3_vector_rag_stack import S3VectorsRagStack


app = cdk.App()

S3VectorsRagStack(
    app,
    "S3VectorsRagStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )
)

app.synth()