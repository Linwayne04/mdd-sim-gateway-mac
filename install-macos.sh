#!/bin/bash
# install-macos.sh — one-click installer / lifecycle manager for the macOS
# native port of mdd-sim-gateway (LaunchDaemon engine, utun tunnel, native
# Asterisk). Intel (x86_64) and Apple Silicon (arm64) are both supported:
# everything is compiled from source on the host, so the architecture is
# handled by the toolchain, and Homebrew/MacPorts prefixes are auto-detected.
#
# Usage:
#   ./install-macos.sh install [--no-launchd] [--no-autostart] [--no-egress]   # full install (default)
#   ./install-macos.sh status | logs | diagnose
#   ./install-macos.sh reload | start | stop | restart          # launchd jobs
#   ./install-macos.sh enable-autostart | disable-autostart
#   ./install-macos.sh update [--version X] [--check]           # one-click update
#   ./install-macos.sh uninstall [--purge]      # --purge also deletes the build root
#
# Run as a normal user — the script only escalates via sudo for the root
# LaunchDaemon step. Every build step is idempotent (existing outputs are
# skipped), so re-running after `git pull` is safe.
#
# --no-autostart installs the launchd jobs but disables them at boot
# (they won't start until you `launchctl enable` them); --no-launchd skips
# the launchd step entirely.
#
# Layout:
#   repo (this checkout)   control/, engine/, host/macos/, webui/, patches/
#   BUILD_ROOT             ~/mdd-macos-build  (override with MDD_BUILD)
#     src/  pjproject/  asterisk/  stage/  asterisk-stage/  venv/  data/  logs/
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BUILD_ROOT="${MDD_BUILD:-$HOME/mdd-macos-build}"
MACOS_DIR="$REPO/host/macos"
WEBUI_DIST="$REPO/webui/dist"

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_rst=$'\033[0m'
info() { printf '%s==>%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn() { printf '%swarn:%s %s\n' "$c_yel" "$c_rst" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$c_red" "$c_rst" "$*" >&2; exit 1; }
have() { command -v "$1" > /dev/null 2>&1; }

# If invoked through sudo, drop back to the invoking user for everything
# except the root daemon step (brew/MacPorts refuse to run as root).
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
  USER_HOME="$(eval echo "~$SUDO_USER")"
  exec sudo -u "$SUDO_USER" env HOME="$USER_HOME" \
    MDD_BUILD="${MDD_BUILD:-$USER_HOME/mdd-macos-build}" "$0" "$@"
fi

# ---------------------------------------------------------------------------
# subcommands that don't need the full flow
# ---------------------------------------------------------------------------
DAEMON_LABEL=system/local.mdd.engine

# Root launchctl verbs (daemon domain). Use sudo when it works
# non-interactively, or when a tty allows a prompt; otherwise print the exact
# manual command and return 1 — never hang waiting for a password.
if sudo -n true 2>/dev/null || [ -t 0 ]; then
  SUDO_LCTL=sudo
else
  SUDO_LCTL=""
fi

daemon_lctl() {
  if [ -n "$SUDO_LCTL" ]; then
    $SUDO_LCTL launchctl "$@"
  else
    warn "cannot sudo non-interactively — run manually:"
    warn "  sudo launchctl $*"
    return 1
  fi
}

bootstrap_retry() {
  # bootstrap <domain> <plist>; bootout is async, retry a few times.
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

daemon_bootstrap_retry() {
  # bootstrap_retry through the sudo path (root daemon domain).
  local domain="$1" plist="$2" attempt=1
  while [ "$attempt" -le 4 ]; do
    if daemon_lctl bootstrap "$domain" "$plist" 2>/dev/null; then
      return 0
    fi
    sleep 2
    attempt=$((attempt+1))
  done
  warn "bootstrap failed for $plist in $domain (see manual commands above)"
  return 1
}

job_state() {
  launchctl print "$1" 2>/dev/null | grep -E 'state|pid' || echo "$1: not loaded"
}

start_jobs() {
  local uid; uid="$(id -u)"
  local agent_plist="$HOME/Library/LaunchAgents/local.mdd.control.plist"
  if [ -f /Library/LaunchDaemons/local.mdd.engine.plist ]; then
    daemon_lctl enable system/local.mdd.engine || true
    daemon_bootstrap_retry system /Library/LaunchDaemons/local.mdd.engine.plist || true
  else
    warn "engine daemon plist not installed — run: sudo $MACOS_DIR/install-launchd.sh --daemon"
  fi
  if [ -f "$agent_plist" ]; then
    # Enable BEFORE bootstrap: a disabled service refuses bootstrap.
    launchctl enable "gui/$uid/local.mdd.control" || true
    bootstrap_retry "gui/$uid" "$agent_plist" || warn "control agent bootstrap failed"
  else
    warn "control agent plist not installed — run: $MACOS_DIR/install-launchd.sh --agent"
  fi
}

stop_jobs() {
  local uid; uid="$(id -u)"
  daemon_lctl bootout system/local.mdd.engine || true
  launchctl bootout "gui/$uid/local.mdd.control" 2>/dev/null || true
}

scrub() {
  # Best-effort masking of tokens/secrets in report output.
  sed -E \
    -e 's/([Tt][Oo][Kk][Ee][Nn]|[Ss][Ee][Cc][Rr][Ee][Tt]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn]|[Aa][Pp][Ii][_-]?[Kk][Ee][Yy])=[^[:space:]]+/\1=***/g' \
    -e 's/Bearer[[:space:]]+[0-9a-fA-F]{8,}/Bearer ***/g'
}

diagnose() {
  local uid; uid="$(id -u)"
  {
    echo "== launchd jobs =="
    job_state system/local.mdd.engine
    job_state "gui/$uid/local.mdd.control"
    echo
    echo "== PC/SC readers (USB) =="
    system_profiler -timeout 15 -detailLevel mini SPUSBDataType 2>/dev/null \
      | grep -i -B2 -A2 -E 'ccid|smart.?card|reader|pcsc' \
      || echo "(none found or system_profiler unavailable)"
    echo
    echo "== engine socket =="
    if [ -S "$BUILD_ROOT/data/run/engine.sock" ]; then
      echo "present: $BUILD_ROOT/data/run/engine.sock"
    else
      echo "(missing)"
    fi
    echo
    echo "== sockets (5038 5060 5061 8088 8443 4500) =="
    netstat -an | grep -E '\.(5038|5060|5061|8088|8443|4500) ' || echo "(none listening)"
    echo
    echo "== disk ($BUILD_ROOT) =="
    df -h "$BUILD_ROOT" | tail -1
    echo
    echo "== recent logs =="
    local f
    for f in "$BUILD_ROOT/logs/control.log" "$BUILD_ROOT/data/logs/engine-daemon.log"; do
      echo "-- $f --"
      [ -f "$f" ] && tail -n 30 "$f" || echo "(missing)"
      echo
    done
    echo "== repo ($REPO) =="
    git -C "$REPO" branch --show-current
    git -C "$REPO" log --oneline -3
  } | scrub
}

cmd_update() {
  local version="latest" check=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --version) version="${2:-}"; [ -n "$version" ] || die "--version needs a value"; shift 2 ;;
      --check)   check=1; shift ;;
      *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
  done
  local orch="$BUILD_ROOT/data/orchestrator"
  if [ "$check" -eq 1 ]; then
    if [ -f "$orch/update-request.json" ]; then
      echo "== update-request.json =="
      cat "$orch/update-request.json"
    else
      echo "no update request"
    fi
    if [ -f "$orch/update-status.json" ]; then
      echo "== update-status.json =="
      cat "$orch/update-status.json"
    fi
    exit 0
  fi
  mkdir -p "$orch"
  # Atomic write via temp + mv. Extra keys the WebUI adds (network fields,
  # asset_sizes) are optional — the updater only needs version/repository.
  local tmp="$orch/.update-request.json.tmp"
  printf '{\n  "version": "%s",\n  "repository": "Linwayne04/mdd-sim-gateway-mac",\n  "requested_at": %s,\n  "source": "cli"\n}\n' \
    "$version" "$(date +%s)" > "$tmp"
  mv "$tmp" "$orch/update-request.json"
  echo "update request written: version=$version repository=Linwayne04/mdd-sim-gateway-mac"
  echo "the update daemon (system/local.mdd.update) picks it up within 5 minutes."
  if [ -n "$SUDO_LCTL" ]; then
    $SUDO_LCTL launchctl kickstart system/local.mdd.update \
      && echo "update daemon kickstarted" || warn "kickstart failed — it will still run on its 5-minute interval"
  else
    warn "cannot sudo non-interactively — run manually:"
    warn "  sudo launchctl kickstart system/local.mdd.update"
  fi
}

