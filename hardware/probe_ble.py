"""BLE discovery from the laptop's own Bluetooth adapter (via bleak).

Default behaviour is advertisement-listening only: nothing is connected to, nothing
is written. `gatt_probe()` (opt-in, read-only) connects to ONE address and lists
its GATT services, then disconnects.

Classification is heuristic and labelled as such in the report.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from bleak import BleakScanner


def _u16(n: int) -> str:
    return f"0000{n:04x}-0000-1000-8000-00805f9b34fb"


MESH_PROVISIONING = _u16(0x1827)   # unprovisioned node, GATT bearer
MESH_PROXY = _u16(0x1828)          # provisioned node, GATT proxy bearer
TUYA_SERVICE_HINTS = {_u16(0xA201): "Tuya BLE service 0xA201", _u16(0x1910): "Tuya BLE GATT service 0x1910"}
TELINK_MESH_SERVICE = "00010203-0405-0607-0809-0a0b0c0d1910"
TUYA_COMPANY_ID = 0x07D0  # tentative: Tuya company ID seen in Tuya BLE advertisements
TELINK_COMPANY_ID = 0x0211
NAME_HINTS = ("tuya", "smart", "out_of_mesh", "telink", "mesh", "track", "magnetic", "light", "lamp", "led")


@dataclass
class BleSeen:
    address: str
    name: str | None = None
    rssi_max: int = -999
    adv_count: int = 0
    manufacturer_data: dict[str, str] = field(default_factory=dict)  # "0xCCCC" -> hex
    service_uuids: set[str] = field(default_factory=set)
    service_data: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "address": self.address, "name": self.name, "rssi_max": self.rssi_max,
            "adv_count": self.adv_count, "manufacturer_data": self.manufacturer_data,
            "service_uuids": sorted(self.service_uuids), "service_data": self.service_data,
            "tags": self.tags,
        }


def _classify(d: BleSeen) -> None:
    tags = []
    if MESH_PROXY in d.service_uuids or MESH_PROXY in d.service_data:
        tags.append("SIG-MESH-PROXY: provisioned Bluetooth Mesh node reachable via GATT proxy")
    if MESH_PROVISIONING in d.service_uuids or MESH_PROVISIONING in d.service_data:
        tags.append("SIG-MESH-UNPROVISIONED: advertising for provisioning (not yet in a network)")
    for uuid, label in TUYA_SERVICE_HINTS.items():
        if uuid in d.service_uuids or uuid in d.service_data:
            tags.append(f"TUYA-BLE?: {label}")
    if TELINK_MESH_SERVICE in d.service_uuids:
        tags.append("TELINK-MESH: legacy Telink/Tuya proprietary mesh service")
    if f"0x{TUYA_COMPANY_ID:04X}" in d.manufacturer_data:
        tags.append("TUYA-BLE?: manufacturer id 0x07D0")
    if f"0x{TELINK_COMPANY_ID:04X}" in d.manufacturer_data:
        tags.append("TELINK-MESH?: manufacturer id 0x0211")
    if d.name and any(h in d.name.lower() for h in NAME_HINTS):
        tags.append(f"name-hint: {d.name!r}")
    d.tags = tags


async def scan(seconds: float = 20.0) -> list[BleSeen]:
    seen: dict[str, BleSeen] = {}

    def on_adv(device, adv):
        s = seen.setdefault(device.address, BleSeen(address=device.address))
        s.adv_count += 1
        s.name = adv.local_name or device.name or s.name
        s.rssi_max = max(s.rssi_max, adv.rssi if adv.rssi is not None else -999)
        for cid, data in (adv.manufacturer_data or {}).items():
            s.manufacturer_data[f"0x{cid:04X}"] = data.hex()
        for u in adv.service_uuids or []:
            s.service_uuids.add(u.lower())
        for u, data in (adv.service_data or {}).items():
            s.service_data[u.lower()] = data.hex()

    async with BleakScanner(detection_callback=on_adv):
        await asyncio.sleep(seconds)

    for d in seen.values():
        _classify(d)
    return sorted(seen.values(), key=lambda d: d.rssi_max, reverse=True)


async def gatt_probe(address: str, timeout: float = 15.0) -> dict:
    """OPT-IN: connect, enumerate services/characteristics, disconnect. Writes nothing."""
    from bleak import BleakClient

    result: dict = {"address": address, "services": [], "error": None}
    try:
        async with BleakClient(address, timeout=timeout) as client:
            for svc in client.services:
                result["services"].append({
                    "uuid": svc.uuid,
                    "characteristics": [{"uuid": c.uuid, "properties": list(c.properties)} for c in svc.characteristics],
                })
    except Exception as e:  # noqa: BLE001 - diagnostic tool, report everything
        result["error"] = f"{type(e).__name__}: {e}"
    return result
