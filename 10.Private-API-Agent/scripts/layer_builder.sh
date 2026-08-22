#!/usr/bin/env bash
# Build a Lambda Layer ZIP containing psycopg for the DSQL schema bootstrap.

set -euo pipefail

LAYER_DIR="layers/psycopg"
PYTHON_DIR="${LAYER_DIR}/python"
ZIP_FILE="psycopg-layer.zip"

# psycopg[binary] ships manylinux wheels. Building on macOS/arm64 without
# these flags silently produces a layer the Lambda runtime cannot import.
PLATFORM="manylinux2014_x86_64"       # match ARM64 -> manylinux2014_aarch64
PY_VERSION="3.13"

echo "Cleaning previous build..."
rm -rf "${LAYER_DIR}" "${ZIP_FILE}"
mkdir -p "${PYTHON_DIR}"

echo "Installing dependencies into layer..."
pip install \
    -r lambdas/db_init/requirements.txt \
    -t "${PYTHON_DIR}" \
    --platform "${PLATFORM}" \
    --python-version "${PY_VERSION}" \
    --only-binary=:all: \
    --upgrade

# Trim: boto3/botocore are already in the runtime and dominate the size.
rm -rf "${PYTHON_DIR}"/boto3* "${PYTHON_DIR}"/botocore* \
       "${PYTHON_DIR}"/*.dist-info "${PYTHON_DIR}"/__pycache__

echo "Creating Lambda Layer ZIP..."
cd "${LAYER_DIR}"
zip -qr "../../${ZIP_FILE}" python
cd - >/dev/null

echo ""
echo "Layer package created:"
echo "  ${ZIP_FILE}  ($(du -h "${ZIP_FILE}" | cut -f1))"
echo ""
echo "Run this before 'cdk deploy' - the stack reads the zip from disk."