CMD="${1:-install}"
case "$CMD" in
  status)
    launchctl print system/local.mdd.engine 2>/dev/null | grep -E 'state|pid' \
      || echo "engine daemon: not loaded"
    launchctl print system/local.mdd.orchestrator 2>/dev/null | grep -E 'state|pid' \
      || echo "orchestrator daemon: not loaded"
    uid="$(id -u)"
    launchctl print "gui/$uid/local.mdd.control" 2>/dev/null | grep -E 'state|pid' \
      || echo "control agent: not loaded"
    [ -S "$BUILD_ROOT/data/run/engine.sock" ] \
      && echo "engine socket: $BUILD_ROOT/data/run/engine.sock" \
      || echo "engine socket: (missing)"
    exit 0
    ;;
  logs)
    for f in "$BUILD_ROOT/logs/control.log" "$BUILD_ROOT/data/logs/engine-daemon.log" \
             "$BUILD_ROOT/logs/orchestrator.log"; do
      echo "== $f =="
      [ -f "$f" ] && tail -n 40 "$f" || echo "(missing)"
    done
    exit 0
    ;;
  uninstall)
    PURGE=0
    [ "${2:-}" = "--purge" ] && PURGE=1
    uid="$(id -u)"
    launchctl bootout "gui/$uid/local.mdd.control" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/local.mdd.control.plist"
    if sudo -n true 2>/dev/null || [ -t 0 ]; then
      sudo launchctl bootout system/local.mdd.engine 2>/dev/null || true
      sudo rm -f /Library/LaunchDaemons/local.mdd.engine.plist
      echo "launchd jobs removed"
    else
      warn "could not sudo; remove /Library/LaunchDaemons/local.mdd.engine.plist manually:"
      warn "  sudo launchctl bootout system/local.mdd.engine"
      warn "  sudo rm -f /Library/LaunchDaemons/local.mdd.engine.plist"
    fi
    if [ "$PURGE" -eq 1 ]; then
      rm -rf "$BUILD_ROOT"
      echo "purged $BUILD_ROOT"
    else
      echo "build root kept at $BUILD_ROOT (use --purge to delete)"
    fi
    exit 0
    ;;
  reload)
    uid="$(id -u)"
    info "kickstarting launchd jobs"
    daemon_lctl kickstart -k system/local.mdd.engine || true
    launchctl kickstart -k "gui/$uid/local.mdd.control" 2>/dev/null || true
    job_state system/local.mdd.engine
    job_state "gui/$uid/local.mdd.control"
    exit 0
    ;;
  start)
    info "starting launchd jobs (enable + bootstrap)"
    start_jobs
    exit 0
    ;;
  stop)
    info "stopping launchd jobs (bootout)"
    stop_jobs
    exit 0
    ;;
  restart)
    info "restarting launchd jobs"
    stop_jobs
    start_jobs
    exit 0
    ;;
  enable-autostart)
    uid="$(id -u)"
    info "enabling launchd jobs at login/boot"
    daemon_lctl enable system/local.mdd.engine || true
    launchctl enable "gui/$uid/local.mdd.control" || true
    echo "note: disabled jobs need 'enable' then 'bootstrap' before they run;"
    echo "if a job is currently unloaded, run: $0 start"
    exit 0
    ;;
  disable-autostart)
    uid="$(id -u)"
    info "disabling launchd jobs at login/boot"
    daemon_lctl disable system/local.mdd.engine || true
    launchctl disable "gui/$uid/local.mdd.control" || true
    echo "note: disabled jobs need 'enable' then 'bootstrap' to run again:"
    echo "  sudo launchctl enable system/local.mdd.engine && sudo launchctl bootstrap system /Library/LaunchDaemons/local.mdd.engine.plist"
    echo "  launchctl enable gui/$uid/local.mdd.control && launchctl bootstrap gui/$uid ~/Library/LaunchAgents/local.mdd.control.plist"
    exit 0
    ;;
  diagnose)
    diagnose
    exit 0
    ;;
  update)
    cmd_update "${@:2}"
    exit 0
    ;;
  install)
    NO_LAUNCHD=0; NO_AUTOSTART=0; NO_EGRESS=0
    for a in "${@:2}"; do
      case "$a" in
        --no-launchd)   NO_LAUNCHD=1 ;;
        --no-autostart) NO_AUTOSTART=1 ;;
        --no-egress)    NO_EGRESS=1 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
      esac
    done
    ;;
  *) echo "usage: $0 [install [--no-launchd] [--no-autostart] [--no-egress]] | status | logs | reload | start | stop | restart | enable-autostart | disable-autostart | diagnose | update [--version X] [--check] | uninstall [--purge]" >&2; exit 2 ;;
