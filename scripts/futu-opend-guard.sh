#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "${SCRIPT_DIR}/futu-opend-env.sh"
load_futu_opend_env

ENSURE_SCRIPT="${FUTU_OPEND_ENSURE_SCRIPT:-${SCRIPT_DIR}/ensure-futu-opend.sh}"
INTERVAL_SECONDS="${FUTU_OPEND_GUARD_INTERVAL_SECONDS:-60}"
LOG_FILE="${FUTU_OPEND_GUARD_LOG:-/tmp/futu-opend-guard.log}"

mkdir -p "$(dirname "$LOG_FILE")"

echo "[$(date '+%F %T')] Futu OpenD guard started. interval=${INTERVAL_SECONDS}s" >>"$LOG_FILE"

while true; do
  {
    echo "[$(date '+%F %T')] health check"
    "$ENSURE_SCRIPT"
    status=$?
    echo "[$(date '+%F %T')] ensure exit=${status}"
  } >>"$LOG_FILE" 2>&1 || true

  sleep "$INTERVAL_SECONDS"
done
