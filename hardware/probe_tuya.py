"""READ-ONLY local Tuya gateway probe.

Uses tinytuya to talk to the WG-S gateway over the LAN and asks it for:
  * its own status
  * its sub-device (child) list
  * the status (DPS) of every child it reports
It also times the round trips. It sends NO control commands (no set_value / turn_on /
turn_off), so nothing about the lights' state or pairing can change.

Credentials come from (in order): ./devices.json (python -m tinytuya wizard), then .env.
Secrets are never printed; see hardware.redact.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS_TO_TRY = [3.4, 3.5, 3.3]


def load_credentials(lan_devices: list[dict] | None = None) -> dict:
    """Return {'gateway': {...}|None, 'children': [...], 'source': str, 'notes': [...]}."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    notes: list[str] = []
    gateway = None
    children: list[dict] = []
    source = None

    dj = ROOT / "devices.json"
    if dj.exists():
        try:
            entries = json.loads(dj.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            entries = []
            notes.append(f"devices.json unreadable: {e}")
        parents = {e.get("parent") for e in entries if e.get("parent")}
        for e in entries:
            if e.get("id") in parents:
                gateway = e
        if gateway is None:
            for e in entries:
                if "gateway" in (e.get("name", "") + e.get("product_name", "")).lower():
                    gateway = e
        if gateway:
            children = [e for e in entries if e.get("parent") == gateway.get("id")]
            source = "devices.json"
        else:
            notes.append("devices.json present but no gateway (an entry with sub-devices) found")

    if gateway is None and os.getenv("TUYA_GATEWAY_ID") and os.getenv("TUYA_GATEWAY_LOCAL_KEY"):
        gateway = {
            "id": os.environ["TUYA_GATEWAY_ID"],
            "key": os.environ["TUYA_GATEWAY_LOCAL_KEY"],
            "ip": os.getenv("TUYA_GATEWAY_IP", ""),
            "version": os.getenv("TUYA_GATEWAY_VERSION", ""),
            "name": "gateway (from .env)",
        }
        source = ".env"

    if gateway is not None:
        # Fill in ip / version from the LAN scan if the credential file lacks them.
        for d in lan_devices or []:
            if d.get("gwId") == gateway.get("id"):
                gateway.setdefault("ip", d.get("ip"))
                if not gateway.get("ip"):
                    gateway["ip"] = d.get("ip")
                if not gateway.get("version"):
                    gateway["version"] = d.get("version")
    return {"gateway": gateway, "children": children, "source": source, "notes": notes}


def _ok(resp) -> bool:
    return isinstance(resp, dict) and "Error" not in resp and "Err" not in resp


def run(creds: dict, repeats: int = 5, interval: float = 1.0) -> dict:
    import tinytuya

    out: dict = {"attempted": False, "notes": list(creds.get("notes", []))}
    gw = creds.get("gateway")
    if not gw:
        out["skipped"] = "no credentials (devices.json or .env) — see HARDWARE_DISCOVERY.md 'What I need from you'"
        return out
    ip = gw.get("ip")
    if not ip:
        out["skipped"] = "gateway IP unknown (not in credentials and not seen by LAN scan)"
        return out

    out["attempted"] = True
    out["gateway_id"] = gw.get("id")
    out["gateway_ip"] = ip
    versions = list(VERSIONS_TO_TRY)
    if gw.get("version"):  # announced/recorded version first, then the others
        first = float(gw["version"])
        versions = [first] + [v for v in versions if v != first]
    out["version_attempts"] = {}

    dev = None
    for v in versions:
        d = tinytuya.Device(gw["id"], address=ip, local_key=gw["key"], version=v,
                            persist=True, connection_timeout=5, connection_retry_limit=1)
        t0 = time.perf_counter()
        st = d.status()
        dt = (time.perf_counter() - t0) * 1000
        out["version_attempts"][str(v)] = {"ok": _ok(st), "ms": round(dt), "response": _short(st)}
        if _ok(st):
            dev, out["protocol_version"], out["gateway_status"] = d, v, st
            break
        d.close()
    if dev is None:
        out["error"] = "could not get a valid status from the gateway with any protocol version"
        return out

    # Sub-device list as reported by the gateway itself.
    try:
        sub = dev.subdev_query()
    except Exception as e:  # noqa: BLE001
        sub = {"Error": f"{type(e).__name__}: {e}"}
    out["subdev_query"] = sub

    child_ids: list[tuple[str, str]] = []  # (label, cid)
    for c in creds.get("children", []):
        child_ids.append((c.get("name", c.get("id")), c.get("node_id") or c.get("id")))
    if isinstance(sub, dict):
        known = {cid for _, cid in child_ids}
        for key in ("online", "offline"):
            for cid in (sub.get(key) or []):
                if cid not in known:
                    child_ids.append((f"({key}) {cid}", cid))

    out["children"] = []
    for label, cid in child_ids:
        entry = {"label": label, "cid": cid}
        child = tinytuya.Device(cid, cid=cid, parent=dev, version=out["protocol_version"])
        times = []
        for i in range(repeats):
            t0 = time.perf_counter()
            st = child.status()
            times.append((time.perf_counter() - t0) * 1000)
            if i == 0:
                entry["status"] = _short(st)
                entry["status_ok"] = _ok(st)
                if _ok(st):
                    entry["dps"] = st.get("dps")
            if not _ok(st) and i == 0:
                break  # no point timing a failing call
            if i < repeats - 1:
                time.sleep(interval)  # stay far below the gateway's safe rate
        if entry.get("status_ok"):
            entry["status_latency_ms"] = {
                "n": len(times), "min": round(min(times)), "median": round(statistics.median(times)),
                "max": round(max(times)),
            }
        out["children"].append(entry)
    dev.close()
    return out


def _short(resp):
    s = json.dumps(resp, default=str)
    return resp if len(s) < 600 else s[:600] + "…"
