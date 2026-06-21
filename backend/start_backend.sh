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
  echo "Set LLM_API_KEY or DASHSCOPE_API_KEY before starting the backend" >&2
  exit 1
fi
export PAPERS_UPLOAD_PASSWORD="${PAPERS_UPLOAD_PASSWORD:-eden}"
export PAPERS_SITE_REPO="${PAPERS_SITE_REPO:-$(pwd)}"
export PAPERS_SITE_PUBLIC_URL="${PAPERS_SITE_PUBLIC_URL:-https://zijixie.github.io/papers-site}"
export PAPERS_SITE_CORS_ORIGINS="${PAPERS_SITE_CORS_ORIGINS:-https://zijixie.github.io,http://localhost:8000,http://127.0.0.1:8000}"

python3 -m uvicorn backend.server:app --host 127.0.0.1 --port "${PAPERS_SITE_BACKEND_PORT:-8765}"