esac

# ---------------------------------------------------------------------------
# 1. preflight
# ---------------------------------------------------------------------------
info "preflight: checking host"
[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS only (use upstream install.sh on Linux)"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  info "architecture: Intel (x86_64)" ;;
  arm64)   info "architecture: Apple Silicon (arm64)" ;;
  *)       die "unsupported architecture: $ARCH" ;;
esac
xcode-select -p > /dev/null 2>&1 || die "Xcode Command Line Tools missing — run: xcode-select --install"
have git  || die "git not found"
have curl || die "curl not found"
mkdir -p "$BUILD_ROOT/logs" "$BUILD_ROOT/data"

# ---------------------------------------------------------------------------
# 2. package manager + build dependencies
# ---------------------------------------------------------------------------
PM=""; PM_PREFIX=""
if command -v brew > /dev/null 2>&1; then
  PM=brew; PM_PREFIX="$(brew --prefix)"
elif command -v port > /dev/null 2>&1; then
  PM=port; PM_PREFIX=/opt/local
else
  die "neither Homebrew nor MacPorts found — install one first:
       Homebrew:   /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"
       MacPorts:   https://www.macports.org/install.php"
fi
info "package manager: $PM ($PM_PREFIX)"
export PATH="$PM_PREFIX/bin:$PATH"

