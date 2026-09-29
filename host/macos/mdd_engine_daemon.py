#!/usr/bin/env python3
"""mdd-sim-gateway macOS port: privileged engine daemon (LaunchDaemon, root).

Owns everything the Docker container used to get from `cap_add=NET_ADMIN` and
root: the per-line engine process tree (swu_ike binds UDP/500, creates a utun,
adds routes; Asterisk needs no privileges but runs as root for container
parity). The control plane (user session) drives it over a Unix socket:

  {"action": "start",  "iid": "1"}        -> spawn engine_supervisor for line 1
  {"action": "stop",   "iid": "1"}        -> TERM the line's process group
  {"action": "status", "iid": "1"}        -> {"running": bool, "pid": pgid}
  {"action": "exec",   "iid": "1", "cmd": "pjsip show registrations"}
  {"action": "logs",   "iid": "1"}        -> {"files": [...]} under the line dir

Config comes from the plist environment: MDD_DATA, MDD_REPO, MDD_VENV,
MDD_AST_STAGE, MDD_SOCKET_USER (socket is chowned to this user, mode 0600).
"""
import json
import os
import pwd
import signal
import socket
import subprocess
import sys
import time
MDD_DATA = os.environ.get("MDD_DATA", os.path.expanduser("~/mdd-macos-build/data"))
MDD_REPO = os.environ.get("MDD_REPO", "/Users/linwayne/mdd-sim-gateway")
MDD_VENV = os.environ.get("MDD_VENV", "/Users/linwayne/mdd-macos-build/venv")
MDD_AST_STAGE = os.environ.get("MDD_AST_STAGE", "/Users/linwayne/mdd-macos-build/asterisk-stage")
SOCK_USER = os.environ.get("MDD_SOCKET_USER", "linwayne")

RUNROOT = os.path.join(MDD_DATA, "run")
SOCK_PATH = os.path.join(RUNROOT, "engine.sock")
SUPERVISOR = os.path.join(MDD_REPO, "host/macos/engine_supervisor.py")

# iid -> {"proc": Popen, "pgid": int, "started": float}
_lines = {}


def log(msg):
    print("[engine-daemon] %s" % msg, flush=True)


def instance_dir(iid):
    # iid comes from the (authenticated) control plane over the 0600 socket; still,
    # never let it escape MDD_DATA/instances.
    if not iid or "/" in iid or iid.startswith("."):
        raise ValueError("bad iid")
    return os.path.join(MDD_DATA, "instances", iid)


def supervisor_env(iid, extra_env=None):
    prefix = instance_dir(iid)
    env = dict(os.environ)
    env.update({
        "MDD_PREFIX": prefix,
        "MDD_RUNDIR": os.path.join(prefix, "run"),
        "MDD_INSTANCE": os.path.join(prefix, "instance.json"),
        "MDD_ID": iid,
        "MDD_ENGINE_DIR": os.path.join(MDD_REPO, "engine"),
        "MDD_PY": os.path.join(MDD_VENV, "bin/python"),
        "MDD_AST_BIN": os.path.join(MDD_AST_STAGE, "usr/local/sbin/asterisk"),
        "MDD_AST_LIB": os.path.join(MDD_AST_STAGE, "usr/local/lib"),
        # macOS stages modules under Library/Application Support (Asterisk's
        # default astmoddir on darwin), not <prefix>/lib/asterisk/modules.
        "MDD_ASTMODDIR": os.path.join(MDD_AST_STAGE, "Library/Application Support/Asterisk/Modules"),
        # Staged read-only data (documentation/xmldoc, sounds, moh, agi-bin, ...)
        # that the supervisor links into the per-line var/lib/asterisk.
        "MDD_AST_DATA": os.path.join(MDD_AST_STAGE, "Library/Application Support/Asterisk"),
    })
    # Per-line extras from the control plane's start request (e.g. SWU_EGRESS_PROXY
    # for SOCKS country egress). String keys/values only; the control plane
    # (engine_native.start) already filters, but this daemon is root — re-check.
    for key, value in (extra_env or {}).items():
        if isinstance(key, str) and isinstance(value, str) and key:
            env[key] = value
    return env


