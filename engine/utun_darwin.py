# SPDX-License-Identifier: GPL-3.0-only
# mdd-sim-gateway macOS port: utun(4) tunnel device for swu_ike.py.
#
# Replaces Linux /dev/net/tun + TUNSETIFF (IFF_TUN | IFF_NO_PI). Two differences
# the caller must respect:
#   1. The fd carries a 4-byte address-family header on EVERY read and write.
#      Verified on darwin 24 x86_64: big-endian in BOTH directions (00 00 00 02
#      for AF_INET). A wrong-endian header is accepted by write(2) but the packet
#      is silently dropped, so these helpers are the only supported path.
#   2. The interface name (utunN) is assigned by the kernel, not chosen. pjsip's
#      Linux bind_interface=ipsec0 has no macOS equivalent; the P-CSCF /32 scoped
#      route (see swu_ike set_routes) provides the isolation instead.
import fcntl
import os
import socket
import struct
import subprocess

PF_SYSTEM = 32
AF_SYS_CONTROL = 2
SYSPROTO_CONTROL = 2
UTUN_CONTROL_NAME = b"com.apple.net.utun_control"
CTLIOCGINFO = 0xC0644E03  # _IOWR('N', 3, struct ctl_info)

CTL_INFO_FMT = "I96s"     # struct ctl_info { u_int32_t ctl_id; char ctl_name[96]; }


def open_utun():
    """Create a utun interface by probing units 1..63 (unit N -> utun(N-1)).

    Returns (fd, ifname) with fd detached from the socket object (plain int fd,
    so select/os.read/os.write work on it exactly like the Linux tun fd).
    """
    sock = socket.socket(PF_SYSTEM, socket.SOCK_DGRAM, SYSPROTO_CONTROL)
    ctl_info = fcntl.ioctl(sock, CTLIOCGINFO,
                           struct.pack(CTL_INFO_FMT, 0, UTUN_CONTROL_NAME))
    ctl_id = struct.unpack(CTL_INFO_FMT, ctl_info)[0]
    for unit in range(1, 64):
        try:
            sock.connect((ctl_id, unit))
        except OSError:
            continue
        return sock.detach(), "utun%d" % (unit - 1)
    sock.close()
    raise RuntimeError("no free utun unit (1-63 all busy?)")


def strip_header(buf):
    """One utun read -> bare IP packet (drop the 4-byte AF header)."""
    return buf[4:]


def family_of(packet):
    version = packet[0] >> 4 if packet else 0
    return socket.AF_INET if version == 4 else socket.AF_INET6


def prepend_header(packet):
    """One bare IP packet -> utun write buffer (4-byte BE AF header)."""
    return struct.pack(">I", family_of(packet)) + packet


def configure(ifname, inner_v4, mtu=None):
    """Point-to-point IPv4 address (+ optional MTU) + up, via ifconfig.

    Linux equivalent: ip addr add <inner>/32 dev <dev>; ip link set dev <dev> up;
    ip link set dev <dev> mtu <mtu>. For a /32 tunnel the PTP destination is the
    local address itself. MTU is applied separately by the caller's MTU sizing
    pass, so it is optional here.
    """
    rc = os.system("ifconfig %s inet %s %s up" % (ifname, inner_v4, inner_v4))
    if rc != 0:
        raise RuntimeError("ifconfig %s inet failed rc=%d" % (ifname, rc))
    if mtu:
        set_mtu(ifname, mtu)


def set_mtu(ifname, mtu):
    rc = os.system("ifconfig %s mtu %d" % (ifname, mtu))
    if rc != 0:
        raise RuntimeError("ifconfig %s mtu failed rc=%d" % (ifname, rc))


def add_host_route(addr, ifname):
    """Scoped /32 (or /128 for v6) route into the tunnel.

    Replaces the container's 0.0.0.0/1 + 128.0.0.0/1 full capture, which would
    hijack the whole Mac's traffic outside a network namespace. Only the ePDG
    and P-CSCF (and SDP-learned media peers) get tunnel routes.
    """
    if ":" in addr:
        cmd = "route add -inet6 -host %s -interface %s" % (addr, ifname)
    else:
        cmd = "route add -host %s -interface %s" % (addr, ifname)
    return os.system(cmd + " 2>/dev/null") == 0


def delete_host_route(addr):
    if ":" in addr:
        cmd = "route delete -inet6 -host %s" % addr
    else:
        cmd = "route delete -host %s" % addr
    return os.system(cmd + " 2>/dev/null") == 0


def default_gateway():
    """Default-route IPv4 next hop, from `route -n get default` (no /proc/net/route)."""
    try:
        out = subprocess_check_output("route -n get default")
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("gateway:"):
                return line.split()[1]
    except Exception:
        pass
    return ""


def subprocess_check_output(cmd):
    return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode()


def interface_mtu(ifname):
    """MTU of a physical interface, from ifconfig (no /sys/class/net)."""
    try:
        out = subprocess_check_output("ifconfig %s" % ifname)
        for line in out.splitlines():
            parts = line.split()
            if "mtu" in parts:
                return int(parts[parts.index("mtu") + 1])
    except Exception:
        pass
    return 0


def default_route_interface(dest):
    """Interface the kernel would use for dest, from `route -n get` (no `ip route get`)."""
    try:
        out = subprocess_check_output("route -n get %s" % dest)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("interface:"):
                return line.split()[1]
    except Exception:
        pass
    return ""
