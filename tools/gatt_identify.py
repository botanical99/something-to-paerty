"""Read ONLY the standard Generic Access characteristics of Tuya BLE devices (no writes, no pairing).

    python tools/gatt_identify.py ADDR [ADDR ...]
"""
import asyncio
import sys

from bleak import BleakClient

READ = {
    "device_name(2A00)": "00002a00-0000-1000-8000-00805f9b34fb",
    "appearance(2A01)": "00002a01-0000-1000-8000-00805f9b34fb",
}


async def one(addr):
    try:
        async with BleakClient(addr, timeout=15) as c:
            out = {}
            for label, uuid in READ.items():
                try:
                    out[label] = bytes(await c.read_gatt_char(uuid)).hex()
                except Exception as e:  # noqa: BLE001
                    out[label] = f"ERR {type(e).__name__}"
            print(addr, out)
    except Exception as e:  # noqa: BLE001
        print(addr, "connect failed:", type(e).__name__, e)


async def main():
    for a in sys.argv[1:]:
        await one(a)
        await asyncio.sleep(2)  # polite gap between connections


asyncio.run(main())
