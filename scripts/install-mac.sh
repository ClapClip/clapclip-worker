#!/usr/bin/env bash
# Install one LaunchAgent; edit .env before running this script.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 13), "Python 3.13+ required"'
"$PY" -m venv .venv
.venv/bin/python -m pip install -e '.[downloader]'
[ -f .env ] || cp .env.example .env
if ! .venv/bin/python -c 'from mediaworker.config import Config; Config.from_env().require_api()'; then
  echo 'Fill in .env and enable only capabilities supported on this machine, then retry.'
  exit 1
fi
LABEL="com.clapclip.mediaworker"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
mkdir -p "$HOME/Library/LaunchAgents" logs
.venv/bin/python - "$ROOT" "$LABEL" "$PLIST" <<'PY'
from pathlib import Path
import plistlib
import sys
root, label, destination = sys.argv[1:]
config = {
    'Label': label,
    'ProgramArguments': [str(Path(root) / '.venv/bin/python'), '-m', 'mediaworker', 'run', '--all-capabilities'],
    'WorkingDirectory': root, 'RunAtLoad': True, 'KeepAlive': True,
    'ProcessType': 'Background',
    'StandardOutPath': str(Path(root) / 'logs/launcher.log'),
    'StandardErrorPath': str(Path(root) / 'logs/launcher.log'),
}
with open(destination, 'wb') as handle:
    plistlib.dump(config, handle)
PY
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Started $LABEL. Logs: $ROOT/logs/mediaworker.log"
