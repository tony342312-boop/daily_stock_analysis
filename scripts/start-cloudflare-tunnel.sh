#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLOUDFLARED="${CLOUDFLARED:-$(command -v cloudflared 2>/dev/null || true)}"
if [[ -z "$CLOUDFLARED" && -x "${HOME}/bin/cloudflared" ]]; then
  CLOUDFLARED="${HOME}/bin/cloudflared"
fi
LOCAL_URL="${LOCAL_URL:-http://127.0.0.1:8000}"

if [[ ! -x "$CLOUDFLARED" ]]; then
  echo "cloudflared not found or not executable: $CLOUDFLARED" >&2
  echo "Install it first, for example:" >&2
  echo "  mkdir -p \"\$HOME/bin\"" >&2
  echo "  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o \"\$HOME/bin/cloudflared\"" >&2
  echo "  chmod +x \"\$HOME/bin/cloudflared\"" >&2
  exit 1
fi

cd "$PROJECT_DIR"
exec "$CLOUDFLARED" tunnel --no-autoupdate --url "$LOCAL_URL"
