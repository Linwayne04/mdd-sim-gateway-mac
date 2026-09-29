#!/bin/bash
# install-launchd.sh — render + install the launchd jobs for the macOS port:
#   root LaunchDaemon  local.mdd.engine   (privileged engine daemon, socket-driven)
#   user LaunchAgent   local.mdd.control  (control plane, runs as the login user)
#
# Usage:
#   host/macos/install-launchd.sh [--no-autostart]     # install both (daemon part
#                                                      # re-invokes itself through
#                                                      # sudo when needed)
#   host/macos/install-launchd.sh [--no-autostart] --daemon   # root daemon only
#   host/macos/install-launchd.sh [--no-autostart] --agent    # user agent only
#
# --no-autostart installs the jobs but `launchctl disable`s them and leaves
# them unloaded: they stay stopped across reboots until you start them by hand:
#   sudo launchctl enable system/local.mdd.engine
#   sudo launchctl bootstrap system /Library/LaunchDaemons/local.mdd.engine.plist
#   launchctl enable gui/$(id -u)/local.mdd.control
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.mdd.control.plist
#
# Idempotent: safe to re-run after pulling repo changes (plists are re-rendered
# and the services kickstarted).
set -euo pipefail

AUTOSTART=1
ARGS=()
for a in "$@"; do
  case "$a" in
    --no-autostart) AUTOSTART=0 ;;
    *) ARGS+=("$a") ;;
  esac
done
if [ "${#ARGS[@]}" -eq 0 ]; then
  set -- all
else
  set -- "${ARGS[@]}"
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
BUILD_ROOT="${MDD_BUILD:-$HOME/mdd-macos-build}"

if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
  # Rendered agent must belong to the invoking user, not root.
  USER_NAME="$SUDO_USER"
  USER_HOME="$(eval echo "~$SUDO_USER")"
else
  USER_NAME="$(id -un)"
  USER_HOME="$HOME"
fi

render() {
  # render <template> <dest> — strip the template comment block and substitute
  # path placeholders.
  sed -e '/<!--/,/-->/d' \
      -e "s|@HOME@|$USER_HOME|g" \
      -e "s|@USER@|$USER_NAME|g" \
      -e "s|@REPO@|$REPO|g" \
      -e "s|@BUILD@|$BUILD_ROOT|g" \
      "$1" > "$2"
}

bootstrap_retry() {
  # bootstrap <domain> <plist> — bootout is asynchronous; retry a few times.
  local domain="$1" plist="$2" attempt=1
  while [ "$attempt" -le 4 ]; do
    if launchctl bootstrap "$domain" "$plist" 2>/dev/null; then
      return 0
    fi
    sleep 2
    attempt=$((attempt+1))
  done
  echo "bootstrap failed for $plist in $domain" >&2
  return 1
}

install_daemon() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "daemon install needs root — re-invoking via sudo"
    if [ "$AUTOSTART" -eq 1 ]; then
      exec sudo MDD_BUILD="$BUILD_ROOT" "$HERE/install-launchd.sh" --daemon
    else
      exec sudo MDD_BUILD="$BUILD_ROOT" "$HERE/install-launchd.sh" --no-autostart --daemon
    fi
  fi
  local dst=/Library/LaunchDaemons/local.mdd.engine.plist
  mkdir -p "$BUILD_ROOT/data/logs"
  render "$HERE/local.mdd.engine.plist" "$dst"
  chown root:wheel "$dst"
  chmod 644 "$dst"
  launchctl bootout system/local.mdd.engine 2>/dev/null || true
  if [ "$AUTOSTART" -eq 1 ]; then
    launchctl enable system/local.mdd.engine
    bootstrap_retry system "$dst"
  else
    # Disabled services must not be bootstrapped (launchd refuses); install
    # the plist, stop any running instance, and leave it for a manual
    # `launchctl enable && launchctl bootstrap` when wanted.
    launchctl disable system/local.mdd.engine
    echo "engine daemon installed but DISABLED at boot (--no-autostart)"
    echo "to start it later:"
    echo "  sudo launchctl enable system/local.mdd.engine"
    echo "  sudo launchctl bootstrap system $dst"
    return 0
  fi
  sleep 1
  launchctl print system/local.mdd.engine | grep -E 'state|pid' || true
  echo "engine daemon installed; socket: $BUILD_ROOT/data/run/engine.sock"
}

install_agent() {
  local dst="$USER_HOME/Library/LaunchAgents/local.mdd.control.plist"
  mkdir -p "$USER_HOME/Library/LaunchAgents" "$BUILD_ROOT/logs"
  render "$HERE/local.mdd.control.plist" "$dst"
  chmod 644 "$dst"
  local uid
  uid="$(id -u "$USER_NAME")"
  launchctl bootout "gui/$uid/local.mdd.control" 2>/dev/null || true
  if [ "$AUTOSTART" -eq 1 ]; then
    launchctl enable "gui/$uid/local.mdd.control"
    bootstrap_retry "gui/$uid" "$dst"
  else
    launchctl disable "gui/$uid/local.mdd.control"
    echo "control agent installed but DISABLED at boot (--no-autostart)"
    echo "to start it later:"
    echo "  launchctl enable gui/$uid/local.mdd.control"
    echo "  launchctl bootstrap gui/$uid $dst"
    return 0
  fi
  sleep 1
  launchctl print "gui/$uid/local.mdd.control" | grep -E 'state|pid' || true
  echo "control agent installed; WebUI: https://127.0.0.1:8443"
}

case "${1:-all}" in
  --daemon) install_daemon ;;
  --agent)  install_agent ;;
  all)      install_daemon; install_agent ;;
  *) echo "usage: $0 [--no-autostart] [--daemon|--agent]" >&2; exit 2 ;;
esac
