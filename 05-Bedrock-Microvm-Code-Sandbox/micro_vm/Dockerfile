# Lambda builds these layers ON TOP of the managed base image you pass to
# create-microvm-image via --base-image-arn. Verify the exact base ARN (and whether
# FROM must reference it) in the "Create your first MicroVM" tutorial for your region.
FROM public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /workspace

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
