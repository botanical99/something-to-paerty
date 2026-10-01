"""Passive BLE watcher: do Tuya advertisements rotate addresses/payloads? (listen only)

    python tools/ble_watch.py [seconds]
"""
import asyncio
import sys
import time
from collections import defaultdict

from bleak import BleakScanner

TUYA = 0x07D0


async def main(seconds: float):
    t0 = time.time()
    seen = defaultdict(lambda: {"first": None, "last": None, "n": 0, "mfr": set(), "svc": set(), "rssi": []})

    def cb(dev, adv):
        if TUYA not in (adv.manufacturer_data or {}) and not any("a201" in u for u in (adv.service_uuids or [])):
            return
        e = seen[dev.address]
        now = time.time() - t0
        e["first"] = now if e["first"] is None else e["first"]
        e["last"] = now
        e["n"] += 1
        e["rssi"].append(adv.rssi)
        e["mfr"].add(adv.manufacturer_data.get(TUYA, b"").hex())
        for d in (adv.service_data or {}).values():
            e["svc"].add(d.hex())

    async with BleakScanner(detection_callback=cb):
        await asyncio.sleep(seconds)

    for addr, e in sorted(seen.items(), key=lambda kv: -max(kv[1]["rssi"])):
        print(f"{addr}  first={e['first']:.0f}s last={e['last']:.0f}s adverts={e['n']} "
              f"rssi(avg)={sum(e['rssi'])/len(e['rssi']):.0f}  distinct_mfr={len(e['mfr'])} distinct_svc={len(e['svc'])}")
        for m in sorted(e["mfr"])[:3]:
            print("    mfr", m)
        for s in sorted(e["svc"])[:3]:
            print("    svc", s)


asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 45))
