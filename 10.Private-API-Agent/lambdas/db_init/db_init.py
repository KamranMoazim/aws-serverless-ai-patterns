import os
import boto3
import psycopg
import socket

REGION = os.environ["AWS_REGION"]
DSQL_ENDPOINT = os.environ["DSQL_ENDPOINT"]   # public cluster name - cert + token
DSQL_HOST     = os.environ["DSQL_HOST"]       # private name - resolves to VPCE IPs


DDL = """
CREATE TABLE IF NOT EXISTS patients (
    patient_id text PRIMARY KEY,
    name       text,
    last_visit date,
    notes      text
);
"""

SEED = """
INSERT INTO patients (patient_id, name, last_visit, notes) VALUES
    ('P001', 'Ada Lovelace',  DATE '2026-07-14', 'stable, routine follow-up'),
    ('P002', 'Alan Turing',   DATE '2026-06-02', 'referred to cardiology'),
    ('P003', 'Grace Hopper',  DATE '2026-08-01', 'post-op review pending')
ON CONFLICT (patient_id) DO NOTHING;
"""


def handler(event, context):
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": "dsql-schema"}

    token = boto3.client("dsql", region_name=REGION)\
        .generate_db_connect_admin_auth_token(Hostname=DSQL_ENDPOINT, Region=REGION)

    hostaddr = socket.gethostbyname(DSQL_HOST)
    with psycopg.connect(
        host=DSQL_ENDPOINT, hostaddr=hostaddr, port=5432,
        dbname="postgres", user="admin", password=token,
        sslmode="verify-full", 
        sslrootcert="/var/task/AmazonRootCA1.pem",
        autocommit=True, connect_timeout=10,
    ) as conn, conn.cursor() as cur:
        # DSQL: no sequences, no foreign keys, PK must be declared at create time.
        cur.execute(DDL)
        cur.execute(SEED)

    return {"PhysicalResourceId": "dsql-schema"}