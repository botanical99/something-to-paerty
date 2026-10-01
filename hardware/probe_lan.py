"""Find Tuya devices (incl. the WG-S gateway) on the LAN.

All probes are passive/read-only:
  * tinytuya scanner: listens for Tuya UDP broadcasts (6666/6667/7000) and sends the
    standard Tuya discovery broadcast.
  * TCP sweep of port 6668 (Tuya local protocol port) - connect only, no payload.
  * ARP table dump, to match MAC addresses.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import subprocess


def tinytuya_scan(seconds: int = 20) -> list[dict]:
    """Blocking. Returns one dict per Tuya device seen on UDP."""
    import tinytuya
    from tinytuya import scanner

    found = scanner.devices(
        verbose=False, scantime=seconds, color=False, poll=False,
        forcescan=False, assume_yes=True,
    )
    devices = []
    for ip, d in (found or {}).items():
        devices.append({
            "ip": d.get("ip", ip),
            "gwId": d.get("gwId") or d.get("id"),
            "version": d.get("version"),
            "productKey": d.get("productKey"),
            "encrypted": d.get("encrypt"),
            "active": d.get("active"),
            "mac": d.get("mac"),
        })
    return devices


async def _probe(ip: str, port: int, timeout: float) -> str | None:
    try:
        fut = asyncio.open_connection(ip, port)
        _, w = await asyncio.wait_for(fut, timeout)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return ip
    except Exception:
        return None


async def tcp_sweep(local_ip: str, prefix: int = 24, port: int = 6668, timeout: float = 2.5) -> list[str]:
    net = ipaddress.ip_network(f"{local_ip}/{prefix}", strict=False)
    hosts = [str(h) for h in net.hosts() if str(h) != local_ip]
    if len(hosts) > 1024:  # refuse to sweep huge networks
        hosts = hosts[:1024]
    sem = asyncio.Semaphore(32)  # Wi-Fi needs ARP resolution; refused ports take ~2s

    async def guarded(h: str):
        async with sem:
            return await _probe(h, port, timeout)

    res = await asyncio.gather(*(guarded(h) for h in hosts))
    return [r for r in res if r]


def arp_table() -> dict[str, str]:
    """ip -> mac, from the OS neighbour cache."""
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {}
    table = {}
    for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})", out):
        table[m.group(1)] = m.group(2).lower().replace("-", ":")
    return table