def line_pgid(iid):
    rec = _lines.get(iid)
    if not rec:
        return None
    # poll() reaps the child if it exited; without this the supervisor stays a
    # zombie and killpg(pgid, 0) keeps succeeding, so the line looks running forever.
    proc = rec.get("proc")
    if proc is not None and proc.poll() is not None:
        _lines.pop(iid, None)
        return None
    try:
        os.killpg(rec["pgid"], 0)
    except ProcessLookupError:
        _lines.pop(iid, None)
        return None
    except PermissionError:
        pass
    return rec["pgid"]


def _spawn(iid, extra_env=None):
    prefix = instance_dir(iid)
    os.makedirs(os.path.join(prefix, "run"), exist_ok=True)
    os.makedirs(os.path.join(prefix, "logs"), exist_ok=True)
    console = open(os.path.join(prefix, "logs", "engine-console.log"), "ab")
    proc = subprocess.Popen(
        [os.path.join(MDD_VENV, "bin/python"), "-u", SUPERVISOR],
        env=supervisor_env(iid, extra_env),
        stdout=console, stderr=subprocess.STDOUT,
        start_new_session=True)   # own pgid => stop can kill the whole tree
    rec = {"proc": proc, "pgid": proc.pid, "started": time.time(),
           "stopping": False, "restarts": 0}
    _lines[iid] = rec
    log("started line %s (pgid %d)" % (iid, proc.pid))
    return rec


def _watch(iid, proc):
    """Container-restart-policy parity: if the supervisor tree exits on its own
    (Asterisk crash, render failure, ...), bring the line back with backoff.
    An explicit stop sets rec["stopping"] and ends the watch."""
    rc = proc.wait()
    rec = _lines.get(iid)
    if not rec or rec.get("proc") is not proc:
        return                      # line was stopped/restarted meanwhile
    rec["restarts"] = rec.get("restarts", 0) + 1
    if rec.get("stopping"):
        _lines.pop(iid, None)
        return
    delay = min(30, 5 * rec["restarts"])
    log("line %s exited (rc=%s); restarting in %ds (restart #%d)"
        % (iid, rc, delay, rec["restarts"]))
    time.sleep(delay)
    cur = _lines.get(iid)
    if not cur or cur.get("proc") is not proc or cur.get("stopping"):
        return
    try:
        new = _spawn(iid)
        new["restarts"] = rec["restarts"]
    except Exception as e:
        log("line %s respawn failed: %r" % (iid, e))
        _lines.pop(iid, None)
        return
    _watch_thread(iid, new["proc"])


def _watch_thread(iid, proc):
    import threading
    threading.Thread(target=_watch, args=(iid, proc), daemon=True,
                     name="mdd-watch-%s" % iid).start()


def _kill_stale_supervisor(iid):
    """After a daemon restart the in-memory _lines record is gone, but the old
    line's process group may still be alive (orphaned). The supervisor writes
    its pgid to run/supervisor.pid; reclaim the group here so start() never
    stacks a second engine tree on top of a forgotten one."""
    pidfile = os.path.join(instance_dir(iid), "run", "supervisor.pid")
    try:
        with open(pidfile) as f:
            pgid = int(f.read().strip())
    except (OSError, ValueError):
        return
    if pgid <= 1:
        return
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return                      # group already gone
    log("line %s: killing stale process group %d from a previous daemon" % (iid, pgid))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            break
        if sig is signal.SIGTERM:
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    os.killpg(pgid, 0)
                except (ProcessLookupError, PermissionError):
                    return
                time.sleep(0.2)


def do_start(iid, extra_env=None):
    if line_pgid(iid):
        return {"ok": True, "already_running": True}
    _kill_stale_supervisor(iid)
    rec = _spawn(iid, extra_env)
    _watch_thread(iid, rec["proc"])
    return {"ok": True, "pid": rec["pgid"]}


