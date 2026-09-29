"""Host health for an appliance that is usually a single-board computer.

A VoWiFi line depends on the box it runs on far more than on anything this project
configures: on a Raspberry Pi 3 the Ethernet NIC hangs off the same USB 2.0 bus (and the same
power rail) as the cellular module and the card reader, so a brown-out takes out the network,
the modem and the SIM at the same instant. That failure reaches the status machine as a tunnel
that stopped passing traffic, which is indistinguishable from a bad carrier or a bad exit node
unless the host's own state is recorded next to it.

Everything here is read-only, cheap (procfs/sysfs plus one optional vcgencmd call) and
degrades silently: fields that a platform cannot answer are omitted rather than faked, so a
non-Pi host simply reports fewer of them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time

# vcgencmd get_throttled bits (Raspberry Pi firmware). The low nibble is "right now", the
# high half is "has happened since boot" and never clears until a power cycle.
THROTTLED_BITS = {
    0x1: "undervoltage",
    0x2: "frequency_capped",
    0x4: "throttled",
    0x8: "soft_temperature_limit",
}
THROTTLED_STICKY = {
    0x10000: "undervoltage",
    0x20000: "frequency_capped",
    0x40000: "throttled",
    0x80000: "soft_temperature_limit",
}
# Free space below this is a real risk on an SD card: a full root filesystem corrupts the
# SQLite history and stops the engines from writing their runtime state.
DISK_WARN_PERCENT = float(os.environ.get("MDD_DISK_WARN_PERCENT", "90"))
DISK_CRITICAL_PERCENT = float(os.environ.get("MDD_DISK_CRITICAL_PERCENT", "96"))
# Sustained paging to an SD card slows every operation enough to time out status reads. The
# signal is the paging RATE, not how much swap is occupied: pages parked there since boot and
# never touched again cost nothing, and alerting on occupancy fires on a perfectly healthy box.
SWAP_PAGES_PER_SECOND = float(os.environ.get("MDD_SWAP_PAGES_PER_SECOND", "50"))
DOCKER_STORAGE_CACHE_SECONDS = float(
    os.environ.get("MDD_DOCKER_STORAGE_CACHE_SECONDS", "21600"))
_docker_storage_cache: dict = {}
_docker_storage_cached_at = 0.0
_docker_storage_lock = threading.Lock()
# The Pi throttles at 80°C, and a loaded board sits in the seventies quite normally. A
# threshold inside the ordinary operating range just flaps across it all day.
TEMPERATURE_WARN_C = float(os.environ.get("MDD_TEMPERATURE_WARN_C", "80"))


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _vcgencmd(*args: str) -> str:
    binary = shutil.which("vcgencmd")
    if not binary:
        return ""
    try:
        result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=4)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


_darwin_hardware_cache: dict = {}


def _darwin_hardware() -> dict:
    """Model/serial from system_profiler on macOS; {} on any failure."""
    if _darwin_hardware_cache:
        return _darwin_hardware_cache
    if sys.platform != "darwin":
        return {}
    try:
        result = subprocess.run(
            ["system_profiler", "-timeout", "15", "-detailLevel", "mini",
             "SPHardwareDataType"],
            capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return {}
        info = {}
        for line in result.stdout.splitlines():
            if line.strip().startswith("Model Name:"):
                info["model"] = line.split(":", 1)[1].strip()
            elif line.strip().startswith("Serial Number (system):"):
                info["serial"] = line.split(":", 1)[1].strip()
        _darwin_hardware_cache.update(info)
        return _darwin_hardware_cache
    except (OSError, subprocess.SubprocessError):
        return {}


def model() -> str:
    """Board model, from the device tree on ARM boards and DMI elsewhere."""
    if sys.platform == "darwin":
        return _darwin_hardware().get("model", "")
    value = _read("/proc/device-tree/model").replace("\x00", "").strip()
    return value or _read("/sys/class/dmi/id/product_name")


def serial() -> str:
    """Machine serial number where the platform exposes one."""
    if sys.platform == "darwin":
        return _darwin_hardware().get("serial", "")
    return ""


def throttling() -> dict:
    """Decode the firmware's power/thermal verdict.

    ``get_throttled`` is the only place a Pi admits to a brown-out. Nothing else in the
    system reports it: the NIC shows no errors, the link stays up, and the symptom surfaces
    minutes later as unexplained packet loss.
    """
    raw = _vcgencmd("get_throttled")
    match = re.search(r"0x([0-9a-fA-F]+)", raw)
    if not match:
        return {}
    value = int(match.group(1), 16)
    return {
        "raw": f"0x{value:x}",
        "now": sorted(name for bit, name in THROTTLED_BITS.items() if value & bit),
        "since_boot": sorted(name for bit, name in THROTTLED_STICKY.items() if value & bit),
    }


def undervoltage_events() -> dict:
    """Count firmware brown-out messages in the kernel ring buffer.

    The sticky bit only says "it happened once". The count and the most recent timestamp are
    what distinguish a one-off at boot from a supply that dips every few minutes under load.
    """
    try:
        result = subprocess.run(["dmesg", "-T"], capture_output=True, text=True, timeout=8)
        if result.returncode != 0:
            return {}
        lines = [line for line in result.stdout.splitlines() if "Undervoltage detected" in line]
    except (OSError, subprocess.SubprocessError):
        return {}
    stamp = ""
    if lines:
        found = re.search(r"\[(.*?)\]", lines[-1])
        stamp = found.group(1) if found else ""
    return {"count": len(lines), "last": stamp}


def temperature_c() -> float | None:
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        value = float(raw) / 1000.0
    except ValueError:
        return None
    # Some platforms report millidegrees, others degrees; reject anything implausible.
    return round(value, 1) if 0 < value < 150 else None


def cpu_mhz() -> int | None:
    raw = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if raw.isdigit():
        return int(raw) // 1000
    match = re.search(r"=(\d+)", _vcgencmd("measure_clock", "arm"))
    return int(match.group(1)) // 1_000_000 if match else None


def memory() -> dict:
    values = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        digits = rest.strip().split(" ")[0]
        if digits.isdigit():
            values[key] = int(digits) // 1024          # MiB
    total, available = values.get("MemTotal", 0), values.get("MemAvailable", 0)
    swap_total, swap_free = values.get("SwapTotal", 0), values.get("SwapFree", 0)
    swap_used = swap_total - swap_free
    paging = {}
    for line in _read("/proc/vmstat").splitlines():
        key, _, rest = line.partition(" ")
        if key in ("pswpin", "pswpout") and rest.strip().isdigit():
            paging[key] = int(rest.strip())
    return {
        "total_mb": total, "available_mb": available,
        "used_percent": round((total - available) * 100 / total, 1) if total else 0.0,
        "swap_total_mb": swap_total, "swap_used_mb": swap_used,
        "swap_used_percent": round(swap_used * 100 / swap_total, 1) if swap_total else 0.0,
        # Cumulative since boot; a rate needs two samples, which alerts() compares.
        "swap_in_pages": paging.get("pswpin", 0), "swap_out_pages": paging.get("pswpout", 0),
    }


def load() -> dict:
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        return {}
    cores = os.cpu_count() or 1
    return {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2),
            "cores": cores, "per_core": round(one / cores, 2)}


def disk(path: str) -> dict:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {}
    return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
            "total_mb": usage.total // (1024 * 1024), "free_mb": usage.free // (1024 * 1024),
            "used_percent": round((usage.total - usage.free) * 100 / usage.total, 1)
            if usage.total else 0.0}


def _path_usage_bytes(path: str) -> int:
    """Allocated bytes under one project path, without following symlinks."""
    try:
        result = subprocess.run(["du", "-sk", os.path.abspath(path)], capture_output=True,
                                text=True, timeout=15)
        value = result.stdout.split(None, 1)[0] if result.returncode == 0 else ""
        if value.isdigit():
            return int(value) * 1024
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    total = 0
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in [*dirs, *files]:
                try:
                    stat = os.lstat(os.path.join(root, name))
                except OSError:
                    continue
                total += stat.st_blocks * 512 if stat.st_blocks else stat.st_size
    except OSError:
        return 0
    return total


def _project_paths(data_dir: str) -> list[str]:
    """Runtime data plus native-install source/venv paths, with nested paths counted once."""
    candidates = [data_dir, os.environ.get("MDD_REPO_DIR", ""),
                  os.environ.get("MDD_VENV_DIR", "")]
    paths = []
    for candidate in sorted({os.path.realpath(item) for item in candidates if item}, key=len):
        if not os.path.exists(candidate):
            continue
        try:
            if any(os.path.commonpath([candidate, parent]) == parent for parent in paths):
                continue
        except ValueError:
            pass
        paths.append(candidate)
    return paths


def _docker_storage(*, refresh: bool = False) -> dict:
    """MDD image/container usage plus separately identified shared builder cache.

    Image ``Size`` values are virtual sizes and repeat shared layers. Docker's aggregate usage
    objects carry the real layer-store total and reclaimable bytes. Builder records created by
    legacy releases have no project label, so report the daemon's shared cache separately and
    use Docker's own conservative-prune estimate rather than treating every ``InUse=false``
    record as deletable.
    """
    global _docker_storage_cache, _docker_storage_cached_at
    now = time.monotonic()
    if (not refresh and _docker_storage_cache
            and now - _docker_storage_cached_at < DOCKER_STORAGE_CACHE_SECONDS):
        return dict(_docker_storage_cache)
    # A DSM daemon may spend over a minute walking xattrs for one df() call. Serialise callers
    # and cache the result so the 60-second host health poll cannot keep dockerd permanently busy.
    with _docker_storage_lock:
        now = time.monotonic()
        if (not refresh and _docker_storage_cache
                and now - _docker_storage_cached_at < DOCKER_STORAGE_CACHE_SECONDS):
            return dict(_docker_storage_cache)
        try:
            import docker
            client = docker.from_env(timeout=10)
        except Exception:
            return dict(_docker_storage_cache)
        try:
            report = client.df()
        except Exception:
            return dict(_docker_storage_cache)
        finally:
            try:
                client.close()
            except Exception:
                pass

    repositories = ("mdd-sim-gateway/", "ghcr.io/mddidd/mdd-sim-gateway-")
    image_bytes, image_ids = 0, set()
    for item in report.get("Images") or []:
        image_id = str(item.get("Id") or item.get("ID") or "")
        labels = item.get("Labels") or {}
        labels = labels if isinstance(labels, dict) else {}
        tags = [str(tag) for tag in (item.get("RepoTags") or []) if tag]
        managed = labels.get("io.mdd-sim-gateway.managed") == "true" or any(
            tag.startswith(repositories) for tag in tags)
        if managed and image_id not in image_ids:
            image_ids.add(image_id)
            image_bytes += max(0, int(item.get("Size") or 0))

    writable_bytes, container_ids = 0, set()
    for item in report.get("Containers") or []:
        container_id = str(item.get("Id") or item.get("ID") or "")
        labels = item.get("Labels") or {}
        labels = labels if isinstance(labels, dict) else {}
        image = str(item.get("Image") or "")
        managed = labels.get("io.mdd-sim-gateway.managed") == "true" or image.startswith(
            repositories)
        if managed and container_id not in container_ids:
            container_ids.add(container_id)
            writable_bytes += max(0, int(item.get("SizeRw") or 0))

    image_usage = report.get("ImageUsage") or {}
    image_items = image_usage.get("Items") or report.get("Images") or []
    all_image_ids = {str(item.get("Id") or item.get("ID") or "")
                     for item in image_items if item.get("Id") or item.get("ID")}
    all_images_are_mdd = bool(all_image_ids) and all_image_ids == image_ids
    current_tags = {"mdd-sim-gateway/engine:latest",
                    "mdd-sim-gateway/control:latest",
                    "mdd-sim-gateway/engine-base:trusted"}
    old_image_count, protected_unused = 0, False
    for item in image_items:
        image_id = str(item.get("Id") or item.get("ID") or "")
        if image_id not in image_ids or int(item.get("Containers") or 0) != 0:
            continue
        tags = {str(tag) for tag in (item.get("RepoTags") or []) if tag}
        if tags & current_tags:
            protected_unused = True
        else:
            old_image_count += 1
    layer_bytes = max(0, int(image_usage.get("TotalSize")
                             or report.get("LayersSize") or 0))
    image_reclaimable = max(0, int(image_usage.get("Reclaimable") or 0))
    old_image_reclaimable = (image_reclaimable
                             if all_images_are_mdd and not protected_unused else None)

    cache = report.get("BuildCache") or []
    cache_usage = report.get("BuildCacheUsage") or {}
    cache_bytes = max(0, int(cache_usage.get("TotalSize")
                             or sum(max(0, int(item.get("Size") or 0)) for item in cache)))
    if "Reclaimable" in cache_usage:
        cache_reclaimable = max(0, int(cache_usage.get("Reclaimable") or 0))
    else:
        # Older daemons do not publish the aggregate. Shared records are parents of reusable
        # cache and survive the default prune; unshared, unused records are the safe estimate.
        cache_reclaimable = sum(max(0, int(item.get("Size") or 0)) for item in cache
                                if not item.get("InUse") and not item.get("Shared"))
    result = {"docker_images_bytes": image_bytes,
              "docker_image_layers_bytes": layer_bytes,
              "docker_image_reclaimable_bytes": image_reclaimable,
              "docker_images_all_managed": all_images_are_mdd,
              "mdd_old_image_count": old_image_count,
              "mdd_old_images_reclaimable_bytes": old_image_reclaimable,
              "container_writable_bytes": writable_bytes,
              "build_cache_bytes": cache_bytes,
              "build_cache_reclaimable_bytes": cache_reclaimable}
    _docker_storage_cache = dict(result)
    _docker_storage_cached_at = time.monotonic()
    return result


def project_storage(data_dir: str, *, include_docker_storage: bool = True) -> dict:
    files = sum(_path_usage_bytes(path) for path in _project_paths(data_dir))
    result = {"files_bytes": files}
    if include_docker_storage:
        result.update(_docker_storage())
    image_storage = (result.get("docker_image_layers_bytes", 0)
                     if result.get("docker_images_all_managed")
                     else result.get("docker_images_bytes", 0))
    result["known_total_bytes"] = (files + image_storage
                                   + result.get("container_writable_bytes", 0))
    result["known_total_is_logical"] = not bool(result.get("docker_images_all_managed"))
    return result


def uptime_seconds() -> int:
    raw = _read("/proc/uptime").split(" ")
    try:
        return int(float(raw[0]))
    except (ValueError, IndexError):
        return 0


def _interface_counters(name: str) -> dict:
    base = f"/sys/class/net/{name}/statistics"
    out = {}
    for key in ("rx_errors", "rx_dropped", "tx_errors", "tx_dropped"):
        raw = _read(f"{base}/{key}")
        if raw.isdigit():
            out[key] = int(raw)
    return out


def default_route_interfaces() -> list[str]:
    """Interfaces carrying a default route, newest kernel format, best effort.

    Listed in kernel order, so the first is the one currently in use. More than one is normal
    redundancy; it is the choice *moving* that changes the source address every outbound
    connection is made from.
    """
    try:
        result = subprocess.run(["ip", "-4", "route", "show", "default"],
                                capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if "dev" in parts:
            name = parts[parts.index("dev") + 1]
            if name not in found:
                found.append(name)
    return found


def network() -> dict:
    """Uplink interfaces, plus whether the primary one is a USB device.

    A USB-attached NIC shares its bus and power rail with the modem and card reader, so it
    fails at the same moment they do — the single most confusing failure mode on this
    hardware, because every line drops at once and nothing blames the host.
    """
    interfaces = default_route_interfaces()
    primary = interfaces[0] if interfaces else ""
    result = {"default_interfaces": interfaces, "multiple_default_routes": len(interfaces) > 1,
              "addresses": interface_addresses()}
    if not primary:
        return result
    result["primary"] = primary
    result["counters"] = _interface_counters(primary)
    link = os.path.realpath(f"/sys/class/net/{primary}")
    result["usb_attached"] = "/usb" in link
    return result


def interface_addresses() -> list[dict]:
    """Addresses of the real interfaces, so an operator can see which network this box is on.

    Loopback and the gateway's own tunnels are filtered out: they are implementation detail
    and would bury the one or two addresses that identify the host on its LAN.
    """
    try:
        result = subprocess.run(["ip", "-o", "addr", "show"],
                                capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] not in ("inet", "inet6"):
            continue
        name, family, address = parts[1], parts[2], parts[3]
        if name == "lo" or name.startswith(("mdd-", "ipsec", "veth", "docker", "br-")):
            continue
        if family == "inet6" and address.lower().startswith("fe80"):
            continue
        found.append({"interface": name, "family": "ipv4" if family == "inet" else "ipv6",
                      "address": address})
    return found


def usb_devices() -> list[str]:
    """Devices sharing the USB bus, named from sysfs so no lsusb dependency is needed."""
    names = []
    for entry in sorted(os.listdir("/sys/bus/usb/devices")) if os.path.isdir(
            "/sys/bus/usb/devices") else []:
        product = _read(f"/sys/bus/usb/devices/{entry}/product")
        if product:
            names.append(product)
    return names


def collect(data_dir: str = "/", *, include_docker_storage: bool = True) -> dict:
    """One host snapshot. Cheap enough to record on every line failure."""
    snapshot = {
        "ts": int(time.time()),
        "model": model(),
        "uptime_seconds": uptime_seconds(),
        "temperature_c": temperature_c(),
        "cpu_mhz": cpu_mhz(),
        "load": load(),
        "memory": memory(),
        "disk": disk(data_dir),
        "project_storage": project_storage(
            data_dir, include_docker_storage=include_docker_storage),
        "network": network(),
    }
    for key, value in (("throttling", throttling()),
                       ("undervoltage", undervoltage_events()),
                       ("usb_devices", usb_devices()),
                       ("serial", serial())):
        if value:
            snapshot[key] = value
    return snapshot


def swap_paging_rate(snapshot: dict, previous: dict | None) -> float | None:
    """Pages per second moved to or from swap between two samples.

    Occupancy says nothing: a box that swapped once at boot and never touched those pages
    again reports a large number forever while performing perfectly. Only movement costs
    anything, and on an SD card it costs enough to time out a status read.
    """
    if not previous:
        return None
    elapsed = (snapshot.get("ts") or 0) - (previous.get("ts") or 0)
    if elapsed <= 0:
        return None
    now, before = snapshot.get("memory") or {}, previous.get("memory") or {}
    moved = ((now.get("swap_in_pages", 0) - before.get("swap_in_pages", 0))
             + (now.get("swap_out_pages", 0) - before.get("swap_out_pages", 0)))
    if moved < 0:                       # counters reset across a reboot
        return None
    return moved / elapsed


def alerts(snapshot: dict, previous: dict | None = None) -> list[dict]:
    """Conditions an operator must act on, worst first.

    Deliberately narrow: every entry here means "the hardware cannot do its job", because an
    alert that fires on ordinary load teaches people to ignore the banner that would have
    explained a real outage.

    ``previous`` is an earlier snapshot. Rate-based conditions need two samples and are simply
    not reported without one, rather than being approximated from a single reading.
    """
    found = []
    throttle = snapshot.get("throttling") or {}
    now, sticky = set(throttle.get("now") or []), set(throttle.get("since_boot") or [])
    undervoltage = snapshot.get("undervoltage") or {}

    if "undervoltage" in now:
        found.append({"code": "undervoltage_now", "severity": "critical",
                      "detail": {"events": undervoltage.get("count", 0)}})
    elif "undervoltage" in sticky:
        found.append({"code": "undervoltage_seen", "severity": "warning",
                      "detail": {"events": undervoltage.get("count", 0),
                                 "last": undervoltage.get("last", "")}})
    if now & {"throttled", "frequency_capped", "soft_temperature_limit"}:
        found.append({"code": "throttled_now", "severity": "warning",
                      "detail": {"reasons": sorted(now - {"undervoltage"})}})

    temperature = snapshot.get("temperature_c")
    if temperature is not None and temperature >= TEMPERATURE_WARN_C:
        found.append({"code": "temperature_high", "severity": "warning",
                      "detail": {"celsius": temperature}})

    disk_used = (snapshot.get("disk") or {}).get("used_percent")
    if disk_used is not None and disk_used >= DISK_CRITICAL_PERCENT:
        found.append({"code": "disk_critical", "severity": "critical",
                      "detail": {"used_percent": disk_used}})
    elif disk_used is not None and disk_used >= DISK_WARN_PERCENT:
        found.append({"code": "disk_low", "severity": "warning",
                      "detail": {"used_percent": disk_used}})

    network_state = snapshot.get("network") or {}
    rate = swap_paging_rate(snapshot, previous)
    if rate is not None and rate >= SWAP_PAGES_PER_SECOND:
        found.append({"code": "swap_pressure", "severity": "warning",
                      "detail": {"pages_per_second": round(rate),
                                 "used_percent": (snapshot.get("memory") or {})
                                 .get("swap_used_percent", 0.0)}})

    # Having a second uplink configured is normal, often deliberate redundancy, and the kernel
    # picks between them deterministically by metric — nothing is wrong until the choice
    # actually moves. A change is worth reporting because it swaps the source address every
    # outbound connection is made from, which can drop every line at the same instant.
    was, now_primary = (previous or {}).get("network", {}).get("primary"), network_state.get("primary")
    if previous and was and now_primary and was != now_primary:
        found.append({"code": "default_route_changed", "severity": "warning",
                      "detail": {"from": was, "to": now_primary}})

    order = {"critical": 0, "warning": 1}
    found.sort(key=lambda item: order.get(item["severity"], 9))
    return found
