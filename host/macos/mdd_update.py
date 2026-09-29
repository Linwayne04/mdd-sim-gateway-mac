#!/usr/bin/env python3
"""mdd_update.py — root-runnable updater for the macOS native port.

Driven by a request file written by the WebUI (control/app/update_check.py) or
by `install-macos.sh update`:

  $MDD_DATA/orchestrator/update-request.json  {"version": ..., "repository": ...,
                                              "requested_at": <epoch>, ...}
  $MDD_DATA/orchestrator/update-status.json   {"state": ..., "phase": ...,
                                              "error": null|str, "target": ...,
                                              "updated_at": <epoch>}

Runs as the root LaunchDaemon local.mdd.update (see host/macos/local.mdd.update.plist)
on a 300-second StartInterval. With no pending request it exits 0 immediately
(cheap poll); with a request it: backs up the data dir, checks out the requested
release tag in the repo, rebuilds the native pieces, and kickstarts the engine
daemon + control agent. Status is written atomically at every step so the WebUI
can render progress. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

MDD_BUILD = Path(os.environ.get("MDD_BUILD", str(Path.home() / "mdd-macos-build")))
MDD_REPO = Path(os.environ.get("MDD_REPO", str(Path(__file__).resolve().parents[2])))
MDD_DATA = Path(os.environ.get("MDD_DATA", str(MDD_BUILD / "data")))
MDD_UPDATE_REPO = os.environ.get("MDD_UPDATE_REPO", "Linwayne04/mdd-sim-gateway-mac")

ORCH_DIR = MDD_DATA / "orchestrator"
REQUEST_PATH = ORCH_DIR / "update-request.json"
STATUS_PATH = ORCH_DIR / "update-status.json"
LOG_DIR = MDD_DATA / "logs"
LOG_PATH = LOG_DIR / "update.log"

GIT_TIMEOUT = 120
BUILD_TIMEOUT = 600
RUNNING_STALE_SECONDS = 30 * 60
BACKUP_MAX_AGE_SECONDS = 24 * 3600

# Best-effort secret scrubbing for anything we log: token=/secret=/password=/
# authorization=/api_key= values and 'Bearer <hex>' strings.
_SCRUB_PATTERNS = [
    (re.compile(r"(?i)\b(token|secret|password|authorization|api[_-]?key)=([^\s]+)"),
     r"\1=***"),
    (re.compile(r"Bearer\s+[0-9a-fA-F]{8,}"), "Bearer ***"),
]


def scrub(text: str) -> str:
    for pattern, repl in _SCRUB_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def log(message: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {scrub(str(message))}"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def write_status(state: str, phase: str, *, target: str = "",
                 error: str | None = None, extra: dict | None = None) -> None:
    payload = {"state": state, "phase": phase, "target": target, "error": error,
               "updated_at": int(time.time())}
    if extra:
        payload.update(extra)
    ORCH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, STATUS_PATH)


def read_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_request() -> dict:
    try:
        return json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run(cmd: list[str], *, timeout: int = 60, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a command with a timeout, capturing (scrubbed) output for the log."""
    log(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT after {timeout}s: {cmd[0]}")
        raise
    except OSError as exc:
        log(f"spawn failed: {exc}")
        raise
    output = (result.stdout or "")[-2000:] + (result.stderr or "")[-2000:]
    if result.returncode != 0:
        log(f"exit {result.returncode}: {scrub(output.strip())[-800:]}")
    return result


def fail(target: str, phase: str, message: str, *, extra: dict | None = None) -> int:
    log(f"FAILED at {phase}: {message}")
    write_status("failed", phase, target=target, error=message, extra=extra)
    return 1


def resolve_version(version: str) -> tuple[str, str]:
    """Return (display_version, git_ref). 'latest' is resolved via the GitHub API."""
    version = (version or "latest").strip()
    if version != "latest":
        return version, version
    url = f"https://api.github.com/repos/{MDD_UPDATE_REPO}/releases/latest"
    log(f"resolving latest release via GitHub API ({url})")
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            info = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"could not resolve latest release: {exc}") from exc
    tag = (info.get("tag_name") or "").lstrip("v")
    if not tag:
        raise RuntimeError("latest release has no usable tag_name")
    return tag, info["tag_name"]


def phase_backup(target: str) -> Path | None:
    """Archive $MDD_DATA unless a fresh pre-update backup already exists."""
    backups = MDD_DATA / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    fresh = [item for item in backups.glob(f"pre-update-{target}-*.tar.gz")
             if time.time() - item.stat().st_mtime < BACKUP_MAX_AGE_SECONDS]
    if fresh:
        log(f"backup: fresh pre-update backup exists ({fresh[0].name}) — skipping")
        return fresh[0]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = backups / f"pre-update-{target}-{stamp}.tar.gz"
    log(f"backup: archiving data dir -> {archive.name}")
    # bsdtar: --exclude must precede the file operands.
    result = run(["tar", "-czf", str(archive), "--exclude", "data/backups",
                  "--exclude", "data/run", "-C", str(MDD_BUILD), "data"],
                 timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"backup tar exited {result.returncode}")
    size_mb = archive.stat().st_size / (1024 * 1024)
    log(f"backup: {archive.name} written ({size_mb:.1f} MiB)")
    return archive


