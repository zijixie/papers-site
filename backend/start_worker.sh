#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [ -z "${LLM_API_KEY:-}" ] && [ -z "${DASHSCOPE_API_KEY:-}" ]; then
  echo "Set LLM_API_KEY or DASHSCOPE_API_KEY before starting the worker" >&2
  exit 1
fi

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "Set GITHUB_TOKEN before starting the worker" >&2
  exit 1
fi

python3 -m backend.queue_worker
