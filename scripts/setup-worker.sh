#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif command -v python3.13 >/dev/null 2>&1; then
  PY="$(command -v python3.13)"
elif [ -x /opt/homebrew/opt/python@3.13/bin/python3.13 ]; then
  PY=/opt/homebrew/opt/python@3.13/bin/python3.13
elif [ -x /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 ]; then
  PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  printf '%s\n' 'Install ordinary CPython 3.13 from https://www.python.org/downloads/; see docs/portable-setup.md.' >&2
  exit 1
fi
exec "$PY" "$ROOT/scripts/setup-worker.py" "$@"
