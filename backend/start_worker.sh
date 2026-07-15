#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

has_claude_settings_token="$(
  python3 - <<'PY'
import json
from pathlib import Path

settings = Path.home() / ".claude" / "settings.json"
try:
    env = json.loads(settings.read_text(encoding="utf-8")).get("env", {})
except Exception:
    env = {}
print("1" if env.get("ANTHROPIC_AUTH_TOKEN") else "0")
PY
)"

if [ -z "${LLM_API_KEY:-}" ] && [ -z "${DASHSCOPE_API_KEY:-}" ] && [ "$has_claude_settings_token" != "1" ]; then
  echo "Set LLM_API_KEY, DASHSCOPE_API_KEY, or ANTHROPIC_AUTH_TOKEN in ~/.claude/settings.json before starting the worker" >&2
  exit 1
fi

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "Set GITHUB_TOKEN before starting the worker" >&2
  exit 1
fi

python3 -m backend.queue_worker
