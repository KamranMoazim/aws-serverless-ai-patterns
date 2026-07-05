import os
import json
import base64
import pathlib

MOUNT = os.environ.get("MOUNT_PATH", "/mnt/s3")


def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def lambda_handler(event, context):
    """
    A tiny file API over the mounted S3 bucket — no boto3 transfer code.

        GET    /files            -> list files
        GET    /files/{name}     -> read a file
        PUT    /files/{name}     -> write a file (request body)
        DELETE /files/{name}     -> delete a file
    """
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    parts = [p for p in path.strip("/").split("/") if p]
    name = parts[1] if len(parts) > 1 else None  # /files/<name>

    root = pathlib.Path(MOUNT)

    try:
        if method == "GET" and name is None:
            files = sorted(p.name for p in root.iterdir() if p.is_file())
            return _resp(200, {"mount": MOUNT, "files": files})

        if method == "GET" and name:
            f = root / name
            if not f.is_file():
                return _resp(404, {"error": f"{name} not found"})
            return _resp(200, {"name": name, "content": f.read_text()})

        if method == "PUT" and name:
            body = event.get("body") or ""
            if event.get("isBase64Encoded"):
                body = base64.b64decode(body).decode()
            (root / name).write_text(body)
            return _resp(201, {"written": name, "bytes": len(body)})

        if method == "DELETE" and name:
            f = root / name
            if not f.is_file():
                return _resp(404, {"error": f"{name} not found"})
            f.unlink()
            return _resp(200, {"deleted": name})

        return _resp(400, {
            "error": "Use GET /files, or GET/PUT/DELETE /files/{name}"
        })

    except Exception as e:
        return _resp(500, {"error": str(e)})