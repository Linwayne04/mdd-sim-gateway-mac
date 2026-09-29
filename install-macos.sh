#!/bin/bash
# install-macos.sh — one-click installer / lifecycle manager for the macOS
# native port of mdd-sim-gateway (LaunchDaemon engine, utun tunnel, native
# Asterisk). Intel (x86_64) and Apple Silicon (arm64) are both supported:
# everything is compiled from source on the host, so the architecture is
# handled by the toolchain, and Homebrew/MacPorts prefixes are auto-detected.
#
# Usage:
#   ./install-macos.sh install [--no-launchd]   # full install (default command)
#   ./install-macos.sh status
#   ./install-macos.sh logs
#   ./install-macos.sh uninstall [--purge]      # --purge also deletes the build root
#
# Run as a normal user — the script only escalates via sudo for the root
# LaunchDaemon step. Every build step is idempotent (existing outputs are
# skipped), so re-running after `git pull` is safe.
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
CMD="${1:-install}"
case "$CMD" in
  status)
    launchctl print system/local.mdd.engine 2>/dev/null | grep -E 'state|pid' \
      || echo "engine daemon: not loaded"
    uid="$(id -u)"
    launchctl print "gui/$uid/local.mdd.control" 2>/dev/null | grep -E 'state|pid' \
      || echo "control agent: not loaded"
    [ -S "$BUILD_ROOT/data/run/engine.sock" ] \
      && echo "engine socket: $BUILD_ROOT/data/run/engine.sock" \
      || echo "engine socket: (missing)"
    exit 0
    ;;
  logs)
    for f in "$BUILD_ROOT/logs/control.log" "$BUILD_ROOT/data/logs/engine-daemon.log"; do
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
  install)
    [ "${2:-}" = "--no-launchd" ] && NO_LAUNCHD=1 || NO_LAUNCHD=0
    ;;
  *) echo "usage: $0 [install [--no-launchd] | status | logs | uninstall [--purge]]" >&2; exit 2 ;;
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
# 6. launchd (the only root step)
# ---------------------------------------------------------------------------
if [ "$NO_LAUNCHD" = 1 ]; then
  warn "skipping launchd install (--no-launchd)"
  warn "finish with: sudo $MACOS_DIR/install-launchd.sh"
elif [ -t 0 ] || sudo -n true 2>/dev/null; then
  info "installing launchd jobs (root daemon + user agent)"
  sudo "$MACOS_DIR/install-launchd.sh" --daemon
  "$MACOS_DIR/install-launchd.sh" --agent
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
