# AgentCore Runtime requires linux/arm64.
FROM public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "main.py"]
