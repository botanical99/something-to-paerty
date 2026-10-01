# Local party / music controller for Tuya track lights

Status: **hardware control verified** (6 lights individually controllable over the LAN). Effects engine, music mode and web UI are not built yet. See [HARDWARE_DISCOVERY.md](HARDWARE_DISCOVERY.md) for exactly what is verified vs assumed.

```
hardware/   tuya_lan.py (gateway LAN adapter, works) · tuya_ble_direct.py (BLE client, gateway only) · discovery probes
core/       fixture model, room topology, effects engine   (next)
audio/      music analysis, beat detection                 (later)
api/        FastAPI                                        (later)
web/        mobile-first UI                                (later)
config/     local, git-ignored: tuya_session.json, tuya_devices.json (keys!), tuya_fixtures.json
tools/      login, identify, benchmarks, diagnostics
```

## Setup (once)
```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe tools\tuya_login.py --user-code <code from the app>   # shows a QR; scan + confirm in Smart Life
```
User Code: app → Me → ⚙ Settings → Account and Security → User Code. No Tuya developer account needed.
Later: `tools\tuya_login.py --refresh` re-downloads the device list with the saved session.

## Useful commands
```
.venv\Scripts\python.exe discover.py                       # read-only LAN + BLE discovery
.venv\Scripts\python.exe tools\identify.py <cid> <ip> 6    # blink one lamp for 6 s, restore it
.venv\Scripts\python.exe tools\lan_bench.py latency        # measure (also: rate, sequence, persist)
```

## Secrets
`.env`, `devices.json`, `config/tuya_*.json`, `config/ble_map.json` are git-ignored. Output masks keys. Nothing secret is ever served to a frontend.
