#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"
WEBUI_BIND_HOST="${WEBUI_BIND_HOST:-127.0.0.1}"
WEBUI_BIND_PORT="${WEBUI_BIND_PORT:-8000}"

cd "$PROJECT_DIR"

exec "$PYTHON_BIN" main.py \
  --webui-only \
  --host "$WEBUI_BIND_HOST" \
  --port "$WEBUI_BIND_PORT"
