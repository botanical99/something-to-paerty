"""FastAPI app: REST + WebSocket + static mobile UI. No Tuya secrets ever leave the backend."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.controller import LightingController
from core.effects import DEFAULT_PARAMS, EFFECTS
from core.model import Layout
from core.scenes import SceneStore
from core.scheduler import Scheduler
from hardware.gateway_link import AsyncGatewayLink

log = logging.getLogger("api")
WEB = Path(__file__).resolve().parent.parent / "web"


class Runtime:
    """Everything that lives for the lifetime of the server."""

    def __init__(self):
        self.layout = Layout.load()
        self.link = AsyncGatewayLink(self.layout.gateway_ip, self.layout.gateway_version)
        self.sched = Scheduler(self.layout, self.link, rate=4.0)
        self.scenes = SceneStore()
        self.ctl = LightingController(self.layout, self.sched, self.scenes)
        self.audio = None          # AudioEngine (optional import)
        self.clients: set[WebSocket] = set()
        self._pusher: asyncio.Task | None = None
        self._last_json = ""
        self.started = time.time()
        self._load_music()

    def _load_music(self) -> None:
        try:
            from audio.director import MusicDirector
            self.ctl.music = MusicDirector(self.layout, self.sched, self.ctl)
        except Exception as e:  # noqa: BLE001
            log.warning("music mode unavailable: %s", e)

    async def start(self) -> None:
        self.link.on_online = self.ctl.on_gateway_online
        self.link.start()
        self.sched.start()
        await self.ctl.start()
        self._pusher = asyncio.get_running_loop().create_task(self._push_loop(), name="ws-pusher")
        self.ctl.listeners.append(lambda: None)

    async def shutdown(self) -> None:
        log.info("shutting down: restoring the room")
        if self._pusher:
            self._pusher.cancel()
        await self.ctl.shutdown()
        await self.sched.stop()
        await self.link.close()

    # ---- state
    def full_state(self) -> dict:
        s = self.ctl.state()
        st = self.sched.stats.public()
        st["queue"] = len(self.sched.pending)
        s["gateway"] = {"state": self.link.state, "reconnects": self.link.reconnects, "error": self.link.last_error,
                        "ip": self.link.ip, "stats": st, "rate_limit": self.sched.rate}
        s["music"] = self.ctl.music.public() if self.ctl.music else {"available": False}
        s["uptime"] = round(time.time() - self.started)
        return s

    async def _push_loop(self) -> None:
        while True:
            await asyncio.sleep(0.15)
            if not self.clients:
                continue
            msg = json.dumps({"type": "state", **self.full_state()}, default=str)
            if msg != self._last_json:
                self._last_json = msg
                await self.broadcast(msg)

    async def broadcast(self, msg: str) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


rt: Runtime | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global rt
    rt = Runtime()
    await rt.start()
    yield
    await rt.shutdown()


app = FastAPI(title="Room lights", lifespan=lifespan)


def R() -> Runtime:
    assert rt is not None
    return rt


# ------------------------------------------------------------------ read
@app.get("/api/meta")
def meta():
    r = R()
    return {
        "layout": r.layout.to_public(),
        "effects": [{"name": e.name, "label": e.label, "description": e.description, "cmds_per_step": e.cmds_per_step,
                     "defaults": {**DEFAULT_PARAMS, **e.defaults}} for e in EFFECTS.values()],
        "scenes": r.scenes.public(),
        "defaults": DEFAULT_PARAMS,
        "music_available": r.ctl.music is not None,
    }


@app.get("/api/state")
def state():
    return R().full_state()


# ------------------------------------------------------------------ control
@app.post("/api/normal")
async def normal():
    await R().ctl.normal()
    return {"ok": True}


@app.post("/api/scene/{name}")
async def scene(name: str):
    r = R()
    if name not in r.scenes.scenes:
        raise HTTPException(404, "unknown scene")
    try:
        await r.ctl.apply_scene(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/stop")
async def stop(body: dict = Body(default={})):
    await R().ctl.stop(restore=bool(body.get("restore", True)))
    return {"ok": True}


@app.post("/api/effect/start")
async def effect_start(body: dict = Body(...)):
    try:
        await R().ctl.start_effect(body["name"], body.get("params") or {})
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/params")
def params(body: dict = Body(...)):
    R().ctl.update_params(body)
    return {"ok": True}


@app.post("/api/master")
def master(body: dict = Body(...)):
    R().ctl.set_master(body.get("brightness"), body.get("cct"))
    return {"ok": True}


@app.post("/api/fixture/{fid}")
async def fixture(fid: str, body: dict = Body(...)):
    try:
        await R().ctl.set_fixture(fid, body.get("level"), body.get("cct"))
    except KeyError:
        raise HTTPException(404, "unknown fixture")
    return {"ok": True}


# ------------------------------------------------------------------ music
@app.get("/api/audio/devices")
def audio_devices():
    m = R().ctl.music
    return m.devices() if m else []


@app.post("/api/music/start")
async def music_start(body: dict = Body(default={})):
    try:
        await R().ctl.start_music(body.get("params") or {})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/music/calibrate")
async def music_calibrate(body: dict = Body(default={})):
    m = R().ctl.music
    if not m:
        raise HTTPException(409, "music unavailable")
    asyncio.get_running_loop().create_task(m.calibrate(float(body.get("seconds", 10)), body.get("device")))
    return {"ok": True}


# ------------------------------------------------------------------ scenes
@app.put("/api/scenes/{name}")
def scene_put(name: str, body: dict = Body(...)):
    r = R()
    r.scenes.put(name, body)
    return {"ok": True}


@app.post("/api/scenes/{name}/save-current")
def scene_save_current(name: str):
    R().ctl.save_current_as(name)
    return {"ok": True}


@app.post("/api/scenes/{name}/reset")
def scene_reset(name: str):
    R().scenes.reset(name)
    return {"ok": True}


# ------------------------------------------------------------------ websocket + static
@app.websocket("/ws")
async def ws(sock: WebSocket):
    r = R()
    await sock.accept()
    r.clients.add(sock)
    try:
        await sock.send_text(json.dumps({"type": "state", **r.full_state()}, default=str))
        while True:
            await sock.receive_text()       # client pings; commands use REST
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        r.clients.discard(sock)


@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=WEB), name="static")