# name mapping: brew -> macports
BREW_DEPS=(bison flex pkg-config jansson libxml2 sqlite openssl ncurses
           speex speexdsp libogg libvorbis ossp-uuid libsrtp
           python@3.12 node autoconf automake libtool)
PORT_DEPS=(bison flex pkgconfig jansson libxml2 sqlite3 openssl3 ncurses
           speex speexdsp libogg libvorbis ossp-uuid libsrtp
           python312 nodejs22 autoconf automake libtool)

pkg_installed() {
  if [ "$PM" = brew ]; then brew list --versions "$1" > /dev/null 2>&1
  else port installed "$1" > /dev/null 2>&1; fi
}

if [ "$PM" = brew ]; then DEPS=("${BREW_DEPS[@]}"); else DEPS=("${PORT_DEPS[@]}"); fi
MISSING=()
for d in "${DEPS[@]}"; do pkg_installed "$d" || MISSING+=("$d"); done
if [ "${#MISSING[@]}" -gt 0 ]; then
  info "installing missing dependencies: ${MISSING[*]}"
  if [ "$PM" = brew ]; then
    brew install "${MISSING[@]}"
  else
    sudo port install "${MISSING[@]}"
  fi
else
  info "all build dependencies present"
fi
export PKG_CONFIG_PATH="$PM_PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
[ -d "$PM_PREFIX/share/pkgconfig" ] && \
  export PKG_CONFIG_PATH="$PKG_CONFIG_PATH:$PM_PREFIX/share/pkgconfig"

# ---------------------------------------------------------------------------
# 3. sources + builds (idempotent)
# ---------------------------------------------------------------------------
info "step 1/5: fetching pinned sources"
"$MACOS_DIR/build/fetch-sources.sh" "$BUILD_ROOT"

info "step 2/5: building AMR codec support libraries"
MDD_PM_PREFIX="$PM_PREFIX" "$MACOS_DIR/build/build-support-libs.sh" "$BUILD_ROOT"

info "step 3/5: building Asterisk (sysmocom fork, native — this is the long step)"
MDD_PM_PREFIX="$PM_PREFIX" "$MACOS_DIR/build/build-asterisk.sh" "$BUILD_ROOT"

# ---------------------------------------------------------------------------
# 4. python venv for control plane + engine
# ---------------------------------------------------------------------------
info "step 4/5: python venv"
PY=""
for cand in python3.12 "$PM_PREFIX/opt/python@3.12/bin/python3.12" "$PM_PREFIX/bin/python3.12"; do
  command -v "$cand" > /dev/null 2>&1 && { PY="$cand"; break; }
  [ -x "$cand" ] && { PY="$cand"; break; }
done
[ -n "$PY" ] || die "python3.12 not found after dependency install"
VENV="$BUILD_ROOT/venv"
if [ ! -x "$VENV/bin/python" ]; then
  info "creating venv with $PY"
  "$PY" -m venv "$VENV" || die "venv creation failed (MacPorts: sudo port install py312-virtualenv, then re-run)"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$REPO/control/requirements.txt"
else
  info "venv exists — checking key imports"
  if ! "$VENV/bin/python" -c "import fastapi, smartcard, panoramisk, yaml, cryptography" 2>/dev/null; then
    warn "venv exists but imports fail — reinstalling requirements"
    "$VENV/bin/pip" install --quiet -r "$REPO/control/requirements.txt"
  fi
