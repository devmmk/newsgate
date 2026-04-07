#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ "${1:-}" == "--docker" ]]; then
  docker compose build
  docker compose run --rm server python -m app.session
else
  python -m app.session
fi
