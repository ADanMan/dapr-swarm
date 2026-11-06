#!/bin/zsh
set -eu
cd "$(dirname "$0")"
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
if ! docker info >/dev/null 2>&1; then
  open -a Docker
  for attempt in {1..60}; do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
fi
exec .venv/bin/python scripts/start.py
