#!/usr/bin/env python3
"""macOS port: Darwin specialisation of the country-egress orchestrator.

Selected by ``host/mdd_orchestrator.py`` when ``sys.platform == "darwin"``.
Only the country-egress half of the orchestrator is meaningful on macOS in
this phase: USB modem discovery, ModemManager, NetworkManager, VPCD bridges
and systemd are all Linux machinery, so those hooks are overridden with
inert versions and the reconcile loop idles cleanly until the control plane
publishes egress desired state.

Differences from the Linux base:

* Route pinning uses ``route(8)`` (``route -n add/delete -host <ip>
  -interface <utunX>``) instead of iproute2 ``proto 186`` tags — macOS has
  no route-protocol marks, so the managed set is tracked in a JSON state
  file (``$MDD_DATA/orchestrator/managed-routes.json``, overridable with
  ``MDD_MANAGED_ROUTES_FILE``) instead of being parsed out of ``ip route``.
* ``usb_modems()`` returns [] — modem/VPCD support is a later phase.
* systemd/ModemManager/NetworkManager helpers become no-ops, so an idle
  orchestrator (no egress configured) sleeps in its reconcile loop instead
  of crash-looping on missing Linux binaries.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.mdd_orchestrator import Orchestrator, atomic_json


class DarwinOrchestrator(Orchestrator):
    """Linux-free route management + inert hardware/systemd hooks."""

    # ---------------------------------------------------------------- routes
    def managed_routes_path(self) -> Path:
        return Path(os.environ.get(
            "MDD_MANAGED_ROUTES_FILE", str(self.root / "managed-routes.json")))

    def current_managed_routes(self) -> set[tuple[str, str]]:
        try:
            document = json.loads(self.managed_routes_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return set()
        routes = document.get("routes")
        if not isinstance(routes, list):
            return set()
        found = set()
        for entry in routes:
            if isinstance(entry, dict) and entry.get("ip") and entry.get("iface"):
                found.add((str(entry["ip"]), str(entry["iface"])))
        return found

    def apply_routes(self, wanted: set[tuple[str, str]]):
        if self.dry_run:
            return
        current = self.current_managed_routes()
        # Removed (or moved to another interface): delete first. A missing
        # route is fine — the carrier may have retracted the address while
        # the state file still remembered it.
        for ip, iface in sorted(current - wanted):
            result = subprocess.run(["route", "-n", "delete", "-host", ip],
                                    capture_output=True, text=True)
            if result.returncode:
                self.log(f"route delete -host {ip}: "
                         f"{(result.stderr or result.stdout).strip()}")
        # Added (or re-pointed): macOS `route add` fails on an existing
        # host route, so clear first and ignore that outcome.
        for ip, iface in sorted(wanted - current):
            subprocess.run(["route", "-n", "delete", "-host", ip],
                           capture_output=True, text=True)
            result = subprocess.run(["route", "-n", "add", "-host", ip,
                                     "-interface", iface],
                                    capture_output=True, text=True)
            if result.returncode:
                self.log(f"route add -host {ip} -interface {iface}: "
                         f"{(result.stderr or result.stdout).strip()}")
        atomic_json(self.managed_routes_path(),
                    {"version": 1, "routes": [
                        {"ip": ip, "iface": iface} for ip, iface in sorted(wanted)]})

    # ------------------------------------------------------- inert Linux hooks
    def usb_modems(self, hardware: dict) -> list[dict]:
        """Phase 4: USB modem/VPCD support is not ported yet."""
        return []

    @staticmethod
    def service_active(name: str) -> bool:
        """No systemd on darwin — nothing unit-based is ever "active"."""
        return False

    def retire_obsolete_services(self):
        """No systemd units to retire on darwin."""
        self.obsolete_services_retired = True

    def reconcile_timezone(self):
        """timedatectl does not exist on darwin; the WebUI timezone is
        informational until a macOS mechanism is chosen."""
        return

    def police_orphaned_modem_profiles(self) -> None:
        """No NetworkManager GSM profiles on darwin."""
        self.modem_profiles_swept = True

    def virtualization(self) -> str:
        """systemd-detect-virt does not exist on darwin."""
        if self._virtualization is None:
            self._virtualization = "none"
        return self._virtualization

    def process_update_request(self):
        """Self-updates run through the launchd updater job, not systemd-run.

        The request file is NOT consumed here: ``mdd_update.py``
        (``local.mdd.update``, StartInterval 300s) owns the request lifecycle —
        it validates, applies, writes update-status.json and deletes the request
        on completion. We only nudge launchd so the update starts promptly
        instead of waiting out the poll interval. A fresh ``running`` status
        means the updater is already mid-flight; kicking a running job would
        restart it, so skip. Without this nudge a stale request left by an
        older control plane would still be applied by the next poll.
        """
        request_path = self.root / "update-request.json"
        if not read_json_quiet(request_path):
            return
        try:
            status = json.loads((self.root / "update-status.json").read_text(encoding="utf-8"))
            fresh_running = (status.get("state") == "running"
                             and int(time.time()) - int(status.get("updated_at") or 0) < 1800)
        except (OSError, ValueError, TypeError):
            fresh_running = False
        if fresh_running:
            return
        self.log("update request pending — kickstarting local.mdd.update")
        subprocess.run(["launchctl", "kickstart", "system/local.mdd.update"],
                       capture_output=True, timeout=30)

    def process_service_restart_request(self):
        """Service restarts on macOS are launchd jobs, not systemd units."""
        request_path = self.root / "service-restart-request.json"
        request = read_json_quiet(request_path)
        if not request:
            return
        try:
            request_path.unlink()
        except OSError:
            pass
        status_path = self.root / "service-restart-status.json"
        scope = str(request.get("scope") or "")

        def publish(state: str, **fields):
            atomic_json(status_path, {"state": state, "scope": scope,
                                      "updated_at": int(time.time()), **fields})

        if scope not in {"control", "services", "host"}:
            publish("failed", error_code="restart.error.invalid_scope")
            return
        if self.dry_run:
            publish("failed", error_code="restart.error.dry_run")
            return
        publish("running")
        self.log(f"restarting on request: scope={scope}")
        if scope == "host":
            result = subprocess.run(["/sbin/shutdown", "-r", "+1",
                                     "mdd-sim-gateway restart request"],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                publish("failed", error_code="restart.error.failed",
                        error=(result.stderr or result.stdout or "").strip()[:400])
                return
            publish("done")
            return
        # The control agent runs in the login user's gui domain; its uid is the
        # owner of the data dir (mirrors mdd_update.py's restart phase).
        try:
            uid = os.stat(self.data).st_uid
        except OSError:
            uid = None
        jobs = []
        if scope == "control":
            if uid is not None:
                jobs.append(f"gui/{uid}/local.mdd.control")
        else:  # services: engine + orchestrator daemons and the control agent
            jobs.append("system/local.mdd.engine")
            jobs.append("system/local.mdd.orchestrator")
            if uid is not None:
                jobs.append(f"gui/{uid}/local.mdd.control")
        for target in jobs:
            result = subprocess.run(["launchctl", "kickstart", "-k", target],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                publish("failed", error_code="restart.error.failed",
                        error=f"{target}: {(result.stderr or '').strip()[:300]}")
                return
        publish("done")


def read_json_quiet(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}
