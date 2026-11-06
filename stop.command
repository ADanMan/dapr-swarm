#!/bin/zsh
set -eu
cd "$(dirname "$0")"
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
.venv/bin/python -m app.codex_worker --stop
docker compose stop
echo 'Система остановлена. Задачи, история и файлы сохранены.'
