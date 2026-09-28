"""Native macOS engine backend (process-based, no Docker).

mdd-sim-gateway macOS port — see docs/macos-port/PLAN.md §5. On darwin the
engine is a per-line process tree spawned by the root LaunchDaemon
(host/macos/mdd_engine_daemon.py) instead of a Docker container. This module is
the control-plane half: it speaks the daemon's Unix-socket JSON protocol and
shapes the results like the Docker inspect/exec responses engine.py callers
expect, so main.py's flow works unchanged.

The file contract is identical to the container engine: everything under
$MDD_DATA/instances/<iid>/{run,logs} (engine.env, status files, pcscf,
charon.log, supervisor.jsonl), notify.py HTTP callbacks, AMI, asterisk -rx.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

from .. import config as cfg

# The daemon is only installed on the macOS port; keep an env override so the
# container path can be forced back on for comparison.
def native_mode() -> bool:
    forced = os.environ.get("MDD_ENGINE_BACKEND", "")
    if forced:
        return forced == "native"
    return sys.platform == "darwin"


def _socket_path() -> str:
    return os.path.join(cfg.DATA_DIR, "run", "engine.sock")


class DaemonUnavailable(RuntimeError):
    """The root engine daemon is not installed/running (see
    host/macos/install-engine-daemon.sh)."""


def _request(payload: dict, timeout: float = 30) -> dict:
    path = _socket_path()
    if not os.path.exists(path):
        raise DaemonUnavailable(
            f"engine daemon socket not found at {path}; "
            "run: sudo host/macos/install-engine-daemon.sh")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall((json.dumps(payload) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.decode() or "{}")
    except DaemonUnavailable:
        raise
    except Exception as exc:
        raise DaemonUnavailable(f"engine daemon request failed: {exc}") from exc


# --- engine.py-shaped API ------------------------------------------------------

def start(inst: dict, settings: dict, reason: str = "rebuild") -> str:
    """Write instance.json, then ask the daemon to spawn the line's supervisor.

    Returns a generation marker shaped like a container id; stop() accepts it the
    same way expected_container_id was used (best-effort, single line)."""
    iid = str(inst["id"])
    base = os.path.join(cfg.DATA_DIR, "instances", iid)
    run_dir = os.path.join(base, "run")
    # Create the per-line tree as the control plane's user BEFORE the daemon sees it:
    # the daemon runs as root, and a root-created base dir makes write_instance_json's
    # chmod fail with EPERM, which surfaces as a 500 on the WebUI capability switch.
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(os.path.join(base, "logs"), exist_ok=True)
    cfg.write_instance_json(inst, settings)
    # Docker semantics are "start = recreate": callers such as the IMS line-identity
    # restart invoke start() on a RUNNING line and expect configs re-rendered from the
    # just-written instance.json. The daemon's start is a no-op while the line runs,
    # so without this stop the wipe below would orphan a live engine's status files
    # (pin_keeper/swu_ike only rewrite them on state changes) and the line would read
    # as registering-forever despite a healthy registration.
    try:
        if _request({"action": "status", "iid": iid}).get("running"):
            _request({"action": "stop", "iid": iid})
    except DaemonUnavailable:
        raise
    except Exception:  # noqa
        pass
    # Same staleness rule as engine._clear_runtime_state: an old CONNECTED marker
    # must not make the new process look online before it has done IKE.
    for name in ("swu_status.json", "pcscf", "pcscf.applied", "pin_status.json",
                 "usim_status.json", "engine.env", "swu.ctl", "media_routes"):
        try:
            os.unlink(os.path.join(run_dir, name))
        except FileNotFoundError:
            pass
    resp = _request({"action": "start", "iid": iid})
    if not resp.get("ok"):
        raise DaemonUnavailable(f"engine daemon refused start: {resp.get('error')}")
    return f"native-{iid}-{int(time.time())}"


def stop(iid: str, expected_container_id: str | None = None) -> bool:
    try:
        resp = _request({"action": "stop", "iid": str(iid)})
    except DaemonUnavailable:
        # Nothing running to stop — mirror the Docker NotFound no-op.
        return False
    return bool(resp.get("ok"))


def container_runtime(iid: str) -> dict:
    """Shape the daemon status like a Docker inspect result."""
    try:
        resp = _request({"action": "status", "iid": str(iid)})
    except DaemonUnavailable:
        return {"running": False, "ip": None, "container_id": None,
                "restart_count": 0, "started_at": ""}
    running = bool(resp.get("running"))
    started = resp.get("started_at") or 0
    return {"running": running,
            # The engine runs on this host, so "the container IP" is loopback —
            # the softphone WebSocket relay dials ws://<ip>:8088/ws against it.
            "ip": "127.0.0.1" if running else None,
            "container_id": f"native-{iid}" if running else None,
            "restart_count": 0,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)) if started else ""}


def exec_cli(iid: str, command: str) -> str:
    resp = _request({"action": "exec", "iid": str(iid), "cmd": command})
    return resp.get("output") or ""


def logs(iid: str, tail: int = 200) -> str:
    """The supervisor's console (its stdout/stderr), replacing `docker logs`."""
    path = os.path.join(cfg.DATA_DIR, "instances", str(iid), "logs", "engine-console.log")
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return ""
    return "".join(lines[-tail:])


def pcap(seconds: int = 20, filter: str | None = None) -> str:
    """One-shot IKE-port capture via the root daemon (tcpdump needs root)."""
    resp = _request({"action": "pcap", "seconds": seconds, "filter": filter})
    return resp.get("output") or resp.get("error") or ""
