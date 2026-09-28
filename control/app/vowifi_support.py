"""Whether a SIM's carrier offers VoWiFi at all, before a line spends retries finding out.

Phones do not probe for this either. An iPhone shows the Wi-Fi Calling switch only when the
carrier bundle enables it, and Android reads `carrier_wfc_ims_available_bool` from its
carrier config. Here the same decision comes from two sources:

1. A small table of carriers known not to offer Wi-Fi Calling to ordinary subscribers.
2. The carrier's standard ePDG name. When public DNS answers "no such name", the carrier
   publishes no ePDG, so a tunnel cannot be built from the open internet.

"supported" only ever means worth trying: plan, IMEI allow-lists and country rules can still
refuse a line. "unsupported" is a strong statement, but the user can always try anyway.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time

SUPPORTED = "supported"
UNKNOWN = "unknown"
UNSUPPORTED = "unsupported"

# A reason is shown through t() in the WebUI, so every value here needs a zh entry
# (tests/test_i18n_coverage.py reads this table).
REASONS = {
    "carrier_table": "This carrier does not offer Wi-Fi Calling to ordinary subscribers. "
                     "You can still try it.",
    "mainland_china": "Mainland China carriers do not offer Wi-Fi Calling to ordinary "
                      "subscribers; China Telecom's pilot needs China Telecom home broadband "
                      "in selected cities. You can still try it.",
    "epdg_nxdomain": "The carrier publishes no VoWiFi (ePDG) address, so Wi-Fi Calling is "
                     "most likely not offered. You can still try it.",
    "epdg_placeholder": "The carrier's published VoWiFi (ePDG) address is a placeholder that "
                        "points at this device itself, so Wi-Fi Calling is most likely not "
                        "offered from this network. You can still try it.",
}

# (mcc, mnc) or (mcc, None) for every network of that country code.
_UNSUPPORTED = {
    ("460", None): "mainland_china",
}

# A published ePDG rarely disappears, and an absent one rarely appears: keep a clear answer
# for hours. A lookup that failed for another reason (no network, SERVFAIL) proves nothing
# and is retried soon.
_ANSWER_TTL = 6 * 3600
_ERROR_TTL = 300

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}
_probing: set[str] = set()


def epdg_fqdn(mcc: str, mnc: str) -> str:
    return (f"epdg.epc.mnc{str(mnc).zfill(3)}.mcc{str(mcc).zfill(3)}"
            ".pub.3gppnetwork.org")


def lookup(fqdn: str) -> str:
    """"ok", "nxdomain" (the name does not exist), "placeholder" (the only answers are
    loopback — the zone exists but the record is a dummy) or "error" (nothing learned)."""
    try:
        infos = socket.getaddrinfo(fqdn, None)
    except socket.gaierror as exc:
        # EAI_NONAME is an authoritative "no such name"; EAI_AGAIN/EAI_FAIL are outages.
        # EAI_NODATA (name exists, no address) is not defined on every platform.
        if exc.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)):
            return "nxdomain"
        return "error"
    except OSError:
        return "error"
    addresses = {info[4][0] for info in infos}
    if addresses and all(ipaddress.ip_address(addr).is_loopback for addr in addresses):
        return "placeholder"
    return "ok"


def probe(fqdn: str) -> str:
    """Resolve now and remember the answer (blocking; call off the event loop)."""
    result = lookup(fqdn)
    with _lock:
        _cache[fqdn] = (time.monotonic(), result)
        _probing.discard(fqdn)
    return result


def cached(fqdn: str) -> str | None:
    with _lock:
        entry = _cache.get(fqdn)
    if not entry:
        return None
    at, result = entry
    ttl = _ERROR_TTL if result == "error" else _ANSWER_TTL
    return result if time.monotonic() - at < ttl else None


def probe_in_background(fqdn: str) -> None:
    """Refresh a missing or stale answer without blocking the caller."""
    if cached(fqdn) is not None:
        return
    with _lock:
        if fqdn in _probing:
            return
        _probing.add(fqdn)
    threading.Thread(target=probe, args=(fqdn,), daemon=True).start()


def table_reason(mcc: str, mnc: str) -> str:
    mcc = str(mcc or "").zfill(3)
    mnc = str(mnc or "").zfill(3)
    return (_UNSUPPORTED.get((mcc, mnc)) or _UNSUPPORTED.get((mcc, mnc.lstrip("0") or "0"))
            or _UNSUPPORTED.get((mcc, None)) or "")


def assess(mcc: str, mnc: str, fqdn: str = "", *, dns: str | None = None) -> dict:
    """Verdict for one SIM. `dns` is a lookup result if the caller has one; otherwise the
    cached answer is used and nothing blocks."""
    if not str(mcc or "").strip():
        return {"status": UNKNOWN, "source": "", "reason": ""}
    key = table_reason(mcc, mnc)
    if key:
        return {"status": UNSUPPORTED, "source": "carrier_table", "reason": REASONS[key]}
    fqdn = fqdn or epdg_fqdn(mcc, mnc)
    answer = dns if dns is not None else cached(fqdn)
    if answer == "nxdomain":
        return {"status": UNSUPPORTED, "source": "dns", "reason": REASONS["epdg_nxdomain"]}
    if answer == "placeholder":
        return {"status": UNSUPPORTED, "source": "dns", "reason": REASONS["epdg_placeholder"]}
    if answer == "ok":
        return {"status": SUPPORTED, "source": "dns", "reason": ""}
    return {"status": UNKNOWN, "source": "", "reason": ""}


def for_instance(inst: dict | None, *, probe_now: bool = False) -> dict:
    inst = inst or {}
    mcc, mnc = str(inst.get("mcc") or ""), str(inst.get("mnc") or "")
    if not mcc:
        return assess("", "")
    fqdn = str(inst.get("epdg") or "") or epdg_fqdn(mcc, mnc)
    if table_reason(mcc, mnc):
        return assess(mcc, mnc, fqdn)
    if probe_now:
        return assess(mcc, mnc, fqdn, dns=probe(fqdn))
    probe_in_background(fqdn)
    return assess(mcc, mnc, fqdn)
