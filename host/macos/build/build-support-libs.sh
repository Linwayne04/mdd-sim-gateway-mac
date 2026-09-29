#!/bin/bash
# build-support-libs.sh — build AMR codec libraries into the local stage prefix.
#
# Usage: host/macos/build/build-support-libs.sh [BUILD_ROOT]
#
# Builds (idempotent — skipped when the staged dylib already exists):
#   opencore-amr 0.1.6  -> $BUILD_ROOT/stage/usr/local (libopencore-amrnb/wb)
#   vo-amrwbenc 0.1.3   -> $BUILD_ROOT/stage/usr/local
#
# These feed Asterisk's codec_amr (--with-opencore-amrnb/--with-opencore-amrwb/
# --with-vo-amrwbenc). pcsc-lite/CCID self-builds (Phase 4 modem/vpcd path) are
# intentionally not here: USB PC/SC readers work via Apple's PCSC.framework.
set -euo pipefail

BUILD_ROOT="${1:-${MDD_BUILD:-$HOME/mdd-macos-build}}"
STAGE="$BUILD_ROOT/stage/usr/local"
SRC="$BUILD_ROOT/src"
JOBS=$(sysctl -n hw.ncpu)

# Package-manager include/lib paths (Homebrew or MacPorts), best effort.
PM_PREFIX="${MDD_PM_PREFIX:-}"
if [ -z "$PM_PREFIX" ]; then
  if command -v brew > /dev/null 2>&1; then
    PM_PREFIX="$(brew --prefix)"
  elif [ -d /opt/local/lib/pkgconfig ]; then
    PM_PREFIX=/opt/local
  fi
fi

export CC=cc CXX=c++
export CFLAGS="${CFLAGS:--O2 -I$PM_PREFIX/include}"
export CPPFLAGS="${CPPFLAGS:--I$PM_PREFIX/include}"
export LDFLAGS="${LDFLAGS:--L$PM_PREFIX/lib}"
export PKG_CONFIG_PATH="$PKG_CONFIG_PATH:$STAGE/lib/pkgconfig"

echo "== build-support-libs: BUILD_ROOT=$BUILD_ROOT =="

build_amr() {
  local dir="$1" lib="$2"
  if ls "$STAGE/lib/${lib}"*.dylib > /dev/null 2>&1; then
    echo "skip $dir (already staged)"
    return 0
  fi
  [ -d "$SRC/$dir" ] || { echo "missing $SRC/$dir — run fetch-sources.sh first" >&2; exit 1; }
  echo "-- building $dir"
  (cd "$SRC/$dir" \
    && ./configure --prefix="$STAGE" \
    && make -j"$JOBS" \
    && make install)
}

build_amr opencore-amr-0.1.6 libopencore-amrnb
build_amr vo-amrwbenc-0.1.3 libvo-amrwbenc

echo "build-support-libs: DONE"
