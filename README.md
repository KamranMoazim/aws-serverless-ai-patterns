# AWS Serverless & AI Patterns

Small, production-shaped reference patterns for building serverless and AI systems on AWS - each one a self-contained, deployable [AWS CDK](https://docs.aws.amazon.com/cdk/) project with a walkthrough README.

Every pattern is the *smallest honest version* of a real architecture: minimal code, managed services, zero idle cost. Deploy it, read it, lift the parts you need.

## Patterns

| # | Pattern | Architecture |
|---|---------|--------------|
| 01 | [Lambda as an MCP Tool](./01-lambda-as-mcp-tool/) | Cognito (JWT) → AgentCore Gateway → Lambda (tool) → DynamoDB |
| 02 | [S3 Files Mounted in Lambda](./02-S3-Lambda-mounted-Files/) | API Gateway → Lambda ⇄ S3 bucket mounted at `/mnt/s3` |
| 03 | [Response-Streaming AI Answer](./03-Response-Streaming-AI-answer/) | API Gateway REST (STREAM) → Lambda (FastAPI + Web Adapter) → Bedrock `converse_stream` |
| 04 | [AgentCore Memory & Observability](./04-Agentcore-Memory-Observibility/) | API Gateway → Lambda → AgentCore Runtime (Strands) ⇄ AgentCore Memory → ADOT → CloudWatch GenAI |
| 05 | [Bedrock MicroVM Code Sandbox](./05-Bedrock-Microvm-Code-Sandbox/) | API Gateway → Orchestrator Lambda → Bedrock (writes code) → Lambda MicroVM (runs it, isolated) |
| 06 | [Durable Lambda Order Approval](./06.Durable-Lambda-Order-Approval/) | Lambda Durable Functions + callback → SES human approve/reject → DynamoDB |
| 07 | [Per-Tenant Token Metering](./07.Tenant-Token-Metering/) | AppSync Events (WebSocket) → Lambda → DynamoDB counters → Bedrock → Firehose → S3 → Athena |
| 08 | [Multi-Tenant MCP Gateway](./08.Multi-tenant-MCP-Gateway/) | AgentCore Gateway + pre-token trigger → interceptors → Cedar policy → Lambda tools → Evaluations |
| 09 | [S3 Vectors RAG](./09.S3-Vector-RAG/) | S3 → Step Functions Distributed Map → Titan Embed → S3 Vectors → streamed answer with citations |
| 10 | [Private-API Agent](./10.Private-API-Agent/) | Private API Gateway → Lambda (VPC) → AgentCore Runtime → VPC Lattice + PrivateLink + Aurora DSQL, no internet |
| 11 | [Claims Agent-to-Agent](./11.Claims-Agent-to-Agent/) | Orchestrator agent ─A2A→ intake / policy / fraud / payout agents → Textract, S3 Vectors, DynamoDB, durable approval |
| 12 | [Video Knowledge Base](./12.Video-KB/) | S3 (video) → Step Functions → MediaConvert + Transcribe → S3 Vectors → timestamped CloudFront deep link |

Architecture diagrams for every pattern live in [`docs/diagrams`](./docs/diagrams/).

## How to use a pattern

Each folder is independent. From inside any pattern:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap   # once per account/region
cdk deploy
```

Requirements: Node.js + AWS CDK CLI (`npm install -g aws-cdk`), Python 3.13, AWS credentials, an account bootstrapped in `us-east-1`.

## License

[MIT-0](./LICENSE) - use it however you like, no attribution required.

## Author

Built by Kamran Moazim - AWS-native serverless & AI engineering.
[X / @KamranMoazim](https://x.com/KamranMoazim) · [LinkedIn](https://www.linkedin.com/in/kamran-moazim/)
