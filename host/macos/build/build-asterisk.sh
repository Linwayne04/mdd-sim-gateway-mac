#!/bin/bash
# build-asterisk.sh — build the sysmocom Asterisk fork natively on macOS and
# stage it under BUILD_ROOT/asterisk-stage.
#
# Usage: host/macos/build/build-asterisk.sh [BUILD_ROOT]
#   BUILD_ROOT  default: $MDD_BUILD or ~/mdd-macos-build
#
# Idempotent: if a staged asterisk binary already exists, the build is skipped
# (delete asterisk-stage to force a rebuild). Run fetch-sources.sh first.
#
# Applied on top of the pristine sources, after every distclean (mirrors the
# engine/Dockerfile asterisk build, in the same order):
#   0) engine/patches/asterisk/*.py — the 9 upstream python patches (SMS/USSD
#      dialplan, ...), with the hardcoded /home/asterisk-build/asterisk prefix
#      rewritten to this tree
#   1) main/Makefile          — Darwin bundled-pjproject link (-all_load + archive
#                               -L paths + Foundation/AppKit; upstream leaves
#                               $(PJPROJECT_LIB) empty -> SIGSEGV at boot)
#   2) main/xml.c             — libxml2 >= 2.13 ID-attribute re-registration fix
#   3) patches/asterisk/*.patch from the repo — 01 res_pjproject.c sin_len
#                               (WebRTC ICE candidate memcmp fix), 02 the Darwin
#                               userspace SIP IPsec dataplane (volte.c sipsec.json
#                               export + netlink_xfrm stubs + build fixes)
#   4) sh bootstrap.sh        — regenerate configure after 02's configure.ac
#                               change (remove AST_POLL_COMPAT); Dockerfile
#                               runs bootstrap unconditionally too
# res_geolocation is disabled (GNU-ld blob embedding; unbuildable on macOS) and
# verified absent from MENUSELECT_BUILD_DEPS.
set -euo pipefail

BUILD_ROOT="${1:-${MDD_BUILD:-$HOME/mdd-macos-build}}"
SRC="$BUILD_ROOT/asterisk"
PJPROJECT="$BUILD_ROOT/pjproject"
STAGE="$BUILD_ROOT/stage/usr/local"
AST_STAGE="$BUILD_ROOT/asterisk-stage"
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"

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
export PKG_CONFIG_PATH="$PM_PREFIX/lib/pkgconfig:$STAGE/lib/pkgconfig"
[ -d "$PM_PREFIX/share/pkgconfig" ] && \
  export PKG_CONFIG_PATH="$PKG_CONFIG_PATH:$PM_PREFIX/share/pkgconfig"

AST="$AST_STAGE/usr/local/sbin/asterisk"
# .built-rev stamp: records which ASTERISK_REV the staged binary was built
# from (written by fetch-sources.sh into src/.pinned-revs). A stamp/binary
# mismatch after a pin change forces a rebuild; a binary with no stamp is a
# legacy build — stamp it in place and keep skipping (no rebuild).
PINNED_REV="$(grep ^ASTERISK_REV= "$BUILD_ROOT/src/.pinned-revs" 2>/dev/null | cut -d= -f2)"
BUILT_REV_STAMP="$AST_STAGE/.built-rev"
if [ -x "$AST" ]; then
  if [ ! -f "$BUILT_REV_STAMP" ]; then
    echo "== build-asterisk: already staged ($AST) — no stamp (legacy build); stamping, not rebuilding =="
    echo "$PINNED_REV" > "$BUILT_REV_STAMP" 2>/dev/null || true
    exit 0
  fi
  if [ -n "$PINNED_REV" ] && [ "$(cat "$BUILT_REV_STAMP" 2>/dev/null)" != "$PINNED_REV" ]; then
    echo "== build-asterisk: staged binary is at '$(cat "$BUILT_REV_STAMP" 2>/dev/null)', pin is '$PINNED_REV' — REBUILDING =="
  else
    echo "== build-asterisk: already staged ($AST) — skipping =="
    exit 0
  fi
