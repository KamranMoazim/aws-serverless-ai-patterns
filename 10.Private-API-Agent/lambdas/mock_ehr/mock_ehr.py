import json

# Keyed to the rows seeded by lambdas/db_init — the demo is more convincing
# when both internal systems agree about who exists.
RECORDS = {
    "P001": {
        "name": "Ada Lovelace",
        "allergies": ["penicillin"],
        "conditions": [{"code": "E11.9", "display": "Type 2 diabetes"}],
        "last_encounter": "2026-07-14",
    },
    "P002": {
        "name": "Alan Turing",
        "allergies": [],
        "conditions": [{"code": "I25.10", "display": "Coronary artery disease"}],
        "last_encounter": "2026-06-02",
    },
    "P003": {
        "name": "Grace Hopper",
        "allergies": ["latex"],
        "conditions": [{"code": "Z98.890", "display": "Post-procedural state"}],
        "last_encounter": "2026-08-01",
    },
}

_REASON = {200: "200 OK", 404: "404 Not Found"}

def _resp(status, body):
    return {
        "statusCode": status,
        "statusDescription": _REASON[status],
        "headers": {"Content-Type": "application/json"},
        "isBase64Encoded": False,
        "body": json.dumps(body),
    }


def handler(event, ctx):
    # ALB passes "path", not "pathParameters".  /patients/{id}/summary
    parts = [p for p in event.get("path", "").split("/") if p]
    if len(parts) < 2 or parts[0] != "patients":
        return _resp(404, {"error": "not found", "path": event.get("path")})

    pid = parts[1]
    rec = RECORDS.get(pid)
    if rec is None:
        return _resp(404, {"error": "unknown patient", "patient_id": pid})

    return _resp(200, {"resourceType": "Bundle", "patient_id": pid, **rec})
