"""
The app that runs INSIDE each MicroVM. Lambda snapshots it once (at image build),
then restores it per session. It exposes the MicroVM lifecycle hooks plus /execute.
"""
import os
import subprocess

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

app = FastAPI()
WORKSPACE = "/workspace"
os.makedirs(WORKSPACE, exist_ok=True)


class Code(BaseModel):
    code: str


# ── MicroVM lifecycle hooks ───────────────────────────────────────────────────
@app.get("/ready")     # build-time: Lambda waits for 200 before snapshotting
def ready():
    return PlainTextResponse("ok")


@app.post("/run")      # post-restore: generate any per-session unique state here
def run():
    return PlainTextResponse("ok")


@app.post("/suspend")
def suspend():
    return PlainTextResponse("ok")


@app.post("/terminate")
def terminate():
    return PlainTextResponse("ok")


@app.get("/health")
def health():
    return PlainTextResponse("ok")


# ── The actual work: run the model's code, isolated in this VM ────────────────
@app.post("/execute")
def execute(body: Code):
    # subprocess keeps the server alive if the code crashes; /workspace persists
    # across calls (disk state survives suspend/resume).
    try:
        proc = subprocess.run(
            ["python3", "-c", body.code],
            cwd=WORKSPACE, capture_output=True, text=True, timeout=30,
        )
        return {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "execution timed out (30s)", "exit_code": -1}
