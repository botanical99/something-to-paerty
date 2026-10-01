"""Zero-credential interrogation of the WG-S gateway (read-only). See hardware/probe_gateway.py.

    python tools/gateway_interrogate.py 192.168.100.171
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hardware import probe_gateway as pg  # noqa: E402

ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.100.171"

print("== full UDP discovery payload (12s) ==")
udp = pg.full_udp_payload(12)
print(json.dumps(udp, indent=1, default=str))
dev_id = next((d.get("gwId") for d in udp if d.get("ip") == ip), None)

print("\n== TCP port scan (27 ports, 6 at a time, connect only) ==")
print(json.dumps(asyncio.run(pg.port_scan(ip)), indent=1))

print("\n== mDNS / SSDP answers from the gateway (4s) ==")
print(json.dumps(pg.multicast_queries(4, ip), indent=1))

if dev_id:
    print("\n== unauthenticated (junk-key) status probe, v3.3/3.4/3.5 ==")
    print(json.dumps(pg.junk_key_probe(ip, dev_id), indent=1, default=str))
