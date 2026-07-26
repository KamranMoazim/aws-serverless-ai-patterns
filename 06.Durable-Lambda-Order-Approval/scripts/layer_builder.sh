#!/usr/bin/env bash
# Build a Lambda Layer ZIP containing the durable-execution SDK and dependencies.

set -euo pipefail

LAYER_DIR="layers/durable-sdk"
PYTHON_DIR="${LAYER_DIR}/python"
ZIP_FILE="durable-sdk-layer.zip"

echo "Cleaning previous build..."
rm -rf "${LAYER_DIR}"
mkdir -p "${PYTHON_DIR}"

echo "Installing dependencies into layer..."
pip install \
    -r lambdas/order_fn/requirements.txt \
    -t "${PYTHON_DIR}" \
    --upgrade

echo "Creating Lambda Layer ZIP..."
cd "${LAYER_DIR}"
zip -r "../../${ZIP_FILE}" python
cd - >/dev/null

echo ""
echo "Layer package created:"
echo "  ${ZIP_FILE}"
echo ""
echo "Deploy the layer and attach it to your Lambda."