"""Local network addresses of this machine, for diagnosis when the overlay (Tailscale) is down.

Tailscale (100.x) is the fleet's primary address, but LAN addresses drift (DHCP, randomised
Wi-Fi MACs), so each worker reports the ones it has right now and the orchestrator shows
them in /machines. stdlib only: `ip` on Linux, `ifconfig` on macOS.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess

# Interfaces that are not "the machine's network": overlays, containers, loopback, Apple
# peer-to-peer. Reporting a docker bridge as a LAN address would send someone to the
# wrong place at exactly the moment they need the right one.
_SKIP_IFACE = re.compile(r"^(lo\d*|docker\d*|br-.*|veth.*|virbr.*|tailscale\d*|utun\d*|awdl\d*|llw\d*|"
                         r"bridge\d*|gif\d*|stf\d*|anpi\d*|ap\d*|vmnet.*|vboxnet.*)$")
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _usable(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and not (a.is_loopback or a.is_link_local or a in _TAILSCALE_NET)


def parse_ip_addr(text: str) -> dict[str, str]:
    """`ip -o -4 addr show up` → {iface: ip}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/", line)
        if m and not _SKIP_IFACE.match(m.group(1).split("@")[0]) and _usable(m.group(2)):
            out.setdefault(m.group(1).split("@")[0], m.group(2))
    return out


def parse_ifconfig(text: str) -> dict[str, str]:
    """macOS `ifconfig` → {iface: ip}, only for interfaces that are up and active."""
    out: dict[str, str] = {}
    for block in re.split(r"\n(?=\S)", text):
        head = re.match(r"(\S+?):", block)
        if not head or _SKIP_IFACE.match(head.group(1)):
            continue
        if "status: inactive" in block:
            continue
        m = re.search(r"\n\s+inet (\d+\.\d+\.\d+\.\d+) ", "\n" + block)
        if m and _usable(m.group(1)):
            out.setdefault(head.group(1), m.group(1))
    return out


def _find(name: str) -> str | None:
    """A service's PATH is minimal (launchd has no /sbin), so look in the usual places too."""
    found = shutil.which(name)
    if found:
        return found
    for d in ("/sbin", "/usr/sbin", "/bin", "/usr/bin"):
        if os.path.isfile(f"{d}/{name}") and os.access(f"{d}/{name}", os.X_OK):
            return f"{d}/{name}"
    return None


def lan_ips() -> dict[str, str]:
    """{interface: ipv4} for this machine's real LAN interfaces; {} if it cannot tell."""
    try:
        ip = _find("ip")
        if ip:
            r = subprocess.run([ip, "-o", "-4", "addr", "show", "up"],
                               capture_output=True, text=True, timeout=3)
            return parse_ip_addr(r.stdout) if r.returncode == 0 else {}
        ifc = _find("ifconfig")
        if ifc:
            r = subprocess.run([ifc], capture_output=True, text=True, timeout=3)
            return parse_ifconfig(r.stdout) if r.returncode == 0 else {}
    except (subprocess.SubprocessError, OSError):
        pass
    return {}
