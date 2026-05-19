#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "${SCRIPT_DIR}/futu-opend-env.sh"
load_futu_opend_env

MODE="${FUTU_OPEND_MODE:-gui}"
if [[ "$MODE" == "cmd" || "$MODE" == "cli" ]]; then
  DEFAULT_STARTER="${SCRIPT_DIR}/start-futu-opend-cmd.sh"
else
  DEFAULT_STARTER="/home/tony_9756/Downloads/futu-opend/start-futu-opend-gui.sh"
fi

HOST="${FUTU_OPEND_HOST:-127.0.0.1}"
PORT="${FUTU_OPEND_PORT:-11111}"
STARTER="${FUTU_OPEND_STARTER:-$DEFAULT_STARTER}"
LOG_FILE="${FUTU_OPEND_LOG:-/tmp/futu-opend-start.log}"
WAIT_SECONDS="${FUTU_OPEND_WAIT_SECONDS:-30}"
PYTHON="${FUTU_OPEND_PYTHON:-/home/tony_9756/miniconda3/envs/daily_stock_analysis/bin/python}"
HEALTH_SCRIPT="${FUTU_OPEND_HEALTH_SCRIPT:-${SCRIPT_DIR}/check-futu-opend.py}"
REQUIRE_TRADE="${FUTU_OPEND_REQUIRE_TRADE:-0}"

port_open() {
  timeout 2 bash -c "</dev/tcp/${HOST}/${PORT}" >/dev/null 2>&1
}

health_ready() {
  local trade_arg=()
  if [[ "$REQUIRE_TRADE" == "1" || "$REQUIRE_TRADE" == "true" ]]; then
    trade_arg=(--require-trade)
  fi

  if [[ -x "$PYTHON" && -f "$HEALTH_SCRIPT" ]]; then
    "$PYTHON" "$HEALTH_SCRIPT" \
      --host "$HOST" \
      --port "$PORT" \
      "${trade_arg[@]}"
    return $?
  fi

  port_open
}

if health_ready; then
  exit 0
fi

if [[ ! -x "$STARTER" ]]; then
  echo "Futu OpenD starter is missing or not executable: $STARTER" >&2
  exit 2
fi

if [[ "$MODE" != "cmd" && "$MODE" != "cli" && -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "No desktop session detected. Start OpenD after logging into the GUI desktop." >&2
  exit 3
fi

if ! pgrep -af 'Futu_OpenD|FutuOpenD|OpenD-GUI|FTGateway' >/dev/null 2>&1; then
  nohup "$STARTER" >"$LOG_FILE" 2>&1 &
  echo "Started Futu OpenD. Log: $LOG_FILE"
else
  echo "Futu OpenD process exists, waiting for ${HOST}:${PORT}..."
fi

for _ in $(seq 1 "$WAIT_SECONDS"); do
  if health_ready; then
    exit 0
  fi
  sleep 1
done

echo "Futu OpenD is not ready yet. The GUI may need login/verification." >&2
exit 1
