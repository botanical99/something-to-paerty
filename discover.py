"""Phase 1: non-destructive hardware/protocol discovery.

    python discover.py                 # everything
    python discover.py --skip-ble      # LAN + Tuya only
    python discover.py --ble-seconds 30
    python discover.py --gatt-probe AA:BB:CC:DD:EE:FF   # opt-in: connect + list GATT services

Nothing here sends a control command, writes to a device, re-pairs or resets anything.
Secrets (local keys) are masked in every printed/saved output.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from hardware import probe_ble, probe_env, probe_lan, probe_tuya
from hardware.redact import redact

ROOT = Path(__file__).resolve().parent


def h(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-ble", action="store_true")
    ap.add_argument("--skip-lan", action="store_true")
    ap.add_argument("--skip-tuya", action="store_true")
    ap.add_argument("--ble-seconds", type=float, default=20)
    ap.add_argument("--lan-seconds", type=int, default=20)
    ap.add_argument("--gatt-probe", metavar="ADDRESS", help="opt-in: connect to this BLE address and list GATT services")
    args = ap.parse_args()

    report: dict = {"generated": datetime.now().isoformat(timespec="seconds")}

    # ---- 1. environment -------------------------------------------------------
    h("1. LAPTOP ENVIRONMENT")
    env = probe_env.run()
    report["environment"] = env
    print(f"OS            : {env['os']}\nPython        : {env['python']}")
    print(f"Wi-Fi         : {env['wifi'] or 'n/a'}")
    print(f"LAN           : {env['network']}")
    if env["bluetooth_adapters"]:
        for a in env["bluetooth_adapters"]:
            print(f"BLE adapter   : {a['name']}  [{a['status']}]")
    else:
        print("BLE adapter   : NONE FOUND")

    # ---- 2. LAN / gateway -----------------------------------------------------
    lan_devices: list[dict] = []
    if not args.skip_lan:
        h(f"2. LAN: Tuya devices (UDP scan {args.lan_seconds}s + TCP/6668 sweep)")
        try:
            lan_devices = probe_lan.tinytuya_scan(args.lan_seconds)
        except Exception as e:  # noqa: BLE001
            print(f"tinytuya scan failed: {type(e).__name__}: {e}")
        ip = env["network"]["ip"]
        tcp_hosts = asyncio.run(probe_lan.tcp_sweep(ip, env["network"]["prefix"])) if ip else []
        arp = probe_lan.arp_table()
        for d in lan_devices:
            d["mac"] = d.get("mac") or arp.get(d["ip"])
        report["lan"] = {"udp_devices": lan_devices, "tcp_6668_hosts": tcp_hosts,
                         "arp": {k: arp[k] for k in tcp_hosts if k in arp}}
        if lan_devices:
            for d in lan_devices:
                print(f"Tuya device   : ip={d['ip']}  id={d['gwId']}  v{d['version']}  productKey={d['productKey']}  mac={d.get('mac')}")
        else:
            print("No Tuya UDP broadcasts seen.")
        extra = [x for x in tcp_hosts if x not in {d['ip'] for d in lan_devices}]
        for x in extra:
            print(f"TCP/6668 open : {x}  mac={arp.get(x)}  (Tuya local port, not seen on UDP)")
        if not lan_devices and not tcp_hosts:
            print("=> Gateway NOT found on this LAN (check same Wi-Fi/VLAN, client isolation).")

    # ---- 3. BLE -----------------------------------------------------------------
    ble_seen: list = []
    if not args.skip_ble:
        h(f"3. BLE: advertisements from the laptop adapter ({args.ble_seconds:.0f}s, listen only)")
        try:
            ble_seen = asyncio.run(probe_ble.scan(args.ble_seconds))
        except Exception as e:  # noqa: BLE001
            print(f"BLE scan failed: {type(e).__name__}: {e}")
        report["ble"] = {"devices": [d.as_dict() for d in ble_seen]}
        print(f"{len(ble_seen)} BLE devices seen. Suspected mesh/Tuya/lights (heuristic tags):")
        suspects = [d for d in ble_seen if d.tags]
        for d in suspects:
            print(f"  {d.address}  rssi={d.rssi_max:>4}  name={d.name!r}")
            for t in d.tags:
                print(f"      - {t}")
        if not suspects:
            print("  (none tagged). Full list is in the JSON report.")
        print("\nAll devices (strongest first):")
        for d in ble_seen[:40]:
            mf = ",".join(d.manufacturer_data) or "-"
            print(f"  {d.address}  {d.rssi_max:>4} dBm  {str(d.name or '?'):<28} mfr={mf}  svc={len(d.service_uuids)}")

    if args.gatt_probe:
        h(f"3b. GATT probe (opt-in, read-only) {args.gatt_probe}")
        res = asyncio.run(probe_ble.gatt_probe(args.gatt_probe))
        report["gatt_probe"] = res
        print(json.dumps(res, indent=2))

    # ---- 4. local Tuya gateway control path (read-only) -----------------------------
    if not args.skip_tuya:
        h("4. Tuya gateway local API (READ-ONLY status/latency)")
        creds = probe_tuya.load_credentials(lan_devices)
        print(f"credentials source: {creds['source'] or 'NONE'}")
        res = probe_tuya.run(creds)
        report["tuya_local"] = redact(res)
        if res.get("skipped"):
            print(f"SKIPPED: {res['skipped']}")
        else:
            print(f"protocol version: {res.get('protocol_version')}  attempts: {res.get('version_attempts')}")
            print(f"subdev_query    : {res.get('subdev_query')}")
            for c in res.get("children", []):
                print(f"  child {c['label']} cid={c['cid']} status_ok={c.get('status_ok')} dps={c.get('dps')} "
                      f"latency={c.get('status_latency_ms')}")
            if res.get("error"):
                print("ERROR:", res["error"])

    # ---- 5. verdict ---------------------------------------------------------------
    h("5. SUMMARY")
    t = report.get("tuya_local", {})
    summary = {
        "gateway_found_on_lan": bool(lan_devices),
        "ble_adapter_present": bool(env["bluetooth_adapters"]),
        "ble_suspected_devices": [d.address for d in ble_seen if d.tags],
        "local_gateway_api_works": bool(t.get("protocol_version")),
        "children_controllable_addressable": [c["cid"] for c in t.get("children", []) if c.get("status_ok")],
        "REAL_HARDWARE_CONTROL_VERIFIED": False,  # Phase 2 only; discovery never sends commands
    }
    report["summary"] = summary
    print(json.dumps(summary, indent=2))

    out = ROOT / "reports" / "raw" / f"discovery_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(redact(report), indent=2, default=str), encoding="utf-8")
    print(f"\nSaved (secrets masked): {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
