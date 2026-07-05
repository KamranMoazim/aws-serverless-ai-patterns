FROM public.ecr.aws/docker/library/python:3.13-slim
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000 AWS_LWA_INVOKE_MODE=RESPONSE_STREAM
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]