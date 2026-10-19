#!/usr/bin/env bash
# Remove the macOS LaunchAgent (leaves the venv, .env and models in place).
set -euo pipefail
LABEL="com.clapclip.mediaworker"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Removed $PLIST"
