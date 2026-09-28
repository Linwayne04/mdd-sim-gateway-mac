#!/bin/bash
# mdd-sim-gateway macOS port: install + start the privileged engine daemon.
# Run with sudo:  sudo host/macos/install-engine-daemon.sh
# Idempotent: safe to re-run after pulling repo changes (plist is re-copied).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$HERE/local.mdd.engine.plist"
PLIST_DST="/Library/LaunchDaemons/local.mdd.engine.plist"

mkdir -p /Users/linwayne/mdd-macos-build/data/logs
cp "$PLIST_SRC" "$PLIST_DST"
chown root:wheel "$PLIST_DST"
chmod 644 "$PLIST_DST"

launchctl bootout system/local.mdd.engine 2>/dev/null || true
launchctl bootstrap system "$PLIST_DST"
launchctl enable system/local.mdd.engine
sleep 1
launchctl print system/local.mdd.engine | grep -E 'state|pid' || true
echo "engine daemon installed; socket: /Users/linwayne/mdd-macos-build/data/run/engine.sock"
