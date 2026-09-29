#!/bin/bash
# fetch-sources.sh — download pinned upstream sources for the macOS native build.
#
# Usage: host/macos/build/fetch-sources.sh [BUILD_ROOT]   (default: $MDD_BUILD or ~/mdd-macos-build)
#
# Fetches (idempotent — existing checkouts/tarballs with matching markers are kept):
#   pjproject  (MddIdd/pjproject-sysmocom-mirror @ 20537ab1)
#   asterisk   (MddIdd/asterisk-sysmocom-mirror @ d231cb2 + cherry-pick f1b60dc)
#   pcsc-lite 2.3.3 + CCID 1.6.2 tarballs  (Phase 4 modem/vpcd path; sha256-verified)
#   opencore-amr 0.1.6 + vo-amrwbenc 0.1.3 tarballs (AMR codecs; sha256-verified)
#
# Every revision and tarball hash is pinned — do not float them.
set -uo pipefail

BUILD_ROOT="${1:-${MDD_BUILD:-$HOME/mdd-macos-build}}"
mkdir -p "$BUILD_ROOT/src"
cd "$BUILD_ROOT"

echo "== fetch-sources: BUILD_ROOT=$BUILD_ROOT =="

fetch_repo() {
  local url="$1" dest="$2" rev="$3" attempt=1
  if [ -d "$dest/.git" ]; then
    echo "skip $dest (already fetched)"
    return 0
  fi
  while [ "$attempt" -le 3 ]; do
    rm -rf "$dest"
    git init -q "$dest"
    git -C "$dest" remote add origin "$url"
    if git -C "$dest" -c http.version=HTTP/1.1 fetch --depth=1 -q origin "$rev" && \
       git -C "$dest" checkout -q --detach FETCH_HEAD; then
      echo "OK $dest @ $(git -C "$dest" rev-parse --short HEAD)"
      return 0
    fi
    echo "retry $attempt for $dest" >&2
    attempt=$((attempt+1)); sleep 5
  done
  echo "FAILED $dest" >&2
  return 1
}

FAILED=0

fetch_repo https://github.com/MddIdd/pjproject-sysmocom-mirror.git pjproject 20537ab1e6beb2baf9ec15599e0217a601cf7ce5 &
P1=$!
fetch_repo https://github.com/MddIdd/asterisk-sysmocom-mirror.git asterisk d231cb2c658545773fcd5ebde787219b9ef6566 &
P2=$!
wait $P1 || FAILED=1
wait $P2 || FAILED=1

# cherry-pick the extra commit in asterisk (tagged so shallow/rewritten hashes
# don't fool the idempotency guard — a cherry-pick gets a NEW commit hash)
if [ -d asterisk/.git ] && ! git -C asterisk rev-parse -q --verify refs/tags/mdd-f1b60dc-pick > /dev/null; then
  (cd asterisk && git config user.email "build@vowifi" && git config user.name "vowifi build" \
    && git -c http.version=HTTP/1.1 fetch --depth=2 -q origin f1b60dcd9568c4045512fd0d8b619b9fb91a7f35 \
    && git cherry-pick -X theirs f1b60dcd9568c4045512fd0d8b619b9fb91a7f35 \
    && git tag mdd-f1b60dc-pick \
    && echo "CHERRY-PICK OK") || { echo "CHERRY-PICK FAILED" >&2; FAILED=1; }
fi

fetch_tarball() {
  local url="$1" out="$2" sha="$3"
  if [ -f "src/$out" ]; then
    if echo "$sha  src/$out" | shasum -a 256 -c - > /dev/null 2>&1; then
      echo "skip src/$out (sha256 ok)"
      return 0
    fi
    echo "src/$out hash mismatch, re-downloading" >&2
  fi
  curl -fL --retry 3 -o "src/$out" "$url" \
    && echo "$sha  src/$out" | shasum -a 256 -c - \
    && echo "OK src/$out" \
    || { echo "FAILED src/$out" >&2; return 1; }
}

# pcsc-lite + CCID are Phase 4 (modem/vpcd) inputs — a fetch failure here is a
# warning, not a fatal error, because USB readers work via Apple's PCSC.framework.
fetch_tarball "https://github.com/LudovicRousseau/PCSC/archive/refs/tags/2.3.3.tar.gz" \
  pcsc-2.3.3.tar.gz \
  00b667aa71504ed1d39a48ad377de048c70dbe47229e8c48a3239ab62979c70f || warn_skip=1
fetch_tarball "https://ccid.apdu.fr/files/ccid-1.8.4.tar.xz" \
  ccid-1.8.4.tar.xz \
  4ff98151a7feb828a711e2f9d68c6b6065a97c597a81c2cbbd11d7f7edcfe743 || warn_skip=1
[ -n "${warn_skip:-}" ] && echo "note: pcsc-lite/CCID fetch failed (only needed for the Phase 4 modem path)"
fetch_tarball "https://downloads.sourceforge.net/project/opencore-amr/opencore-amr/opencore-amr-0.1.6.tar.gz" \
  opencore-amr-0.1.6.tar.gz \
  483eb4061088e2b34b358e47540b5d495a96cd468e361050fae615b1809dc4a1 || FAILED=1
fetch_tarball "https://downloads.sourceforge.net/project/opencore-amr/vo-amrwbenc/vo-amrwbenc-0.1.3.tar.gz" \
  vo-amrwbenc-0.1.3.tar.gz \
  5652b391e0f0e296417b841b02987d3fd33e6c0af342c69542cbb016a71d9d4e || FAILED=1

# explode source tarballs the builds expect in extracted form
[ -d src/opencore-amr-0.1.6 ] || (cd src && tar xf opencore-amr-0.1.6.tar.gz)
[ -d src/vo-amrwbenc-0.1.3 ] || (cd src && tar xf vo-amrwbenc-0.1.3.tar.gz)

if [ "$FAILED" -ne 0 ]; then
  echo "fetch-sources: SOME FETCHES FAILED" >&2
  exit 1
fi
echo "fetch-sources: ALL DONE"
