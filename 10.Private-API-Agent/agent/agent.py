"""Agent that runs entirely inside the VPC. No internet, ever.

  - Bedrock  → reached via an interface VPC endpoint (not the public API)
  - EHR API  → reached via VPC Lattice (an internal service, no NAT)
  - Aurora DSQL → IAM-auth token, no password, no connection pooler
"""
import os
import json
import urllib.request
import socket

import boto3
import psycopg
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

app = BedrockAgentCoreApp()

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
DSQL_ENDPOINT = os.environ["DSQL_ENDPOINT"]
EHR_ENDPOINT = os.environ["EHR_ENDPOINT"]      # VPC Lattice DNS - resolves only in-VPC

dsql = boto3.client("dsql", region_name=REGION)
DSQL_HOST = os.environ.get("DSQL_HOST", DSQL_ENDPOINT)


def _dsql_conn():
    token = dsql.generate_db_connect_admin_auth_token(Hostname=DSQL_ENDPOINT, Region=REGION)
    return psycopg.connect(
        host=DSQL_ENDPOINT,
        hostaddr=socket.gethostbyname(DSQL_HOST),
        port=5432, dbname="postgres", user="admin", password=token,
        sslmode="verify-full",
        sslrootcert="/etc/ssl/certs/ca-certificates.crt",
        autocommit=True, connect_timeout=5,
    )


@tool
def get_patient_records(patient_id: str) -> str:
    """Look up a patient's records in the internal clinical database by patient ID."""
    with _dsql_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT patient_id, name, last_visit, notes "
            "FROM patients WHERE patient_id = %s", (patient_id,))
        row = cur.fetchone()
    return json.dumps({"found": bool(row), "record": row}, default=str)


@tool
def get_ehr_summary(patient_id: str) -> str:
    """Fetch a patient summary from the internal EHR system (private API)."""
    # VPC Lattice: this hostname doesn't resolve on the public internet.
    req = urllib.request.Request(
        f"{EHR_ENDPOINT}/patients/{patient_id}/summary",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


@app.entrypoint
def invoke(payload, context):
    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID),   # → Bedrock VPC endpoint
        system_prompt=(
            "You are a clinical assistant. Use the tools to look up patient data. "
            "Never invent clinical details. Cite the source of each fact."
        ),
        tools=[get_patient_records, get_ehr_summary],
        trace_attributes={"session.id": getattr(context, "session_id", "none")},
    )
    return {"answer": str(agent(payload.get("prompt", "")))}


if __name__ == "__main__":
    app.run()