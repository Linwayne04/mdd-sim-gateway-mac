#!/usr/bin/env python3
"""mdd-sim-gateway macOS port: native per-line engine supervisor.

Python port of engine/entrypoint.sh for the process-based (non-Docker) engine.
One instance per SIM line, spawned BY mdd_engine_daemon (root, via launchd), so
the privileged operations the container did with CAP_NET_ADMIN (utun, routes,
UDP/500 bind) keep working. The control-plane contract is unchanged:
$MDD_PREFIX/{run,logs} files (engine.env, pin/swu status, pcscf, charon.log,
swu.ctl FIFO), notify.py HTTP callbacks, AMI, asterisk -rx.

Environment (set by the daemon):
  MDD_PREFIX       per-line root (=$MDD_DATA/instances/<iid>); /etc,/logs,/run redirect here
  MDD_RUNDIR       $MDD_PREFIX/run
  MDD_INSTANCE     $MDD_PREFIX/instance.json
  MDD_ID           line/instance id
  MDD_ENGINE_DIR   repo engine/ dir (render.py, pin_keeper.py, swu_ike.py, ...)
  MDD_PY           venv python for engine scripts
  MDD_AST_BIN      staged asterisk binary
  MDD_AST_LIB      DYLD_LIBRARY_PATH for the staged asterisk
"""
import json
import os
import shlex
import signal
import subprocess
import sys
import time

PREFIX = os.environ["MDD_PREFIX"]
RUNDIR = os.environ.get("MDD_RUNDIR", PREFIX + "/run")
LOGS = PREFIX + "/logs"
AST_LOGDIR = os.environ.get("MDD_AST_LOGDIR", LOGS + "/asterisk")
ENGINE_DIR = os.environ["MDD_ENGINE_DIR"]
PY = os.environ.get("MDD_PY", sys.executable)
AST_BIN = os.environ["MDD_AST_BIN"]
AST_LIB = os.environ.get("MDD_AST_LIB", "")
AST_CONF = PREFIX + "/etc/asterisk/asterisk.conf"
INSTANCE = os.environ.get("MDD_INSTANCE", PREFIX + "/instance.json")

SWU_STABLE_SECONDS = int(os.environ.get("SWU_STABLE_SECONDS", "120"))
AST_LOG_MAX_BYTES = int(os.environ.get("MDD_AST_LOG_MAX_BYTES", 8388608))
AST_LOG_KEEP = int(os.environ.get("MDD_AST_LOG_KEEP", 3))

_children = []


def log(msg):
    print("[supervisor] %s" % msg, flush=True)


def supervisor_record(event, **kv):
    """One machine-readable line per lifecycle transition (manager reads this to
    tell a crash-restart apart from a manager-initiated stop). Bounded file."""
    rec = {"ts": int(time.time()), "event": event}
    rec.update(kv)
    path = os.path.join(AST_LOGDIR, "supervisor.jsonl")
    try:
        lines = []
        if os.path.exists(path):
            with open(path) as f:
                lines = f.read().splitlines()[-499:]
        lines.append(json.dumps(rec, sort_keys=True))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass


