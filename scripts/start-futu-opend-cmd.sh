#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "${SCRIPT_DIR}/futu-opend-env.sh"
load_futu_opend_env

CMD_DIR="${FUTU_OPEND_CMD_DIR:-/home/tony_9756/Downloads/futu-opend/Futu_OpenD_10.4.6408_Ubuntu18.04/Futu_OpenD_10.4.6408_Ubuntu18.04}"
BIN="${FUTU_OPEND_CMD_BIN:-${CMD_DIR}/FutuOpenD}"
CFG_DIR="${FUTU_OPEND_CMD_CFG_DIR:-/home/tony_9756/.config/futu-opend}"
CFG_FILE="${FUTU_OPEND_CMD_CFG_FILE:-${CFG_DIR}/FutuOpenD.xml}"
LOG_DIR="${FUTU_OPEND_CMD_LOG_DIR:-${CFG_DIR}/logs}"

HOST="${FUTU_OPEND_HOST:-127.0.0.1}"
PORT="${FUTU_OPEND_PORT:-11111}"
TELNET_IP="${FUTU_OPEND_TELNET_IP:-127.0.0.1}"
TELNET_PORT="${FUTU_OPEND_TELNET_PORT:-22222}"
LANGUAGE="${FUTU_OPEND_LANG:-chs}"
LOG_LEVEL="${FUTU_OPEND_LOG_LEVEL:-info}"
AUTO_HOLD_QUOTE_RIGHT="${FUTU_OPEND_AUTO_HOLD_QUOTE_RIGHT:-1}"
CONSOLE="${FUTU_OPEND_CONSOLE:-0}"
NO_MONITOR="${FUTU_OPEND_NO_MONITOR:-0}"

xml_escape() {
  local value="$1"
  value="${value//&/&amp;}"
  value="${value//</&lt;}"
  value="${value//>/&gt;}"
  value="${value//\"/&quot;}"
  value="${value//\'/&apos;}"
  printf '%s' "$value"
}

if [[ ! -x "$BIN" ]]; then
  echo "Futu command-line OpenD binary not found or not executable: $BIN" >&2
  exit 2
fi

if [[ -z "${FUTU_LOGIN_ACCOUNT:-}" ]]; then
  echo "FUTU_LOGIN_ACCOUNT is required for command-line OpenD auto login." >&2
  exit 3
fi

if [[ -z "${FUTU_LOGIN_PWD_MD5:-}" && -z "${FUTU_LOGIN_PWD:-}" ]]; then
  echo "FUTU_LOGIN_PWD_MD5 or FUTU_LOGIN_PWD is required for command-line OpenD auto login." >&2
  exit 3
fi

umask 077
mkdir -p "$CFG_DIR" "$LOG_DIR"

ACCOUNT_XML="$(xml_escape "$FUTU_LOGIN_ACCOUNT")"
if [[ -n "${FUTU_LOGIN_PWD_MD5:-}" ]]; then
  PASSWORD_XML="		<login_pwd_md5>$(xml_escape "$FUTU_LOGIN_PWD_MD5")</login_pwd_md5>"
else
  PASSWORD_XML="		<login_pwd>$(xml_escape "$FUTU_LOGIN_PWD")</login_pwd>"
fi

cat >"$CFG_FILE" <<XML
<futu_opend>
	<ip>$(xml_escape "$HOST")</ip>
	<api_port>$(xml_escape "$PORT")</api_port>
	<login_account>${ACCOUNT_XML}</login_account>
${PASSWORD_XML}
	<lang>$(xml_escape "$LANGUAGE")</lang>
	<log_level>$(xml_escape "$LOG_LEVEL")</log_level>
	<log_path>$(xml_escape "$LOG_DIR")</log_path>
	<push_proto_type>0</push_proto_type>
	<telnet_ip>$(xml_escape "$TELNET_IP")</telnet_ip>
	<telnet_port>$(xml_escape "$TELNET_PORT")</telnet_port>
	<price_reminder_push>1</price_reminder_push>
	<auto_hold_quote_right>$(xml_escape "$AUTO_HOLD_QUOTE_RIGHT")</auto_hold_quote_right>
	<future_trade_api_time_zone>UTC+8</future_trade_api_time_zone>
	<pdt_protection>1</pdt_protection>
	<dtcall_confirmation>1</dtcall_confirmation>
</futu_opend>
XML

chmod 600 "$CFG_FILE"

cd "$CMD_DIR"
exec "$BIN" \
  "-cfg_file=${CFG_FILE}" \
  "-console=${CONSOLE}" \
  "-no_monitor=${NO_MONITOR}"