fi

# ---------------------------------------------------------------------------
# 5. webui build
# ---------------------------------------------------------------------------
info "step 5/5: webui"
if [ -f "$WEBUI_DIST/index.html" ]; then
  info "webui already built ($WEBUI_DIST) — skipping (delete it to force rebuild)"
else
  command -v node > /dev/null 2>&1 || die "node not found after dependency install"
  if ! command -v npm > /dev/null 2>&1; then
    info "npm not bundled with this node — bootstrapping npm 11.6.2 into the build root"
    NPM_TGZ="$BUILD_ROOT/npm.tgz"
    if ! echo "585f95094ee5cb2788ee11d90f2a518a7c9ef6e083fa141d0b63ca3383675a20  $NPM_TGZ" | shasum -a 256 -c - > /dev/null 2>&1; then
      curl -fL --retry 3 -o "$NPM_TGZ" "https://registry.npmjs.org/npm/-/npm-11.6.2.tgz"
      echo "585f95094ee5cb2788ee11d90f2a518a7c9ef6e083fa141d0b63ca3383675a20  $NPM_TGZ" | shasum -a 256 -c - \
        || die "npm tarball hash mismatch"
    fi
    mkdir -p "$BUILD_ROOT/package" "$BUILD_ROOT/bin"
    tar xf "$NPM_TGZ" -C "$BUILD_ROOT/package" --strip-components 1
    printf '#!/bin/bash\nexec %s %s/bin/npm-cli.js "$@"\n' "$(command -v node)" "$BUILD_ROOT/package" \
      > "$BUILD_ROOT/bin/npm"
    chmod +x "$BUILD_ROOT/bin/npm"
  fi
  (cd "$REPO/webui" && npm ci && npm run build) || die "webui build failed"
fi

# ---------------------------------------------------------------------------
# 6. country-egress binaries (sing-box / Xray)
# ---------------------------------------------------------------------------
if [ "$NO_EGRESS" = 1 ]; then
  warn "skipping country-egress binaries (--no-egress)"
else
  info "egress: fetching sing-box / Xray darwin binaries"
  "$MACOS_DIR/build/fetch-egress.sh" "$BUILD_ROOT"
fi

# ---------------------------------------------------------------------------
# 7. launchd (the only root step)
# ---------------------------------------------------------------------------
if [ "$NO_LAUNCHD" = 1 ]; then
  warn "skipping launchd install (--no-launchd)"
  warn "finish with: sudo $MACOS_DIR/install-launchd.sh"
elif [ -t 0 ] || sudo -n true 2>/dev/null; then
  info "installing launchd jobs (root daemons + user agent)"
  NO_AUTO_ARG=""; [ "$NO_AUTOSTART" = 1 ] && NO_AUTO_ARG="--no-autostart"
  sudo "$MACOS_DIR/install-launchd.sh" $NO_AUTO_ARG --daemon
  if [ "$NO_EGRESS" = 1 ]; then
    warn "egress skipped — orchestrator daemon not installed (--no-egress)"
  else
    sudo "$MACOS_DIR/install-launchd.sh" $NO_AUTO_ARG --orchestrator
  fi
  "$MACOS_DIR/install-launchd.sh" $NO_AUTO_ARG --agent
  if [ "$NO_AUTOSTART" = 1 ]; then
    info "jobs installed but disabled at boot — to start them later:"
    echo "  sudo launchctl enable system/local.mdd.engine && sudo launchctl bootstrap system /Library/LaunchDaemons/local.mdd.engine.plist"
    if [ "$NO_EGRESS" != 1 ]; then
      echo "  sudo launchctl enable system/local.mdd.orchestrator && sudo launchctl bootstrap system /Library/LaunchDaemons/local.mdd.orchestrator.plist"
    fi
    echo "  launchctl enable gui/$(id -u)/local.mdd.control && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.mdd.control.plist"
  fi
else
  warn "launchd install needs sudo and no terminal is attached."
  warn "finish the install with:"
  warn "  sudo $MACOS_DIR/install-launchd.sh --daemon"
  warn "  $MACOS_DIR/install-launchd.sh --agent"
fi

cat <<EOF

${c_grn}==> install-macos: done${c_rst}
  build root:  $BUILD_ROOT
  engine:      $BUILD_ROOT/asterisk-stage/usr/local/sbin/asterisk
  WebUI:       https://127.0.0.1:8443   (open it and create the admin account)

Next: plug in a USB PC/SC reader with a SIM that has Wi-Fi Calling enabled,
add a line in the WebUI, and start it.
EOF
