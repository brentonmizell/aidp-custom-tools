#!/usr/bin/env bash
# Package this template into an uploadable zip.
# Usage: ./build.sh [output_name]   (default: my_tool.zip)
set -euo pipefail

OUT="${1:-my_tool}"
cd "$(dirname "$0")"

# If you have dependencies, download wheels first for offline install:
#   pip download --dest wheels/ --no-deps <pure-python-pkg>
#   pip download --dest wheels/ --platform manylinux2014_x86_64 \
#       --python-version 3.11 --only-binary=:all: <compiled-pkg>

rm -f "${OUT}.zip"
zip -r "${OUT}.zip" \
  tool_implementation.py \
  tool_config.json \
  requirements.txt \
  utils/ \
  config/ \
  wheels/ \
  -x "*.pyc" "*__pycache__*" "*.DS_Store"

echo "Built ${OUT}.zip"
unzip -l "${OUT}.zip"
