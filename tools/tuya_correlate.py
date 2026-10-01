"""Match the Tuya BLE devices in the room to the account's devices (passive: scan + local crypto only).

The advertisement carries the device UUID encrypted with md5(<product id bytes>). For every BLE
advert we try every plausible key and keep the decryption that equals a UUID from the account.

    python tools/tuya_correlate.py [scan_seconds]
Writes config/ble_map.json (git-ignored): {ble_address: {"device_id":..., "name":..., ...}}
"""
import asyncio
import json
import sys
from pathlib import Path

from bleak import BleakScanner

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hardware.tuya_ble_direct import decode_advert, try_decrypt_uuid  # noqa: E402


async def main(seconds: float):
    devs = json.loads((ROOT / "config" / "tuya_devices.json").read_text(encoding="utf-8"))
    by_uuid = {d["uuid"]: d for d in devs if d.get("uuid")}
    seen = {}

    def cb(dev, adv):
        dec = decode_advert(adv)
        if dec:
            seen[dev.address] = (dec, adv.rssi)

    async with BleakScanner(detection_callback=cb):
        await asyncio.sleep(seconds)

    mapping, unmatched = {}, []
    for addr, (dec, rssi) in sorted(seen.items(), key=lambda kv: -kv[1][1]):
        keys = [dec["svc_payload"]] + [d["product_id"].encode() for d in devs if d.get("product_id")]
        hit = None
        for km, txt in try_decrypt_uuid(dec, keys):
            if txt in by_uuid:
                hit = (by_uuid[txt], km)
                break
        if hit:
            d, km = hit
            mapping[addr] = {"device_id": d["id"], "name": d["name"], "uuid": d["uuid"], "product_id": d["product_id"],
                             "category": d["category"], "rssi": rssi, "bound": dec["is_bound"], "protocol": dec["protocol"]}
            print(f"MATCH {addr}  rssi={rssi}  ->  {d['name']!r} [{d['product_name']}] id={d['id']}")
        else:
            unmatched.append(addr)
            print(f"no match  {addr}  rssi={rssi}  bound={dec['is_bound']} svc_type={dec['svc_type']}")
    (ROOT / "config" / "ble_map.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(f"\n{len(mapping)} matched, {len(unmatched)} unmatched. Saved config/ble_map.json")


asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 15))