fi
[ -d "$SRC/.git" ] || { echo "missing $SRC — run fetch-sources.sh first" >&2; exit 1; }
[ -d "$PJPROJECT/.git" ] || { echo "missing $PJPROJECT — run fetch-sources.sh first" >&2; exit 1; }

JOBS=$(sysctl -n hw.ncpu)

cd "$SRC"

make distclean > /dev/null 2>&1 || true
ln -sfn "$PJPROJECT" third-party/pjproject/source

# --- mdd-sim-gateway macOS port: source patches (applied after every distclean,
# before configure, mirroring the engine/Dockerfile asterisk build) ---
# 0) engine/patches/asterisk/*.py — the 9 upstream python patches (SMS/USSD
#    dialplan, ...), applied like Dockerfile's `for p in .../*.py` loop. The
#    scripts hardcode /home/asterisk-build/asterisk as the tree root; sed the
#    prefix onto a scratch copy (never rewrite the repo files) and run those.
#    The scripts self-check their own markers, so this step is idempotent.
PY_PATCH_SRC="$REPO/engine/patches/asterisk"
[ -d "$PY_PATCH_SRC" ] || { echo "missing $PY_PATCH_SRC" >&2; exit 1; }
PY_PATCH_TMP="$(mktemp -d "${TMPDIR:-/tmp}/mdd-ast-pypatches.XXXXXX")"
trap 'rm -rf "$PY_PATCH_TMP"' EXIT
for pf in "$PY_PATCH_SRC"/*.py; do
  [ -e "$pf" ] || continue
  sed "s|/home/asterisk-build/asterisk|$SRC|g" "$pf" > "$PY_PATCH_TMP/$(basename "$pf")"
done
for pf in "$PY_PATCH_TMP"/*.py; do
  echo "applying engine patch $(basename "$pf")"
  python3 "$pf" || { echo "python patch $pf FAILED" >&2; exit 1; }
done

# 1) main/Makefile, Darwin branch of the bundled-pjproject link: upstream links
#    libasteriskpj.dylib with an EMPTY $(PJPROJECT_LIB), so _pj_init etc. stay
#    undefined (lazy-bound to NULL -> SIGSEGV at every boot in ast_pj_init).
#    Mirror the Linux whole-archive list with -all_load + archive -L paths, and
#    add -framework Foundation -framework AppKit (whole-archived
#    libpjmedia-videodev references NSApplication).
#    The replacement is anchored on the line that sets ASTPJ_LIB for Darwin.
python3 - <<'PYEOF'
p = 'main/Makefile'
s = open(p).read()
if 'PJPROJECT_LDLIBS_DARWIN' in s:
    print("main/Makefile already patched")
else:
    anchor = "else # Darwin\nASTPJ_LIB:=libasteriskpj.dylib\n"
    assert anchor in s, "main/Makefile Darwin anchor not found"
block = anchor + '''
# mdd-sim-gateway macOS port: upstream's Darwin link line uses $(PJPROJECT_LIB),
# which is empty - the bundled pjproject archives were never linked, leaving
# _pj_init/_pj_log_* undefined (lazy-bound to NULL; the first call in
# ast_pj_init jumped to address 0 and crashed Asterisk at every boot). Mirror
# the Linux whole-archive list; GNU-ld's --whole-archive has no macOS
# equivalent, so use -all_load (affects .a archives only, not the dylibs also
# on this line). PJ_LDFLAGS is empty too, so add the archive search paths.
PJPROJECT_LDFLAGS_DARWIN := \\
-L$(PJDIR)/pjlib/lib \\
-L$(PJDIR)/pjlib-util/lib \\
-L$(PJDIR)/pjnath/lib \\
-L$(PJDIR)/pjmedia/lib \\
-L$(PJDIR)/pjsip/lib \\
-L$(PJDIR)/third_party/lib

PJPROJECT_LDLIBS_DARWIN := \\
-all_load \\
$(PJSUA_LIB_LDLIB) \\
$(PJSIP_UA_LDLIB) \\
$(PJSIP_SIMPLE_LDLIB) \\
$(PJSIP_LDLIB) \\
$(PJNATH_LDLIB) \\
$(PJMEDIA_CODEC_LDLIB) \\
$(PJMEDIA_VIDEODEV_LDLIB) \\
$(PJMEDIA_AUDIODEV_LDLIB) \\
$(PJMEDIA_LDLIB) \\
$(PJLIB_UTIL_LDLIB) \\
$(PJLIB_LDLIB) \\
$(APP_THIRD_PARTY_LIBS) \\
$(APP_THIRD_PARTY_EXT)

$(ASTPJ_LIB): _ASTLDFLAGS+=$(PJPROJECT_LDFLAGS_DARWIN)
'''
    s = s.replace(anchor, block, 1)
    old_lib = '$(ASTPJ_LIB): LIBS+=$(PJPROJECT_LIB) $(OPENSSL_LIB) $(UUID_LIB) -lm -lpthread $(RT_LIB)'
    if old_lib in s:
        s = s.replace(old_lib,
            '$(ASTPJ_LIB): LIBS+=$(PJPROJECT_LDLIBS_DARWIN) $(OPENSSL_LIB) $(UUID_LIB) -lm -lpthread $(RT_LIB) -framework Foundation -framework AppKit')
    open(p, 'w').write(s)
    print("main/Makefile patched")
PYEOF
# 2) main/xml.c ast_xml_set_attribute: libxml2 >= 2.13 re-registers attributes
#    flagged type ID inside xmlSetProp (xmlAddIDSafe -> xmlAddIDInternal,
#    dereferencing attr->id). xmldoc documentation nodes can carry stale ID
#    flags with a dangling attr->id -> SIGSEGV in xmlAddIDInternal during
#    module load with libxml2 2.15 (MacPorts). Normalize before setting.
python3 - <<'PYEOF'
p = 'main/xml.c'
s = open(p).read()
if 'mdd-sim-gateway macOS port: libxml2 >= 2.13' in s:
    print("main/xml.c already patched")
    raise SystemExit
anchor = '''int ast_xml_set_attribute(struct ast_xml_node *node, const char *name, const char *value)
{
	if (!name || !value) {
		return -1;
	}

	if (!xmlSetProp((xmlNode *) node, (xmlChar *) name, (xmlChar *) value)) {
		return -1;
	}

	return 0;
}'''
assert anchor in s, "main/xml.c ast_xml_set_attribute anchor not found"
block = '''int ast_xml_set_attribute(struct ast_xml_node *node, const char *name, const char *value)
{
	if (!name || !value) {
		return -1;
	}

#if LIBXML_VERSION >= 21300
	/* mdd-sim-gateway macOS port: libxml2 >= 2.13 made xmlSetProp re-register
	 * attributes flagged as type ID, dereferencing attr->id (via xmlAddIDSafe).
	 * xmldoc documentation nodes can carry stale ID flags whose attr->id no
	 * longer points at a live xmlID; the first such set crashes the loader
	 * (SIGSEGV in xmlAddIDInternal, observed with libxml2 2.15). None of the
	 * trees Asterisk mutates use real ID attributes, so normalize the flags
	 * before setting. */
	{
		xmlAttrPtr attr = xmlHasProp((xmlNode *) node, (xmlChar *) name);
		if (attr) {
			attr->atype = XML_ATTRIBUTE_CDATA;
			attr->id = NULL;
		}
	}
