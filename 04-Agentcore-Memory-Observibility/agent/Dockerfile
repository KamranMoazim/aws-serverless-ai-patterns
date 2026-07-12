# AgentCore Runtime requires linux/arm64 containers.
# Build with:  docker buildx build --platform linux/arm64 ...
FROM public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# AgentCore Runtime calls the container on port 8080 (/invocations, /ping).
EXPOSE 8080

# opentelemetry-instrument auto-instruments Bedrock + Strands and exports traces;
# on AgentCore Runtime, ADOT ships them to CloudWatch (GenAI Observability).
CMD ["opentelemetry-instrument", "python", "agent.py"]
