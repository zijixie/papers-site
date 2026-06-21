#!/usr/bin/env bash
set -euo pipefail

PORT="${PAPERS_SITE_BACKEND_PORT:-8765}"

if command -v cloudflared >/dev/null 2>&1; then
  exec cloudflared tunnel --url "http://127.0.0.1:${PORT}"
fi

if [ -x "./backend/cloudflared" ]; then
  exec ./backend/cloudflared tunnel --url "http://127.0.0.1:${PORT}"
fi

echo "cloudflared is not installed. Install it or put the binary at backend/cloudflared." >&2
exit 1
