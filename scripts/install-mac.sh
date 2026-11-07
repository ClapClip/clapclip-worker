#!/usr/bin/env bash
# Apple Silicon M-series compatibility entry: install only; no autostart/polling.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$(uname -s)" != Darwin ]; then
  printf '%s\n' 'Use scripts/setup-worker.sh on Linux or scripts/setup-worker.ps1 on Windows.' >&2
  exit 1
fi
exec bash "$ROOT/scripts/setup-worker.sh" install "$@"