def rotate_asterisk_logs():
    """Bound the persistent Asterisk logs (macOS stat -f %z, not GNU stat -c %s)."""
    for name in ("full", "messages"):
        path = os.path.join(AST_LOGDIR, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size < AST_LOG_MAX_BYTES:
            continue
        rotated = "%s.%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(path, rotated)
        except OSError:
            continue
        # Ask the running Asterisk to reopen its files; harmless if not up yet.
        asterisk_cli("logger reload")
    try:
        names = sorted((os.path.join(AST_LOGDIR, n) for n in os.listdir(AST_LOGDIR)
                        if n.startswith(("full.", "messages."))),
                       key=lambda p: os.path.getmtime(p), reverse=True)
        for old in names[AST_LOG_KEEP * 2:]:
            os.unlink(old)
    except OSError:
        pass


def asterisk_cli(command):
    env = dict(os.environ)
    if AST_LIB:
        env["DYLD_LIBRARY_PATH"] = AST_LIB
    try:
        subprocess.call([AST_BIN, "-C", AST_CONF, "-rx", command],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except Exception:
        pass


def load_engine_env():
    """Parse $MDD_RUNDIR/engine.env (shlex-quoted KEY=value lines) into os.environ,
    mirroring entrypoint.sh's `set -a; . engine.env; set +a`."""
    path = os.path.join(RUNDIR, "engine.env")
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line or line.startswith("#"):
                continue
            key, _, raw = line.partition("=")
            try:
                value = shlex.split(raw)[0] if raw.strip() else ""
            except ValueError:
                value = raw.strip("'\"")
            os.environ[key] = value


def spawn(argv, **kw):
    proc = subprocess.Popen(argv, **kw)
    _children.append(proc)
    return proc


def read_status(name, key="state"):
    try:
        with open(os.path.join(RUNDIR, name)) as f:
            return json.load(f)[key]
    except Exception:
        return ""


def wait_pin():
    for _ in range(30):
        st = read_status("pin_status.json")
        if st in ("VERIFIED", "PIN_DISABLED"):
            log("PIN state: %s" % st)
            return True
        if st in ("WRONG_PIN", "PIN_BLOCKED"):
            log("PIN problem: %s - continuing (manager will surface)" % st)
            return False
        time.sleep(1)
    log("PIN keeper did not reach VERIFIED in time - continuing anyway")
    return False


def swu_loop():
    """Restart swu_ike on exit with 4s -> 60s backoff, resetting after a stable run
    (same policy as entrypoint.sh)."""
    backoff = 4
    while True:
        log("swu_ike starting")
        started = time.time()
        swu = subprocess.Popen(
            [PY, "-u", os.path.join(ENGINE_DIR, "swu_ike.py"),
             "-m", os.environ.get("USIM_READER_INDEX", "0"),
             "-s", os.environ.get("SWU_SOURCE", ""),
             "-d", os.environ.get("SWU_EPDG", ""),
             "-a", os.environ.get("SWU_APN", "ims"),
             "-I", os.environ.get("USIM_IMSI", ""),
             "-M", os.environ.get("SWU_MCC", ""),
             "-N", os.environ.get("SWU_MNC", ""),
             "-E", os.environ.get("SWU_IMEI", ""),
             "-V", os.environ.get("SWU_IMEISV", "")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _children.append(swu)
        capture = subprocess.Popen(
            [PY, "-u", os.path.join(ENGINE_DIR, "log_capture.py"),
             "--current", os.path.join(RUNDIR, "charon.log"),
             "--archive-dir", os.path.join(LOGS, "ike")],
            stdin=swu.stdout)
        _children.append(capture)
        swu.stdout.close()  # let swu get SIGPIPE if capture dies
        rc = swu.wait()
        capture.wait()
        ran = int(time.time() - started)
        if ran >= SWU_STABLE_SECONDS:
            if backoff != 4:
                log("swu_ike had been up %ds; resetting reconnect delay %ds -> 4s"
                    % (ran, backoff))
                supervisor_record("swu_backoff_reset", ran_seconds=ran,
                                  previous_backoff=backoff)
            backoff = 4
        log("swu_ike exited (rc=%s); reconnecting in %ds" % (rc, backoff))
        supervisor_record("swu_ike_exited", rc=rc, ran_seconds=ran,
                          backoff_seconds=backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def main():
    def _shutdown(signum, _frame):
        """Works in every phase (before/after Asterisk): bash PID 1 in the
        container took the whole tree down on any exit; here an early SIGTERM
        must not orphan pin_keeper/swu_ike (they would hold the SIM card)."""
        log("supervisor got signal %d; terminating children" % signum)
        for child in _children:
            if child.poll() is None:
                try:
                    child.terminate()
                except Exception:
                    pass
        deadline = time.time() + 5
        for child in _children:
            try:
                child.wait(timeout=max(0, deadline - time.time()))
            except Exception:
                try:
                    child.kill()
                except Exception:
                    pass
        os._exit(128 + signum)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # The engine tree runs as root while the control plane (which reads run/*.json,
    # charon.log, etc. directly) runs as the logged-in user. The per-line instance dir
    # is already 0700 user-owned, so create files 0644/0755 — group/other bits are
    # never reachable from outside, and the control plane can read state files.
    os.umask(0o022)
    os.makedirs(RUNDIR, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(AST_LOGDIR, exist_ok=True)
    # Publish our process group so the daemon can reclaim the whole tree even
    # after the daemon itself restarted and lost its in-memory record.
    with open(os.path.join(RUNDIR, "supervisor.pid"), "w") as f:
        f.write(str(os.getpgrp()))
    os.makedirs(os.path.join(PREFIX, "etc/asterisk"), exist_ok=True)
    # Asterisk's redirected state dirs (asterisk.conf [directories]) must exist before
    # it starts or ASTdb initialization fails and the process exits.
    for sub in ("lib/asterisk", "lib/asterisk/keys", "spool/asterisk",
                "run/asterisk", "var/lib/asterisk"):
        os.makedirs(os.path.join(PREFIX, sub), exist_ok=True)
    # The per-line astvarlibdir is empty, but Asterisk needs the staged read-only
    # data (documentation/ for xmldoc — without it Stasis init fails — plus sounds,
    # moh, agi-bin, rest-api, static-http). Link each staged entry in; per-line
    # content (mdd-sounds) is already rendered into the same dir and wins.
    ast_data = os.environ.get("MDD_AST_DATA", "")
    ast_varlib = os.path.join(PREFIX, "var/lib/asterisk")
    if ast_data and os.path.isdir(ast_data):
        for entry in sorted(os.listdir(ast_data)):
            dst = os.path.join(ast_varlib, entry)
            if not os.path.lexists(dst):
                os.symlink(os.path.join(ast_data, entry), dst)
    os.environ.setdefault("MDD_PREFIX", PREFIX)
    os.environ.setdefault("MDD_RUNDIR", RUNDIR)
    os.environ.setdefault("MDD_INSTANCE", INSTANCE)
    os.environ.setdefault("MDD_TPL", os.path.join(ENGINE_DIR, "templates"))
    os.environ.setdefault("MDD_ENV", os.path.join(RUNDIR, "engine.env"))
    os.environ.setdefault("MDD_NOTIFY_BIN", os.path.join(ENGINE_DIR, "notify.py"))
    os.environ.setdefault("SWU_RENDER", os.path.join(ENGINE_DIR, "render.py"))
    os.environ.setdefault("SWU_NOTIFY", os.path.join(ENGINE_DIR, "notify.py"))
    os.environ.setdefault("MDD_ASTERISK_CONF", AST_CONF)
    # The staged binary and its modules link against the staged lib dir; without
    # this, engine-side `asterisk -rx` (swu_ike's P-CSCF apply) fails to exec.
    if AST_LIB:
        os.environ.setdefault("DYLD_LIBRARY_PATH", AST_LIB)
    # Derive the staged data dirs from the binary location so an older daemon
    # (which may not export MDD_ASTMODDIR/MDD_AST_DATA yet) still renders a
    # working asterisk.conf: <stage>/usr/local/sbin/asterisk -> <stage>/Library/...
    _stage = os.path.dirname(os.path.dirname(os.path.dirname(AST_BIN)))
    _ast_support = os.path.join(_stage, "Library/Application Support/Asterisk")
    os.environ.setdefault("MDD_ASTMODDIR", os.path.join(_ast_support, "Modules"))
    os.environ.setdefault("MDD_AST_DATA", _ast_support)

    rotate_asterisk_logs()

    # --- 1. Render configs from instance.json --------------------------------------
    log("rendering configs...")
    env = dict(os.environ)
    if AST_LIB:
        env["DYLD_LIBRARY_PATH"] = AST_LIB
    rc = subprocess.call([PY, os.path.join(ENGINE_DIR, "render.py")], env=env)
    if rc != 0:
        log("render failed")
        sys.exit(1)
    load_engine_env()

    # --- 1b. Rekey/liveness patience (macOS port tuning) ---------------------------
    # Observed on the Play/T-Mobile PL ePDG (2026-09-29, root tcpdump at rekey): the
    # ePDG can go silent on IKE for 30-90s around the 30-min CHILD_SA rekey — it
    # eventually answers, but the upstream defaults (10s timeout x3 retransmits ->
    # give up at ~30s; liveness 4 probes -> ~86s) tore the tunnel down at nearly
    # every first rekey. The new IKE_SA_INIT right after re-establish always got
    # sub-second answers, so the path is fine — the ePDG is just slow. Verbatim
    # retransmissions (same message id) are harmless; wait it out. engine.env
    # values still win via setdefault.
    os.environ.setdefault("SWU_REKEY_TIMEOUT", "20")
    os.environ.setdefault("SWU_REKEY_RETRANSMITS", "8")
    os.environ.setdefault("SWU_LIVENESS_RETRIES", "8")

    # --- 2. PIN keeper; wait for the SIM to be usable ------------------------------
    log("starting pin_keeper (reader=%s)..." % os.environ.get("USIM_READER", ""))
    keeper_env = dict(os.environ)
    keeper_env["USIM_READER"] = os.environ.get("PIN_USIM_READER") or os.environ.get("USIM_READER", "")
    spawn([PY, "-u", os.path.join(ENGINE_DIR, "pin_keeper.py")], env=keeper_env)
    wait_pin()

    # --- 3. SWu tunnel, supervised --------------------------------------------------
    log("starting SWu IKEv2 tunnel (epdg=%s apn=%s reader=%s port=%s)..." % (
        os.environ.get("SWU_EPDG", ""), os.environ.get("SWU_APN", ""),
        os.environ.get("USIM_READER_INDEX", "0"), os.environ.get("USIM_READER_PORT", "none")))
    for stale in ("swu.ctl", "swu_status.json"):
        try:
            os.unlink(os.path.join(RUNDIR, stale))
        except OSError:
            pass
    import threading
    threading.Thread(target=swu_loop, daemon=True).start()

    # --- 4. Wait for tunnel + P-CSCF, (re)render pjsip with it ----------------------
    log("waiting for SWu tunnel to establish...")
    for _ in range(90):
        if read_status("swu_status.json") == "CONNECTED":
            log("SWu tunnel CONNECTED")
            break
        time.sleep(1)
    log("waiting for P-CSCF discovery...")
    pcscf = ""
    for _ in range(30):
        try:
            with open(os.path.join(RUNDIR, "pcscf")) as f:
                pcscf = f.read().strip()
        except OSError:
            pcscf = ""
        if pcscf:
            break
        time.sleep(1)
    if pcscf:
        log("discovered P-CSCF: %s" % pcscf)
        subprocess.call([PY, os.path.join(ENGINE_DIR, "render.py")], env=env)
        # Seed the applied-marker so swu_ike's in-process P-CSCF watcher only re-renders
        # + reloads Asterisk on a LATER change, not redundantly right after this render.
        with open(os.path.join(RUNDIR, "pcscf.applied"), "w") as f:
            f.write(pcscf)
    else:
        log("no P-CSCF discovered yet - continuing (manager will surface tunnel state)")

    # --- 5. USIM<->AMI bridge + Asterisk ---------------------------------------------
    log("starting ami_usim bridge...")
    spawn([PY, "-u", os.path.join(ENGINE_DIR, "ami_usim.py"),
           os.path.join(PREFIX, "usr/local/etc/ami_usim.ini")])

    ast_args = [AST_BIN, "-C", AST_CONF, "-f"]
    # Core dumps stay opt-in on the native port: a crashed Asterisk writes an
    # ~9 GB core on this host, and the watchdog respawn loop turned that into a
    # disk-filling core-per-crash loop.
    if os.environ.get("MDD_ASTERISK_CORE_DUMP", "0") == "1":
        ast_args.append("-g")
    log("starting Asterisk... (%s)" % " ".join(ast_args))
    ast = spawn(ast_args, env=env)

    def rotator():
        while True:
            time.sleep(3600)
            rotate_asterisk_logs()
    threading.Thread(target=rotator, daemon=True).start()

    # SIGHUP reloads Asterisk config; TERM/INT keep the _shutdown handler
    # installed at the top of main() so children are never orphaned.
    def forward_hup(_signum, _frame):
        try:
            ast.send_signal(signal.SIGHUP)
        except Exception:
            pass
    signal.signal(signal.SIGHUP, forward_hup)

    rc = ast.wait()
    if rc < 0:
        log("asterisk terminated by signal %d" % -rc)
        supervisor_record("asterisk_exited", rc=rc, signal=-rc, disposition="signal")
    else:
        log("asterisk exited normally with status %s" % rc)
        supervisor_record("asterisk_exited", rc=rc, disposition="exit")
    # Bash PID 1 in the container took the whole tree down when it exited; here the
    # supervisor must do it explicitly or pin_keeper/ami_usim/swu_ike survive as
    # orphans (duplicate pin_keepers then fight over the card on the next start).
    for child in _children:
        if child.poll() is None:
            try:
                child.terminate()
            except Exception:
                pass
    deadline = time.time() + 5
    for child in _children:
        remaining = max(0, deadline - time.time())
        try:
            child.wait(timeout=remaining)
        except Exception:
            try:
                child.kill()
            except Exception:
                pass
    try:
        subprocess.call([PY, os.path.join(ENGINE_DIR, "notify.py"), "engine_stopped", str(rc)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    sys.exit(rc if rc >= 0 else 128 - rc)


if __name__ == "__main__":
    main()
