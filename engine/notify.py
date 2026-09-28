#!/usr/bin/env python3
"""
notify.py - Best-effort event hook: POST engine events to the manager.

Usage (from the Asterisk dialplan or swu_ike tunnel hooks):
  notify.py <event> [arg1] [arg2] ...

Events: call_in <from>, call_out <to>, sms_in <from> <body_b64>,
        sms_out <to> <body_b64>, tunnel_up, tunnel_down, registered, unregistered

Reads MANAGER_URL and MDD_ID from /run/mdd-sim-gateway/engine.env (written by render.py).
Never fails the caller (all exceptions swallowed).
"""
import os
import sys
import json
import time


def load_env():
    env = {}
    path = os.environ.get("MDD_ENV", "/run/mdd-sim-gateway/engine.env")
    try:
        with open(path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    except Exception:
        pass
    return env


def main():
    if len(sys.argv) < 2:
        return
    event = sys.argv[1]
    args = sys.argv[2:]
    env = load_env()
    manager_url = os.environ.get("MANAGER_URL") or env.get("MANAGER_URL", "")
    inst_id = os.environ.get("MDD_ID") or env.get("MDD_ID", "1")
    payload = {"ts": int(time.time()), "instance": inst_id, "event": event, "args": args}
    # Always append to a local event log so nothing is lost if the manager is down.
    # macOS port: MDD_PREFIX redirects /logs into the per-line instance dir; unset
    # (Linux container) keeps the historical path.
    try:
        events_dir = os.environ.get("MDD_PREFIX", "") + "/logs"
        os.makedirs(events_dir, exist_ok=True)
        with open(events_dir + "/events.jsonl", "a") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass
    if not manager_url:
        return
    url = f"{manager_url.rstrip('/')}/api/engine/event"
    token = os.environ.get("MANAGER_EVENT_TOKEN") or env.get("MANAGER_EVENT_TOKEN", "")
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
        requests.post(url, json=payload,
                      headers={"X-MDD-Engine-Token": token}, timeout=3, verify=False)
        return
    except ImportError:
        pass
    except Exception:
        return
    # Fallback for interpreters without requests (e.g. the macOS system
    # python3 that the dialplan shebang resolves to): stdlib urllib only.
    try:
        import ssl
        import urllib.request
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", "X-MDD-Engine-Token": token})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=3, context=ctx) as r:
            r.read(256)
    except Exception:
        pass


if __name__ == "__main__":
    main()
