# Your AI Writes Code. Where Do You Run It? - Bedrock + Lambda MicroVMs

An LLM will happily write you a Python script. The moment you actually *run* that script, you have a problem: it's untrusted code, and `exec()` in your own process is a security incident waiting to happen. The usual answers - containers (shared kernel), gVisor (fiddly), a hand-rolled VM pool (slow to start) - all trade safety against speed or effort. AWS just shipped a primitive that doesn't: **Lambda MicroVMs**. This wires one up behind Bedrock so the model writes code and a throwaway, VM-isolated sandbox runs it.

## What Lambda MicroVMs are

MicroVMs are Firecracker-based sandboxes with full VM-level isolation - a separate kernel, memory, and disk per session, running Amazon Linux 2023. Two things make them different from "just use a VM": they launch from a **snapshot** in a couple of seconds, and they **suspend when idle** (preserving memory and disk) and resume on the next request. So you get real isolation with near-instant start and pay-nothing-when-idle economics - the gap no single service filled before.

## What I built

A code interpreter. You send a prompt; Bedrock writes Python; the code runs in a MicroVM dedicated to your session and streams back its output.

![Architecture](../diagrams/5.30_Articles-05.drawio.png)

```
User → API Gateway → Orchestrator Lambda
                       ├→ Bedrock (writes Python from the prompt)
                       └→ run-microvm → MicroVM sandbox (executes, isolated) → output
```

## Why it matters

Safe execution of untrusted code is one of the genuinely hard problems in AI products, and everyone reinvents a worse version of it. MicroVMs turn it into three API calls. Each session gets its own kernel, so one user's code can't touch another's. State persists across turns - write a file, come back later, it's still there - because suspend/resume checkpoints the whole VM. And idle sessions cost nothing. For an AI code interpreter, notebook backend, or agent tool-runner, that combination is exactly the shape of the workload.

## How it works (follow the numbers)

1. **Prompt - User → Orchestrator.** API Gateway hands the request to a Lambda. This Lambda is the *control plane*: it never runs user code itself.
2. **Write - Orchestrator → Bedrock.** The model turns the prompt into a self-contained Python script.
3. **Isolate - Orchestrator → run-microvm.** The Lambda looks up this session in DynamoDB; if there's no MicroVM yet it calls `run_microvm`, which restores a sandbox from the image snapshot in ~2 seconds and returns a dedicated HTTPS endpoint. A suspended VM from a previous turn just wakes up.
4. **Execute - Orchestrator → MicroVM.** The Lambda mints a short-lived token (`create_microvm_auth_token`) and POSTs the code to the MicroVM's `/execute`, where it runs in `/workspace`, isolated from everything.
5. **Return + rest.** Output comes back to the user; the `idlePolicy` suspends the VM after the idle window and terminates it later - no cleanup code on the hot path.

## The key code

The orchestrator is small - it's glue between three services and never touches the code itself:

```python
micro = boto3.client("lambda-microvms")

def _get_or_run(session_id):
    item = table.get_item(Key={"session_id": session_id}).get("Item")
    if item:                       # reuse (autoResume wakes a suspended VM)
        return item["microvm_id"], item["endpoint"]
    r = micro.run_microvm(imageIdentifier=IMAGE_ARN, idlePolicy={
        "autoResumeEnabled": True, "maxIdleDurationSeconds": 300})
    table.put_item(Item={"session_id": session_id, **r})
    return r["microvmId"], r["endpoint"]

def _execute(mid, endpoint, code):
    token = micro.create_microvm_auth_token(
        microvmIdentifier=mid, expirationInMinutes=10,
        allowedPorts=[{"allPorts": {}}])["authToken"]["X-aws-proxy-auth"]
    return http_post(f"https://{endpoint}/execute",
                     {"code": code}, {"X-aws-proxy-auth": token})
```

Inside the MicroVM, a tiny FastAPI app runs the code in a subprocess and implements the lifecycle hooks (`/ready` for the build snapshot, `/run` for post-restore init, plus `/execute`):

```python
@app.post("/execute")
def execute(body: Code):
    proc = subprocess.run(["python3", "-c", body.code],
                          cwd="/workspace", capture_output=True, text=True, timeout=30)
    return {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode}
```

## Building it - hybrid IaC

MicroVMs are new, so there's no CloudFormation for the lifecycle yet. A MicroVM is a runtime resource, like a Lambda invocation, not something you declare at deploy time. So CDK owns the durable infra:

```python
sessions = dynamodb.Table(self, "Sessions", partition_key=..., ...)   # session → VM map
orchestrator = lambda_.Function(self, "Orchestrator", ...)
orchestrator.add_to_role_policy(iam.PolicyStatement(actions=[
    "lambda:RunMicrovm", "lambda:CreateMicrovmAuthToken", "lambda:TerminateMicrovm"],
    resources=["*"]))
```

…and the **image** is built by a script - zip the sandbox app + Dockerfile, upload to S3, call `create-microvm-image` (Lambda builds it on a managed base and captures a snapshot, ~2–3 min), then wire the image ARN into the orchestrator. It's the same "build the artifact, then point infra at it" flow as pushing a container to ECR.

## Deploy and see it working

```bash
cdk deploy
MicrovmExecutionRoleArn=<arn> BUNDLE_BUCKET=<bucket> ORCH_FN=<fn-name> ./scripts/build_image.sh

curl -s -X POST "<RunUrl>" -H 'Content-Type: application/json' \
  -d '{"prompt":"print os.uname() and /etc/os-release"}' | jq
```

![Architecture](./ss/output.png)


## Close

The model writing code was never the hard part - running it safely was. Lambda MicroVMs make per-session, VM-isolated execution a managed primitive with snapshot-fast starts and suspend-to-zero idle cost. Your control plane stays a boring little Lambda; the dangerous part runs somewhere it can't hurt you.

```
./scripts/teardown.sh 
```

Repo: *[your repo link]*

What would you let an AI actually execute, now that it's sandboxed?