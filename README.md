# AWS Serverless & AI Patterns

Small, production-shaped reference patterns for building serverless and AI systems on AWS — each one a self-contained, deployable [AWS CDK](https://docs.aws.amazon.com/cdk/) project with a walkthrough README.

Every pattern is the *smallest honest version* of a real architecture: minimal code, managed services, zero idle cost. Deploy it, read it, lift the parts you need.

## Patterns

| # | Pattern | Architecture | Deploy |
|---|---------|-------------|--------|
| 01 | [Lambda as an MCP Tool](./01-lambda-as-mcp-tool/) | Bedrock AgentCore Gateway → Lambda → DynamoDB | `cdk deploy` |

_More patterns added as they ship._

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

[MIT-0](./LICENSE) — use it however you like, no attribution required.

## Author

Built by Kamran Moazim — AWS-native serverless & AI engineering.
[X / @KamranMoazim](https://x.com/KamranMoazim) · [LinkedIn](https://www.linkedin.com/)