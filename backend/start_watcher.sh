#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

export PAPERS_SITE_REPO="${PAPERS_SITE_REPO:-$(pwd)}"
export PAPERS_SITE_PUBLIC_URL="${PAPERS_SITE_PUBLIC_URL:-https://zijixie.github.io/papers-site}"
export PAPERS_SITE_GIT_PUSH="${PAPERS_SITE_GIT_PUSH:-1}"
export PAPERS_POLL_SECONDS="${PAPERS_POLL_SECONDS:-5}"

echo "[start_watcher] Starting git-pull watcher..."
python3 -m backend.watcher
