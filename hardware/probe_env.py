"""Inspect the laptop: OS, Python, Bluetooth adapter(s), Wi-Fi/LAN."""
from __future__ import annotations

import json
import platform
import socket
import subprocess
import sys


def _powershell(script: str, timeout: float = 15) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _json_list(text: str) -> list:
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]


def local_network() -> dict:
    """Find the LAN IP / prefix used for the default route (no packets are sent)."""
    ip = None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # UDP connect only selects a route
        ip = s.getsockname()[0]
    except OSError:
        pass
    finally:
        s.close()
    prefix = 24
    if ip and platform.system() == "Windows":
        txt = _powershell(f"(Get-NetIPAddress -IPAddress {ip} -AddressFamily IPv4).PrefixLength")
        if txt.isdigit():
            prefix = int(txt)
    return {"ip": ip, "prefix": prefix}


def bluetooth_adapters() -> list[dict]:
    if platform.system() != "Windows":
        return []
    txt = _powershell(
        "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
        "Where-Object { $_.InstanceId -like 'USB*' -or $_.InstanceId -like 'PCI*' } | "
        "Select-Object Status,FriendlyName,InstanceId | ConvertTo-Json"
    )
    return [
        {"name": d.get("FriendlyName"), "status": d.get("Status"), "instance": d.get("InstanceId")}
        for d in _json_list(txt)
    ]


def wifi_info() -> dict:
    if platform.system() != "Windows":
        return {}
    info = {}
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return info
    for line in out.splitlines():
        if ":" in line:
            k, v = (p.strip() for p in line.split(":", 1))
            if k in ("SSID", "Band", "Channel", "Radio type", "State"):
                info[k] = v
    return info


def run() -> dict:
    net = local_network()
    return {
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "bluetooth_adapters": bluetooth_adapters(),
        "wifi": wifi_info(),
        "network": net,
    }
