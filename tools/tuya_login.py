"""One-time Smart Life / Tuya Smart QR login (no developer account), then enumerate devices.

    python tools/tuya_login.py --user-code ABC123XYZ     # first time: shows a QR, you scan + confirm
    python tools/tuya_login.py --refresh                 # later: reuse the saved session, re-download devices

Uses Tuya's official device-sharing SDK (the same login Home Assistant uses).
Writes (all git-ignored):
    config/tuya_session.json   login token (revocable in the app)
    config/tuya_devices.json   full device list incl. local keys
Prints a numbered list with secrets masked.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hardware.redact import mask  # noqa: E402

CLIENT_ID = "HA_3y9q4ak7g4ephrvke"   # Home Assistant's public sharing-app id (same as the official HA integration)
SCHEMA = "haauthorize"
SESSION = ROOT / "config" / "tuya_session.json"
DEVICES = ROOT / "config" / "tuya_devices.json"
QR_PNG = ROOT / "reports" / "raw" / "login_qr.png"
POLL_SECONDS = 150


def jsonable(o):
    if isinstance(o, SimpleNamespace):
        return {k: jsonable(v) for k, v in vars(o).items()}
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [jsonable(v) for v in o]
    return o


def do_login(user_code: str) -> dict:
    import qrcode
    from tuya_sharing import LoginControl

    lc = LoginControl()
    resp = lc.qr_code(CLIENT_ID, SCHEMA, user_code)
    if not resp.get("success"):
        sys.exit(f"Tuya rejected the User Code (is it correct / same region account?): {resp.get('msg') or resp.get('code')}")
    token = resp["result"]["qrcode"]

    img = qrcode.make(f"tuyaSmart--qrLogin?token={token}", box_size=12, border=4)
    QR_PNG.parent.mkdir(parents=True, exist_ok=True)
    img.save(QR_PNG)
    print(f"QR_READY {QR_PNG}", flush=True)
    try:
        os.startfile(QR_PNG)  # pop the image up on the desktop
    except Exception:  # noqa: BLE001
        pass
    print(f"Waiting up to {POLL_SECONDS}s: in the app tap  +  ->  Scan , scan the QR, then tap Confirm login.", flush=True)

    deadline = time.time() + POLL_SECONDS
    while time.time() < deadline:
        ok, info = lc.login_result(token, CLIENT_ID, user_code)
        if ok:
            session = {
                "user_code": user_code,
                "terminal_id": info["terminal_id"],
                "endpoint": info["endpoint"],
                "token_info": {k: info[k] for k in ("t", "uid", "expire_time", "access_token", "refresh_token")},
            }
            SESSION.parent.mkdir(exist_ok=True)
            SESSION.write_text(json.dumps(session, indent=2), encoding="utf-8")
            QR_PNG.unlink(missing_ok=True)  # token in the QR is now spent
            print("LOGIN_OK", flush=True)
            return session
        time.sleep(2)
    QR_PNG.unlink(missing_ok=True)
    sys.exit("LOGIN_TIMEOUT: QR was not approved in time. Re-run to get a fresh QR.")


def load_manager(session: dict):
    from tuya_sharing import Manager, SharingTokenListener

    class Listener(SharingTokenListener):
        def update_token(self, token_info):  # keep the refreshed token on disk
            session["token_info"] = {k: token_info.get(k) for k in ("t", "uid", "expire_time", "access_token", "refresh_token")}
            SESSION.write_text(json.dumps(session, indent=2), encoding="utf-8")

    m = Manager(CLIENT_ID, session["user_code"], session["terminal_id"], session["endpoint"],
                session["token_info"], Listener())
    m.update_device_cache()
    return m


def capabilities(d) -> list[str]:
    codes = set(getattr(d, "status", {}) or {}) | set(getattr(d, "function", {}) or {})
    cap = []
    if codes & {"switch_led", "switch_1", "switch"}:
        cap.append("on/off")
    if codes & {"bright_value", "bright_value_v2", "bright_value_1"}:
        cap.append("brightness")
    if codes & {"temp_value", "temp_value_v2"}:
        cap.append("colour-temp")
    if codes & {"colour_data", "colour_data_v2"}:
        cap.append("RGB")
    return cap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-code")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    if a.refresh and SESSION.exists():
        session = json.loads(SESSION.read_text(encoding="utf-8"))
    elif a.user_code:
        session = do_login(a.user_code.strip())
    else:
        sys.exit("Need --user-code (first time) or --refresh (saved session).")

    mgr = load_manager(session)
    devs = list(mgr.device_map.values())
    DEVICES.write_text(json.dumps([jsonable(d) for d in devs], indent=2, default=str), encoding="utf-8")
    print(f"\n{len(devs)} device(s) in the account. Full details (incl. keys) saved to config/tuya_devices.json (git-ignored).\n")

    for i, d in enumerate(devs, 1):
        print(f"Device {i} — {d.name!r}  [{d.product_name}]")
        print(f"    category={d.category} product_id={d.product_id} sub_device={d.sub} online={d.online} ip={getattr(d, 'ip', '')}")
        print(f"    id={d.id}  uuid={d.uuid}  local_key={mask(getattr(d, 'local_key', ''))}")
        print(f"    capabilities: {', '.join(capabilities(d)) or '—'}   status codes: {sorted((d.status or {}).keys())}")
    extra = sorted({k for d in devs for k in vars(d)} - {"id", "name", "local_key", "category", "product_id", "product_name",
                                                         "sub", "uuid", "asset_id", "online", "icon", "ip", "time_zone",
                                                         "active_time", "create_time", "update_time", "set_up",
                                                         "support_local", "local_strategy", "status", "function", "status_range"})
    if extra:
        print("\nadditional fields the API returned:", extra)


if __name__ == "__main__":
    main()
