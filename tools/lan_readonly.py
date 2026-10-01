"""READ-ONLY: talk to the gateway over the LAN with the account's key and read every child's state.
No control commands are sent (sub-device list / status queries only). Always closes its socket.

    python tools/lan_readonly.py [gateway_ip] [version]
"""
import json
import sys
import time
from pathlib import Path

import tinytuya

ROOT = Path(__file__).resolve().parent.parent
devs = json.loads((ROOT / "config" / "tuya_devices.json").read_text(encoding="utf-8"))
gw = next(d for d in devs if not d["sub"])
kids = [d for d in devs if d["sub"]]
ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.100.171"
ver = float(sys.argv[2]) if len(sys.argv) > 2 else 3.3
dump = lambda o: json.dumps(o, default=lambda x: "<obj>")[:300]  # noqa: E731

g = tinytuya.Device(gw["id"], address=ip, local_key=gw["local_key"], version=ver, persist=True,
                    connection_timeout=5, connection_retry_limit=1)
try:
    ok = False
    for attempt in range(1, 5):
        t = time.perf_counter()
        sq = g.subdev_query()
        ms = (time.perf_counter() - t) * 1e3
        ok = isinstance(sq, dict) and "Error" not in sq
        print(f"attempt {attempt}: subdev_query ok={ok} {ms:.0f} ms resp={dump(sq)}", flush=True)
        if ok:
            break
        g.close()
        time.sleep(8)
    if ok:
        for k in kids:
            c = tinytuya.Device(k["id"], cid=k["node_id"], parent=g, version=ver)
            t = time.perf_counter()
            r = c.status()
            ms = (time.perf_counter() - t) * 1e3
            print(f"  {k['name']!r:9} cid={k['node_id']}  {ms:.0f} ms  -> {dump(r)}", flush=True)
            time.sleep(0.5)
finally:
    g.close()
