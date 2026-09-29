#!/bin/bash
# mdd-sim-gateway macOS port: install + start the privileged engine daemon.
#
# Deprecated shim — the real implementation now lives in install-launchd.sh,
# which renders both plists from templates (no hardcoded paths) and installs
# the control agent alongside the daemon. Kept so existing docs/habits keep
# working:
#   sudo host/macos/install-engine-daemon.sh   ==   host/macos/install-launchd.sh --daemon
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/install-launchd.sh" --daemon
