"""Raw AD-type histogram straight from WinRT (listen only).

Bleak hides AD types such as 0x29 (PB-ADV), 0x2A (Mesh Message), 0x2B (Mesh Beacon).
This shows every AD type each address transmits so we can tell SIG Mesh from plain Tuya BLE.

    python tools/ble_raw_ad.py [seconds]
"""
import sys
import threading
import time
from collections import defaultdict

from winrt.windows.devices.bluetooth.advertisement import (
    BluetoothLEAdvertisementWatcher, BluetoothLEScanningMode)
from winrt.windows.storage.streams import DataReader

NAMES = {0x01: "flags", 0x02: "uuid16-part", 0x03: "uuid16-all", 0x07: "uuid128-all", 0x08: "short-name",
         0x09: "name", 0x0A: "txpower", 0x16: "svc-data16", 0x19: "appearance", 0x29: "MESH-PB-ADV",
         0x2A: "MESH-MESSAGE", 0x2B: "MESH-BEACON", 0xFF: "mfr-data"}

seen = defaultdict(lambda: defaultdict(int))
samples = {}


def on_rx(sender, args):
    addr = ":".join(f"{(args.bluetooth_address >> s) & 0xFF:02X}" for s in range(40, -8, -8))
    for ds in args.advertisement.data_sections:
        buf = ds.data
        b = bytearray(buf.length)
        DataReader.from_buffer(buf).read_bytes(b)
        seen[addr][ds.data_type] += 1
        if ds.data_type in (0x29, 0x2A, 0x2B) or (ds.data_type == 0x16 and bytes(b[:2]) in (b"\x27\x18", b"\x28\x18")):
            samples.setdefault((addr, ds.data_type), bytes(b).hex())


w = BluetoothLEAdvertisementWatcher()
w.scanning_mode = BluetoothLEScanningMode.ACTIVE
w.add_received(on_rx)
w.start()
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 40)
w.stop()

mesh_types = {0x29, 0x2A, 0x2B}
print(f"{len(seen)} addresses seen")
mesh_hits = {a: t for a, t in seen.items() if mesh_types & set(t)}
print("Addresses sending Bluetooth-Mesh AD types (0x29/0x2A/0x2B):", mesh_hits and {a: [hex(x) for x in t] for a, t in mesh_hits.items()} or "NONE")
print("Mesh service-data samples (0x1827/0x1828):", {k: v for k, v in samples.items() if k[1] == 0x16} or "NONE")
print("\nAD types per address that also carry Tuya (0x07D0) data:")
for addr, t in seen.items():
    if 0xFF in t:
        print(" ", addr, {NAMES.get(k, hex(k)): v for k, v in sorted(t.items())})