#endif

	if (!xmlSetProp((xmlNode *) node, (xmlChar *) name, (xmlChar *) value)) {
		return -1;
	}

	return 0;
}'''
s = s.replace(anchor, block, 1)
open(p, 'w').write(s)
print("main/xml.c patched")
PYEOF

# 3) patches/asterisk/*.patch from the repo (git-diff format) — incl.
#    res/res_pjproject.c sin_len (canonical copy of the WebRTC ICE fix).
PATCH_DIR="$REPO/patches/asterisk"
[ -d "$PATCH_DIR" ] || { echo "missing $PATCH_DIR" >&2; exit 1; }
for pf in "$PATCH_DIR"/*.patch; do
	[ -e "$pf" ] || continue
	marker=$(grep -m1 '^+' "$pf" | sed 's/^+//' | head -c 60)
	if [ -n "$marker" ] && grep -qF "$marker" $(grep -m1 '^+++' "$pf" | sed 's/^+++ b\///') 2>/dev/null; then
		echo "$(basename "$pf") already applied"
	else
		patch -p1 --forward < "$pf"
		echo "$(basename "$pf") applied"
	fi
done

# 4) sh bootstrap.sh — 02_sip_ipsec_darwin.patch edits configure.ac (drops the
#    10.4-era AST_POLL_COMPAT poll workaround that conflicts with <poll.h>);
#    regenerate configure + the menuselect configure before running them.
#    engine/Dockerfile runs bootstrap unconditionally after the py patches too.
echo "=== bootstrap (regenerate configure) ==="
sh bootstrap.sh > bootstrap-macos.log 2>&1 || { tail -20 bootstrap-macos.log >&2; echo "BOOTSTRAP FAILED" >&2; exit 1; }
echo "bootstrap OK"

echo "=== configure (CC=cc, C17) ==="
# AST_EXT_LIB_CHECK only adds -L/-I when --with-<lib>=PATH is given (no pkg-config path);
# AMR libs live in our local stage prefix.
./configure --enable-binary-modules ac_cv_prog_cc_c23=no \
	--with-opencore-amrnb="$STAGE" \
	--with-opencore-amrwb="$STAGE" \
	--with-vo-amrwbenc="$STAGE" > configure-macos.log 2>&1 || {
		tail -20 configure-macos.log >&2; echo "CONFIGURE FAILED" >&2; exit 1; }
echo "configure OK"

echo "=== menuselect ==="
make menuselect/menuselect menuselect-tree menuselect.makeopts
./menuselect/menuselect --enable codec_opus --disable BUILD_NATIVE
# Whitelist modules that menuselect may have auto-disabled; harmless if deps still missing.
./menuselect/menuselect --enable res_pjsip_outbound_registration --enable codec_amr
# res_geolocation uses GNU-ld -b binary blob embedding (-Wl,-znoexecstack): unbuildable on macOS,
# not in the engine whitelist (noloaded on Linux too). res_pjsip_geolocation depends on it and is
# likewise not whitelisted. Disable AFTER the enables and VERIFY: menuselect's --check-deps (re-run
# by make) force-enables res_geolocation into MENUSELECT_BUILD_DEPS if anything still depends on it.
./menuselect/menuselect --disable res_geolocation --disable res_pjsip_geolocation
if grep -E "^MENUSELECT_BUILD_DEPS=.*res_geolocation" menuselect.makeopts > /dev/null; then
	echo "FATAL: res_geolocation still in MENUSELECT_BUILD_DEPS"; exit 1
fi

echo "=== make -j$JOBS ==="
set -o pipefail
# NOTE: the grep filter group must end with `|| true` — with set -e + pipefail,
# grep exiting 1 (no error lines matched) inside the group kills the shell.
make -j"$JOBS" 2>&1 | tee build-macos.log | { grep -E "error:|Error [0-9]" | head -30 || true; }

echo "=== make install (DESTDIR staging) ==="
rm -rf "$AST_STAGE"
make install DESTDIR="$AST_STAGE" > install-macos.log 2>&1

echo "=== verify ==="
DYLD_LIBRARY_PATH="$AST_STAGE/usr/local/lib" "$AST" -V
echo "MODULES_BUILT=$(ls "$AST_STAGE/Library/Application Support/Asterisk/Modules"/*.so | wc -l)"
echo "$PINNED_REV" > "$BUILT_REV_STAMP"
echo "build-asterisk: DONE"
