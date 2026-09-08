#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
command -v docker >/dev/null 2>&1 || { echo "Docker is required." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker engine is not running." >&2; exit 1; }
[ -f .env ] || cp .env.example .env
docker compose up --build -d
echo "System started: http://localhost:3000"