def _group_alive(pgid):
    """True while the line's process group has live members. A zombie still
    answers killpg(pgid, 0), so callers must poll() (reap) our direct child
    first; PermissionError means only unreapable zombies remain — effectively
    gone."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def do_stop(iid):
    pgid = line_pgid(iid)
    rec = _lines.get(iid)
    if rec:
        rec["stopping"] = True
    if not pgid:
        _lines.pop(iid, None)
        return {"ok": True, "running": False}
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # Group already gone (or only a zombie left, which line_pgid's poll() will
        # reap on the next call) — nothing to stop.
        _lines.pop(iid, None)
        return {"ok": True, "running": False}
    deadline = time.time() + 10
    while time.time() < deadline:
        if rec.get("proc") is not None:
            rec["proc"].poll()      # reap our child; zombies answer killpg(0)
        if not _group_alive(pgid):
            break
        time.sleep(0.2)
    else:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    _lines.pop(iid, None)
    log("stopped line %s" % iid)
    return {"ok": True, "running": False}


def do_status(iid):
    pgid = line_pgid(iid)
    rec = _lines.get(iid) or {}
    return {"ok": True, "running": pgid is not None, "pid": pgid or 0,
            "started_at": rec.get("started", 0)}


def do_exec(iid, cmd):
    prefix = instance_dir(iid)
    env = supervisor_env(iid)
    env["DYLD_LIBRARY_PATH"] = env["MDD_AST_LIB"]
    try:
        out = subprocess.check_output(
            [env["MDD_AST_BIN"], "-C",
             os.path.join(prefix, "etc/asterisk/asterisk.conf"), "-rx", cmd],
            stderr=subprocess.STDOUT, env=env, timeout=15)
        return {"ok": True, "output": out.decode(errors="replace")}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "output": e.output.decode(errors="replace")}
    except Exception as e:
        return {"ok": False, "output": repr(e)}


def do_logs(iid):
    prefix = instance_dir(iid)
    files = []
    for root, _dirs, names in os.walk(os.path.join(prefix, "logs")):
        for name in names:
            files.append(os.path.join(root, name))
    files.sort()
    return {"ok": True, "files": files}


def do_pcap(seconds, port_filter):
    """One-shot packet capture on the IKE ports (needs root; that is why it lives
    here and not in the control plane). Returns tcpdump's decoded lines.
    tcpdump runs until the time budget expires (IKE retries are slow — a packet
    count target may never be reached) and whatever was captured up to the kill
    is returned, never discarded."""
    seconds = max(1, min(60, int(seconds or 20)))
    bpf = str(port_filter) if port_filter else "udp port 500 or udp port 4500"
    cmd = ["tcpdump", "-i", "any", "-n", "-s0", "-l", bpf]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = proc.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    except Exception as e:
        proc.kill()
        return {"ok": False, "output": repr(e)}
    return {"ok": proc.returncode == 0 or bool(out),
            "output": out.decode(errors="replace")}


def handle(req):
    action = req.get("action")
    iid = str(req.get("iid") or "")
    if action == "start":
        return do_start(iid, req.get("env") or {})
    if action == "stop":
        return do_stop(iid)
    if action == "status":
        return do_status(iid)
    if action == "exec":
        return do_exec(iid, req.get("cmd") or "")
    if action == "logs":
        return do_logs(iid)
    if action == "pcap":
        return do_pcap(req.get("seconds"), req.get("filter"))
    return {"ok": False, "error": "unknown action %r" % action}


def serve_conn(conn):
    """Handle one client connection. Runs in its own thread — a long action
    (pcap's tcpdump runs up to 60s) must not block start/stop/status."""
    try:
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        req = json.loads(data.decode() or "{}")
        resp = handle(req)
    except Exception as e:
        resp = {"ok": False, "error": repr(e)}
    try:
        conn.sendall((json.dumps(resp) + "\n").encode())
    except OSError:
        # Client went away (e.g. timed out during a long pcap) — never let a
        # dead peer take the daemon down with a BrokenPipeError.
        pass
    finally:
        conn.close()


def main():
    os.makedirs(RUNROOT, exist_ok=True)
    # The control plane (user session) reads this daemon's socket and the engine's
    # state files directly; launchd gives daemons a restrictive umask, so normalize.
    os.umask(0o022)
    try:
        os.unlink(SOCK_PATH)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    uid = pwd.getpwnam(SOCK_USER).pw_uid
    gid = pwd.getpwnam(SOCK_USER).pw_gid
    os.chown(SOCK_PATH, uid, gid)
    os.chmod(SOCK_PATH, 0o600)
    srv.listen(8)
    log("listening on %s (user %s)" % (SOCK_PATH, SOCK_USER))
    import threading
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve_conn, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
