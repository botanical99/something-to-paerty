"""Start the local lighting controller.

    python run.py                 real lights (needs config/tuya_devices.json + config/tuya_fixtures.json)
    python run.py --simulate      simulated gateway + lamps - safe anywhere, nothing touches the network

Double-click START_LIGHTS.bat on Windows. Ctrl+C (or STOP_LIGHTS.bat) stops it and restores the room.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.net import lan_ip  # noqa: E402
from core.settings import Settings  # noqa: E402


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(log_dir / "lights.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    logging.basicConfig(level=logging.INFO, handlers=[fh, ch], force=True)
    for noisy in ("uvicorn.access", "tinytuya", "asyncio", "multipart", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def banner(s: Settings, pin: str | None) -> None:
    ip = lan_ip()
    url = f"http://{ip}:{s.port}"
    pair = f"{url}/pair?code={pin}" if (pin and s.require_pin) else url
    print("\n" + "=" * 64)
    print("  ROOM LIGHTS controller" + ("   *** SIMULATOR - not real lights ***" if s.simulate else ""))
    print(f"  This laptop :  http://localhost:{s.port}")
    print(f"  iPhone/iPad :  {url}     (same Wi-Fi)")
    if pin and s.require_pin:
        print(f"  Pairing PIN :  {pin}   (or just scan the QR code below)")
    print("=" * 64)
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(pair)
        qr.print_ascii(invert=True)
    except Exception:  # noqa: BLE001
        pass
    print("  Ctrl+C here (or STOP_LIGHTS.bat) stops it. The room is restored on exit.\n")


def install_close_handler(holder: dict) -> None:
    """Windows: closing the console window should still restore the room."""
    if os.name != "nt":
        return
    handler_t = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)  # type: ignore[attr-defined]

    @handler_t
    def handler(event):  # CTRL_CLOSE_EVENT=2, LOGOFF=5, SHUTDOWN=6
        if event in (2, 5, 6):
            try:
                loop, app = holder.get("loop"), holder.get("app")
                rt = getattr(app.state, "rt", None) if app else None
                if loop and rt:
                    asyncio.run_coroutine_threadsafe(rt.shutdown(), loop).result(timeout=4)
            except Exception:  # noqa: BLE001
                pass
        return 0

    holder["h"] = handler  # keep a reference alive
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)  # type: ignore[attr-defined]


def already_running(port: int) -> bool:
    """A second START must not fight the first one for the gateway's single connection slot."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Room lights controller")
    ap.add_argument("--simulate", action="store_true", help="use the built-in simulator instead of the real gateway")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)

    settings = Settings.load(simulate=True if a.simulate else None, port=a.port,
                             open_browser=False if a.no_browser else None)
    if already_running(settings.port):
        print(f"\n  The controller is already running on port {settings.port}  ->  http://localhost:{settings.port}\n")
        if settings.open_browser and hasattr(os, "startfile"):
            os.startfile(f"http://localhost:{settings.port}/connect")  # type: ignore[attr-defined]
        return
    setup_logging(settings.log_dir)
    import uvicorn

    from api.server import create_app

    app = create_app(settings)
    banner(settings, app.state.auth.pin)
    holder: dict = {"app": app}
    install_close_handler(holder)

    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning"))

    async def serve() -> None:
        holder["loop"] = asyncio.get_running_loop()

        async def wire_exit() -> None:
            for _ in range(100):                              # runtime exists once the lifespan has started
                rt = getattr(app.state, "rt", None)
                if rt is not None:
                    rt.on_exit_request = lambda: setattr(server, "should_exit", True)
                    return
                await asyncio.sleep(0.1)

        asyncio.get_running_loop().create_task(wire_exit())
        if settings.open_browser and os.getenv("LIGHTS_NO_BROWSER") != "1" and hasattr(os, "startfile"):
            threading.Timer(2.0, lambda: os.startfile(f"http://localhost:{settings.port}/connect")).start()  # type: ignore[attr-defined]
        await server.serve()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
