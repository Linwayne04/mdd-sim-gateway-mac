#!/bin/bash
# fetch-egress.sh — download pinned sing-box / Xray-core darwin release binaries
# for the macOS native country-egress path (host/mdd_orchestrator.py spawns them
# as plain subprocesses, the way the Linux port spawned them in Docker).
#
# Usage: host/macos/build/fetch-egress.sh [BUILD_ROOT]   (default: $MDD_BUILD or ~/mdd-macos-build)
#
# Layout produced:
#   $BUILD_ROOT/egress/dist/    cached release archives (reused on re-run)
#   $BUILD_ROOT/egress/bin/     sing-box, xray  (host-arch binaries, verified once)
#
# Idempotent: when $BUILD_ROOT/egress/bin/sing-box already exists and reports the
# pinned version, the whole step is skipped.
set -euo pipefail

BUILD_ROOT="${1:-${MDD_BUILD:-$HOME/mdd-macos-build}}"
DIST="$BUILD_ROOT/egress/dist"
BIN="$BUILD_ROOT/egress/bin"

SINGBOX_VERSION="1.13.15"
XRAY_VERSION="26.3.27"

# Archive sha256, pinned. The amd64 digests were recorded on an Intel host from
# these exact release assets; the arm64 digests were recorded from the same
# release pages (v1.13.15 / v26.3.27) — do not float them.
SINGBOX_SHA256_AMD64="817e04f90f941b718fedd965ff05bfe72abfcc62952888b01751a6dec5547e14"
SINGBOX_SHA256_ARM64="3452d866834c9572389e5ca73e60d4ee45a7d5b79332188c9a9e533c5fd40a6d"
XRAY_SHA256_AMD64="f5b0471d3459eff1b82e48af0aeac186abcc3298210070afbbbd8437a4e8b203"
XRAY_SHA256_ARM64="2e93a67e8aa1936ecefb307e120830fcbd4c643ab9b1c46a2d0838d5f8409eaf"

case "$(uname -m)" in
  x86_64) ARCH="amd64" ;;
  arm64)  ARCH="arm64" ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

eval "SINGBOX_SHA256=\$SINGBOX_SHA256_$(echo "$ARCH" | tr 'a-z' 'A-Z')"
eval "XRAY_SHA256=\$XRAY_SHA256_$(echo "$ARCH" | tr 'a-z' 'A-Z')"

SINGBOX_URL="https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-darwin-${ARCH}.tar.gz"
if [ "$ARCH" = "amd64" ]; then
  XRAY_URL="https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-macos-64.zip"
else
  XRAY_URL="https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-macos-arm64-v8a.zip"
fi

echo "== fetch-egress: BUILD_ROOT=$BUILD_ROOT arch=$ARCH =="
mkdir -p "$DIST" "$BIN"

# Skip when the host-arch binary is already installed and reports the pin.
if [ -x "$BIN/sing-box" ] && "$BIN/sing-box" version 2>/dev/null | grep -q "version ${SINGBOX_VERSION}"; then
  echo "skip $BIN/sing-box (already at ${SINGBOX_VERSION})"
else
  archive="$DIST/$(basename "$SINGBOX_URL")"
  if ! echo "$SINGBOX_SHA256  $archive" | shasum -a 256 -c - > /dev/null 2>&1; then
    echo "downloading $(basename "$SINGBOX_URL")"
    curl -fL --retry 3 -o "$archive" "$SINGBOX_URL"
    echo "$SINGBOX_SHA256  $archive" | shasum -a 256 -c - || { echo "sing-box archive hash mismatch" >&2; exit 1; }
  else
    echo "skip $(basename "$archive") (sha256 ok)"
  fi
  tmp="$(mktemp -d)"
  tar xzf "$archive" -C "$tmp"
  install -m 0755 "$tmp/sing-box-${SINGBOX_VERSION}-darwin-${ARCH}/sing-box" "$BIN/sing-box"
  rm -rf "$tmp"
  "$BIN/sing-box" version | grep "version ${SINGBOX_VERSION}" \
    || { echo "installed sing-box does not report ${SINGBOX_VERSION}" >&2; exit 1; }
  echo "OK $BIN/sing-box"
fi

if [ -x "$BIN/xray" ] && "$BIN/xray" version 2>/dev/null | grep -q "Xray ${XRAY_VERSION}"; then
  echo "skip $BIN/xray (already at ${XRAY_VERSION})"
else
  archive="$DIST/$(basename "$XRAY_URL")"
  if ! echo "$XRAY_SHA256  $archive" | shasum -a 256 -c - > /dev/null 2>&1; then
    echo "downloading $(basename "$XRAY_URL")"
    curl -fL --retry 3 -o "$archive" "$XRAY_URL"
    echo "$XRAY_SHA256  $archive" | shasum -a 256 -c - || { echo "xray archive hash mismatch" >&2; exit 1; }
  else
    echo "skip $(basename "$archive") (sha256 ok)"
  fi
  tmp="$(mktemp -d)"
  unzip -q -o "$archive" -d "$tmp"
  install -m 0755 "$tmp/xray" "$BIN/xray"
  rm -rf "$tmp"
  "$BIN/xray" version | grep "Xray ${XRAY_VERSION}" \
    || { echo "installed xray does not report ${XRAY_VERSION}" >&2; exit 1; }
  echo "OK $BIN/xray"
fi

echo "fetch-egress: ALL DONE"
