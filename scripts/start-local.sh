#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  printf '%s\n' 'Local environment missing. Follow docs/local-mode.md to create .venv first.' >&2
  exit 1
fi
exec .venv/bin/python -m mediaworker local "$@"
