"""Start the local lighting controller.   python run.py   (or double-click START_LIGHTS.bat)"""
import asyncio
import ctypes
import logging
import logging.handlers
import os
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

PORT = int(os.getenv("LIGHTS_PORT", "8080"))


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(ROOT / "logs" / "lights.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    logging.basicConfig(level=logging.INFO, handlers=[fh, ch])
    for noisy in ("uvicorn.access", "tinytuya", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def banner(url: str) -> None:
    print("\n" + "=" * 62)
    print("  ROOM LIGHTS controller")
    print(f"  This laptop :  http://localhost:{PORT}")
    print(f"  iPhone/iPad :  {url}      (same Wi-Fi)")
    print("=" * 62)
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:  # noqa: BLE001
        pass
    print("  Ctrl+C here to stop. The room is restored on exit.\n")


def install_close_handler(loop_holder: dict) -> None:
    """Best effort: closing the console window should still restore the room."""
    if os.name != "nt":
        return
    HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    @HANDLER
    def handler(event):  # CTRL_CLOSE_EVENT=2, LOGOFF=5, SHUTDOWN=6
        if event in (2, 5, 6):
            try:
                from api import server
                loop = loop_holder.get("loop")
                if loop and server.rt:
                    asyncio.run_coroutine_threadsafe(server.rt.shutdown(), loop).result(timeout=4)
            except Exception:  # noqa: BLE001
                pass
        return 0

    loop_holder["h"] = handler  # keep a reference
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)


def main() -> None:
    setup_logging()
    import uvicorn

    url = f"http://{lan_ip()}:{PORT}"
    banner(url)
    holder: dict = {}
    install_close_handler(holder)

    config = uvicorn.Config("api.server:app", host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)

    async def serve():
        holder["loop"] = asyncio.get_running_loop()
        if os.getenv("LIGHTS_NO_BROWSER") != "1":
            threading.Timer(2.0, lambda: os.startfile(f"http://localhost:{PORT}")).start()
        await server.serve()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
