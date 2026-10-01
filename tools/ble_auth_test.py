"""Direct-BLE test of hardware/tuya_ble_direct.py against a device matched in config/ble_map.json.
Only: connect -> device-info -> authenticate -> request state -> disconnect. No DP writes.

    python tools/ble_auth_test.py [device_id]      # default: the gateway
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hardware.tuya_ble_direct import TuyaBleClient  # noqa: E402


async def main():
    devs = json.loads((ROOT / "config" / "tuya_devices.json").read_text(encoding="utf-8"))
    bmap = json.loads((ROOT / "config" / "ble_map.json").read_text(encoding="utf-8"))
    want = sys.argv[1] if len(sys.argv) > 1 else next(d["id"] for d in devs if not d["sub"])
    addr = next((a for a, m in bmap.items() if m["device_id"] == want), None)
    if not addr:
        sys.exit("that device has no BLE address in config/ble_map.json (it is not advertising over BLE)")
    d = next(x for x in devs if x["id"] == want)
    c = TuyaBleClient(addr, d["uuid"], d["local_key"], d["id"], protocol_version=bmap[addr]["protocol"], is_bound=bmap[addr]["bound"])
    try:
        await c.connect()
        print("AUTH OK", {k: round(v) for k, v in c.timings.items()}, getattr(c, "info", None))
        t = time.perf_counter()
        st = await c.read_state()
        print(f"state read in {(time.perf_counter() - t) * 1e3:.0f} ms: {st}")
    except Exception as e:  # noqa: BLE001
        print("FAILED:", type(e).__name__, e)
    finally:
        await c.disconnect()


asyncio.run(main())
