#!/usr/bin/env bash

futu_opend_repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "${script_dir}/.." && pwd
}

futu_opend_trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

futu_opend_unquote() {
  local value
  value="$(futu_opend_trim "$1")"
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

load_futu_opend_env() {
  local repo_root env_file line key value
  repo_root="$(futu_opend_repo_root)"
  env_file="${FUTU_OPEND_ENV_FILE:-${repo_root}/.env}"

  [[ -f "$env_file" ]] || return 0

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -n "$(futu_opend_trim "$line")" ]] || continue
    [[ "$(futu_opend_trim "$line")" == \#* ]] && continue
    [[ "$line" == *=* ]] || continue

    key="$(futu_opend_trim "${line%%=*}")"
    value="$(futu_opend_unquote "${line#*=}")"

    case "$key" in
      FUTU_LOGIN_ACCOUNT|FUTU_LOGIN_PWD|FUTU_LOGIN_PWD_MD5|FUTU_OPEND_*)
        if [[ -z "${!key+x}" ]]; then
          export "$key=$value"
        fi
        ;;
    esac
  done <"$env_file"
}
