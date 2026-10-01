"""Zero-credential interrogation of the gateway: what does it expose without a key?

Everything here is connect-and-read / listen-only:
  * full UDP broadcast payload (tinytuya scanner, all fields)
  * TCP connect scan of a short list of well-known + Tuya ports (single host)
  * mDNS / SSDP multicast queries (standard discovery, read-only)
  * one junk-key status query per protocol version to learn how the gateway reacts to an
    unauthenticated client. The key is deliberately wrong, so it cannot alter anything.
"""
from __future__ import annotations

import asyncio
import socket
import time

PORTS = [21, 22, 23, 53, 80, 81, 443, 554, 1883, 5353, 5683, 6666, 6667, 6668, 6669, 7000, 7001,
         8000, 8080, 8081, 8443, 8883, 9000, 10000, 49152, 49153, 55443]


async def _tcp(ip: str, port: int, timeout: float) -> dict | None:
    t0 = time.perf_counter()
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        banner = b""
        try:
            banner = await asyncio.wait_for(r.read(64), 0.4)  # don't send anything; just see if it talks first
        except Exception:
            pass
        w.close()
        return {"port": port, "ms": round((time.perf_counter() - t0) * 1000), "banner": banner.hex()}
    except Exception:
        return None


async def port_scan(ip: str) -> list[dict]:
    sem = asyncio.Semaphore(6)

    async def g(p):
        async with sem:
            return await _tcp(ip, p, 3.0)

    return [r for r in await asyncio.gather(*(g(p) for p in PORTS)) if r]


def multicast_queries(seconds: float = 4.0, ip_filter: str | None = None) -> dict:
    """mDNS (5353) service-enumeration + SSDP M-SEARCH; report anything answered by ip_filter."""
    out = {"mdns": [], "ssdp": []}
    # mDNS: PTR query for _services._dns-sd._udp.local
    q = (b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
         b"\x09_services\x07_dns-sd\x04_udp\x05local\x00\x00\x0c\x00\x01")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    try:
        s.sendto(q, ("224.0.0.251", 5353))
        s.sendto(b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n",
                 ("239.255.255.250", 1900))
        end = time.time() + seconds
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            if ip_filter is None or addr[0] == ip_filter:
                out["mdns" if addr[1] == 5353 else "ssdp"].append({"from": addr[0], "bytes": len(data), "head": data[:80].hex()})
    finally:
        s.close()
    return out


def junk_key_probe(ip: str, dev_id: str) -> dict:
    """Ask for status with a wrong key. Shows whether/how the gateway demands authentication."""
    import tinytuya

    res = {}
    for v in (3.3, 3.4, 3.5):
        d = tinytuya.Device(dev_id, address=ip, local_key="0123456789abcdef", version=v,
                            connection_timeout=4, connection_retry_limit=1)
        t0 = time.perf_counter()
        try:
            r = d.status()
        except Exception as e:  # noqa: BLE001
            r = {"exception": f"{type(e).__name__}: {e}"}
        res[str(v)] = {"ms": round((time.perf_counter() - t0) * 1000), "response": r}
        d.close()
        time.sleep(1.0)
    return res


def full_udp_payload(seconds: int = 12) -> list[dict]:
    from tinytuya import scanner

    found = scanner.devices(verbose=False, scantime=seconds, color=False, poll=False,
                            forcescan=False, assume_yes=True)
    return list((found or {}).values())