def phase_repo(target: str) -> str:
    """Fetch + checkout the release tag; returns the previous HEAD for rollback info."""
    status = run(["git", "-C", str(MDD_REPO), "status", "--porcelain"], timeout=GIT_TIMEOUT)
    if status.returncode != 0:
        raise RuntimeError(f"git status failed: {scrub((status.stderr or '').strip()[-300:])}")
    if status.stdout.strip():
        raise RuntimeError(
            "repo has uncommitted local changes — refusing to update; commit or stash first")
    previous = run(["git", "-C", str(MDD_REPO), "rev-parse", "HEAD"], timeout=GIT_TIMEOUT)
    if previous.returncode != 0:
        raise RuntimeError("could not record previous HEAD")
    prev_head = previous.stdout.strip()
    log(f"repo: previous HEAD {prev_head}")
    result = run(["git", "-C", str(MDD_REPO), "fetch", "--tags", "origin"], timeout=GIT_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError("git fetch --tags failed")
    result = run(["git", "-C", str(MDD_REPO), "checkout", target], timeout=GIT_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"git checkout {target} failed")
    log(f"repo: checked out {target}")
    return prev_head


def phase_rebuild() -> None:
    venv_pip = MDD_BUILD / "venv" / "bin" / "pip"
    steps: list[tuple[str, list[str]]] = [
        ("fetch-sources", [str(MDD_REPO / "host/macos/build/fetch-sources.sh"), str(MDD_BUILD)]),
        ("support-libs", [str(MDD_REPO / "host/macos/build/build-support-libs.sh"), str(MDD_BUILD)]),
        ("asterisk", [str(MDD_REPO / "host/macos/build/build-asterisk.sh"), str(MDD_BUILD)]),
    ]
    if venv_pip.exists():
        steps.append(("venv-requirements",
                      [str(venv_pip), "install", "--quiet",
                       "-r", str(MDD_REPO / "control/requirements.txt")]))
    else:
        log("rebuild: venv pip missing — leaving venv as-is (install-macos.sh recreates it)")
    for name, cmd in steps:
        log(f"rebuild: step {name}")
        result = run(cmd, timeout=BUILD_TIMEOUT,
                     cwd=MDD_REPO / "host/macos/build")
        if result.returncode != 0:
            raise RuntimeError(f"rebuild step '{name}' exited {result.returncode}")
    webui_dist = MDD_REPO / "webui" / "dist" / "index.html"
    if webui_dist.exists():
        log("rebuild: webui/dist present — npm rebuild skipped on update")
    else:
        log("rebuild: webui rebuild skipped; run npm build manually")


def phase_restart() -> None:
    result = run(["launchctl", "kickstart", "-k", "system/local.mdd.engine"], timeout=30)
    if result.returncode != 0:
        log("restart: engine kickstart failed (logged, not fatal)")
    try:
        uid = os.stat(MDD_BUILD).st_uid
    except OSError as exc:
        log(f"restart: could not stat build root: {exc}")
        return
    result = run(["launchctl", "kickstart", "-k", f"gui/{uid}/local.mdd.control"], timeout=30)
    if result.returncode != 0:
        log("restart: control agent kickstart failed (logged, not fatal)")


def run_update() -> int:
    request = read_request()
    if not request:
        # Cheap early exit for the 300s poll interval: no request pending.
        return 0
    status = read_status()
    if (status.get("state") == "running"
            and int(time.time()) - int(status.get("updated_at") or 0) < RUNNING_STALE_SECONDS):
        log("update already running (fresh status) — exiting")
        return 0

    target = str(request.get("version") or "latest")
    write_status("running", "resolving", target=target)
    try:
        resolved, git_ref = resolve_version(target)
    except RuntimeError as exc:
        return fail(target, "resolving", str(exc))
    log(f"update requested: {target} -> {resolved} ({git_ref})")

    write_status("running", "backup", target=resolved)
    try:
        phase_backup(resolved)
    except (RuntimeError, subprocess.SubprocessError) as exc:
        return fail(resolved, "backup", str(exc))

    write_status("running", "repo", target=resolved)
    try:
        previous_head = phase_repo(git_ref)
    except RuntimeError as exc:
        return fail(resolved, "repo", str(exc), extra={"previous_head": ""})

    write_status("running", "rebuild", target=resolved, extra={"previous_head": previous_head})
    try:
        phase_rebuild()
    except (RuntimeError, subprocess.SubprocessError) as exc:
        return fail(resolved, "rebuild", str(exc), extra={"previous_head": previous_head})

    write_status("running", "restart", target=resolved, extra={"previous_head": previous_head})
    try:
        phase_restart()
    except (RuntimeError, subprocess.SubprocessError) as exc:
        return fail(resolved, "restart", str(exc), extra={"previous_head": previous_head})

    write_status("done", "finished", target=resolved, extra={"previous_head": previous_head})
    log(f"update to {resolved} finished")
    try:
        REQUEST_PATH.unlink()
    except OSError:
        pass
    return 0


def cmd_status() -> int:
    if STATUS_PATH.exists():
        print(STATUS_PATH.read_text(encoding="utf-8"), end="")
    else:
        print("no status")
    return 0


def cmd_request(version: str) -> int:
    ORCH_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"version": version, "repository": MDD_UPDATE_REPO,
               "requested_at": int(time.time()), "source": "cli"}
    tmp = REQUEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, REQUEST_PATH)
    log(f"update request written: version={version}")
    return 0


def main(argv: list[str]) -> int:
    if "--status" in argv:
        return cmd_status()
    if "--request" in argv:
        index = argv.index("--request")
        try:
            version = argv[index + 1]
        except IndexError:
            print("usage: mdd_update.py [--status] | [--request <version>] | [run]", file=sys.stderr)
            return 2
        return cmd_request(version)
    return run_update()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
