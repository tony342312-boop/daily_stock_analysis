#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHROME="${CHROME:-}"

if [[ -z "$CHROME" ]]; then
  for candidate in \
    "${HOME}/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome" \
    "$(command -v chromium 2>/dev/null || true)" \
    "$(command -v chromium-browser 2>/dev/null || true)" \
    "$(command -v google-chrome 2>/dev/null || true)" \
    "$(command -v google-chrome-stable 2>/dev/null || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      CHROME="$candidate"
      break
    fi
  done
fi

if [[ -z "$CHROME" || ! -x "$CHROME" ]]; then
  echo "No executable Chromium/Chrome found. Install chromium or set CHROME=/path/to/chrome." >&2
  exit 1
fi

LIB_DIRS=()
if [[ -n "${CONDA_PREFIX:-}" && -d "${CONDA_PREFIX}/lib" ]]; then
  LIB_DIRS+=("${CONDA_PREFIX}/lib")
fi
if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_PREFIX="$(cd "$(dirname "$PYTHON_BIN")/.." 2>/dev/null && pwd || true)"
  if [[ -n "$PYTHON_PREFIX" && -d "${PYTHON_PREFIX}/lib" ]]; then
    LIB_DIRS+=("${PYTHON_PREFIX}/lib")
  fi
fi
for candidate in \
  "${HOME}/miniconda3/envs/daily_stock_analysis/lib" \
  "/opt/miniconda3/envs/daily_stock_analysis/lib" \
  "${PROJECT_DIR}/.venv/lib"; do
  if [[ -d "$candidate" ]]; then
    LIB_DIRS+=("$candidate")
  fi
done
if [[ ${#LIB_DIRS[@]} -gt 0 ]]; then
  IFS=:
  export LD_LIBRARY_PATH="${LIB_DIRS[*]}:${LD_LIBRARY_PATH:-}"
  unset IFS
fi

exec "${CHROME}" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-crash-reporter \
  --disable-breakpad \
  "$@"
