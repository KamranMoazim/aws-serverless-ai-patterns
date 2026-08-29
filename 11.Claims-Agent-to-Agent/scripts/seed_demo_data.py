"""Load the demo fixtures in data/ into the deployed stack.

Uploads the claim PDFs to the docs bucket, embeds the policy documents into the
S3 Vectors index, and writes the claimant histories the fraud rules read. Every
target is discovered from the CloudFormation stack outputs, so there is nothing
to configure.

    python3 scripts/seed_demo_data.py
"""
import json
import os
import sys

import boto3

STACK = os.environ.get("STACK_NAME", "ClaimsA2AStack")
REGION = os.environ.get("AWS_REGION", "us-east-1")
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

cloudformation = boto3.client("cloudformation", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
s3vectors = boto3.client("s3vectors", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)


def stack_outputs():
    try:
        stacks = cloudformation.describe_stacks(StackName=STACK)["Stacks"]
    except cloudformation.exceptions.ClientError as error:
        sys.exit(f"cannot read stack {STACK}: {error}")
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def upload_claims(bucket):
    folder = os.path.join(DATA, "claims")
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".pdf"):
            continue
        key = f"claims/{name}"
        s3.upload_file(os.path.join(folder, name), bucket, key)
        print(f"  s3://{bucket}/{key}")


def embed(text):
    response = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps({"inputText": text}),
    )
    return [float(value) for value in json.loads(response["body"].read())["embedding"]]


def index_policies(vector_bucket, index_name):
    folder = os.path.join(DATA, "policies")
    vectors = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".txt"):
            continue
        with open(os.path.join(folder, name)) as handle:
            body = handle.read().strip()
        vectors.append({
            "key": name.replace(".txt", ""),
            "data": {"float32": embed(body)},
            # raw_text is non-filterable in the index, which is why the policy
            # agent can return it verbatim as a quote.
            "metadata": {"source": name, "raw_text": body},
        })
        print(f"  embedded {name} ({len(body)} chars)")
    s3vectors.put_vectors(
        vectorBucketName=vector_bucket,
        indexName=index_name,
        vectors=vectors,
    )
    print(f"  put {len(vectors)} vectors into {index_name}")


def seed_history(table_name):
    table = dynamodb.Table(table_name)
    with open(os.path.join(DATA, "claimants.json")) as handle:
        claimants = json.load(handle)
    for claimant in claimants:
        table.put_item(Item={
            "claimant_id": claimant["claimant_id"],
            "claim_count": claimant["claim_count"],
            "flagged_count": claimant["flagged_count"],
        })
        print(f"  {claimant['claimant_id']}: "
              f"{claimant['claim_count']} prior, {claimant['flagged_count']} flagged")


def main():
    outputs = stack_outputs()
    required = ["DocsBucket", "VectorBucket", "PolicyIndexName", "HistoryTable"]
    missing = [key for key in required if key not in outputs]
    if missing:
        sys.exit(f"stack {STACK} is missing outputs: {', '.join(missing)} - redeploy first")

    print("claim documents:")
    upload_claims(outputs["DocsBucket"])
    print("policy documents:")
    index_policies(outputs["VectorBucket"], outputs["PolicyIndexName"])
    print("claimant history:")
    seed_history(outputs["HistoryTable"])
    print(f"\nDone. Try it against {outputs.get('ClaimUrl', '<ClaimUrl>')}")


if __name__ == "__main__":
    main()
