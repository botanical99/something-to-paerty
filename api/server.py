"""FastAPI app: REST + WebSocket + static mobile UI (PWA). No Tuya secrets ever leave the backend.

`create_app(settings)` builds an isolated app (own config dir, simulator or real gateway) so tests and
production share the same code.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.auth import COOKIE, Auth, AuthMiddleware
from core import logbuf
from core.controller import LightingController
from core.effects import DEFAULT_PARAMS, EFFECTS
from core.model import DP_BRIGHT, DP_CCT, DP_ON, Layout, raw_to_cct, raw_to_level
from core.net import lan_ip, qr_svg
from core.params import sanitize
from core.scenes import SceneStore
from core.scheduler import Scheduler
from core.settings import Settings

log = logging.getLogger("api")
WEB = Path(__file__).resolve().parent.parent / "web"
VERSION = "1.0.0"


class Runtime:
    """Everything that lives for the lifetime of the server."""

    def __init__(self, settings: Settings):
        self.settings = settings
        cdir = settings.config_dir
        self.layout = Layout.load(cdir, simulate=settings.simulate)
        self.sim = None
        if settings.simulate:
            from hardware.simulator import SimGateway, SimLink
            self.sim = SimGateway([f.cid for f in self.layout.fixtures.values()],
                                  latency_ms=settings.sim_latency_ms, realtime=settings.sim_realtime)
            self.link = SimLink(self.sim)
        else:
            from hardware.gateway_link import AsyncGatewayLink
            self.link = AsyncGatewayLink(self.layout.gateway_ip, self.layout.gateway_version)
        self.sched = Scheduler(self.layout, self.link, rate=settings.rate)
        self.scenes = SceneStore(cdir / "scenes.json")
        self.ctl = LightingController(self.layout, self.sched, self.scenes, state_file=cdir / "runtime_state.json")
        self.clients: set[WebSocket] = set()
        self._pusher: asyncio.Task | None = None
        self._last_json = ""
        self.started = time.time()
        self.exit_requested = asyncio.Event()
        self.on_exit_request = None            # run.py sets this to stop uvicorn
        self._load_music()

    def _load_music(self) -> None:
        try:
            from audio.director import MusicDirector
            from audio.sources import DEMO_ID, _sd
            device = DEMO_ID if (self.settings.simulate or _sd() is None) else "default"
            self.ctl.music = MusicDirector(self.layout, self.sched, self.ctl,
                                           calibration_file=self.settings.config_dir / "music_calibration.json",
                                           budget_rate=self.settings.music_budget, default_device=device)
        except Exception as e:  # noqa: BLE001
            log.warning("music mode unavailable: %s", e)

    async def start(self) -> None:
        self.link.on_online = self.ctl.on_gateway_online
        self.link.start()
        self.sched.start()
        await self.ctl.start()
        self._pusher = asyncio.get_running_loop().create_task(self._push_loop(), name="ws-pusher")
        log.info("controller ready (%s)", "SIMULATOR" if self.settings.simulate else "real gateway")

    async def shutdown(self) -> None:
        log.info("shutting down: restoring the room")
        if self._pusher:
            self._pusher.cancel()
        await self.ctl.shutdown()
        await self.sched.stop()
        await self.link.close()

    # ---- state
    def sim_state(self) -> dict | None:
        if not self.sim:
            return None
        truth = {}
        for fid, f in self.layout.fixtures.items():
            d = self.sim.lamps.get(f.cid, {})
            on = bool(d.get(DP_ON, True))
            truth[fid] = {"on": on, "level": raw_to_level(d.get(DP_BRIGHT, 10)) if on else 0.0,
                          "cct": raw_to_cct(d.get(DP_CCT, 500))}
        return {"fixtures": truth, **self.sim.stats()}

    def full_state(self) -> dict:
        s = self.ctl.state()
        st = self.sched.stats.public()
        st["queue"] = len(self.sched.pending)
        s["gateway"] = {"state": self.link.state, "reconnects": self.link.reconnects, "error": self.link.last_error,
                        "ip": self.link.ip, "stats": st, "rate_limit": self.sched.rate}
        s["music"] = self.ctl.music.public() if self.ctl.music else {"available": False}
        s["simulated"] = self.settings.simulate
        s["sim"] = self.sim_state()
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


def R(request: Request) -> Runtime:
    return request.app.state.rt


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    auth = Auth(settings.config_dir / "auth.json", require_pin=settings.require_pin, lan_only=settings.lan_only,
                trust_localhost=settings.trust_localhost)
    logbuf.install()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = Runtime(settings)
        app.state.rt = rt
        await rt.start()
        try:
            yield
        finally:
            await rt.shutdown()

    app = FastAPI(title="Room lights", version=VERSION, lifespan=lifespan)
    app.state.auth = auth
    app.state.settings = settings
    app.add_middleware(AuthMiddleware, auth=auth)

    # ------------------------------------------------------------------ auth / pairing
    def _set_cookie(resp):
        resp.set_cookie(COOKIE, auth.token(), max_age=180 * 86400, httponly=True, samesite="strict", path="/")
        return resp

    @app.get("/login", include_in_schema=False)
    def login_page():
        return FileResponse(WEB / "login.html", headers={"Cache-Control": "no-store"})

    @app.post("/api/login")
    def login(request: Request, body: dict = Body(...)):
        ip = request.client.host if request.client else "?"
        wait = auth.locked(ip)
        if wait > 0:
            return JSONResponse({"detail": f"too many attempts, wait {int(wait) + 1}s"}, status_code=429)
        if not auth.pin_ok(str(body.get("pin", "")).strip()):
            auth.fail(ip)
            return JSONResponse({"detail": "wrong PIN"}, status_code=401)
        auth.success(ip)
        return _set_cookie(JSONResponse({"ok": True}))

    @app.get("/pair", include_in_schema=False)
    def pair(request: Request, code: str = ""):
        ip = request.client.host if request.client else "?"
        if auth.locked(ip) > 0:
            return JSONResponse({"detail": "too many attempts"}, status_code=429)
        if not auth.pin_ok(code):
            auth.fail(ip)
            return RedirectResponse("/login?bad=1", status_code=302)
        auth.success(ip)
        return _set_cookie(RedirectResponse("/", status_code=302))

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.post("/api/auth/rotate")
    def rotate():
        pin, _ = auth.rotate()
        return {"ok": True, "pin": pin}

    @app.get("/api/connect-info")
    def connect_info(request: Request):
        r = R(request)
        ip = lan_ip()
        base = f"http://{ip}:{settings.port}"
        link = f"{base}/pair?code={auth.pin}" if settings.require_pin else base
        return {"url": base, "pair_url": link, "pin": auth.pin if settings.require_pin else None,
                "qr_svg": qr_svg(link), "simulated": r.settings.simulate, "require_pin": settings.require_pin}

    # ------------------------------------------------------------------ read
    @app.get("/api/meta")
    def meta(request: Request):
        r = R(request)
        return {
            "version": VERSION, "simulated": settings.simulate,
            "layout": r.layout.to_public(),
            "effects": [{"name": e.name, "label": e.label, "description": e.description,
                         "cmds_per_step": e.cmds_per_step, "defaults": {**DEFAULT_PARAMS, **e.defaults}}
                        for e in EFFECTS.values()],
            "scenes": r.scenes.public(), "scene_order": r.scenes.order(),
            "defaults": DEFAULT_PARAMS, "music_available": r.ctl.music is not None,
        }

    @app.get("/api/state")
    def state(request: Request):
        return R(request).full_state()

    @app.get("/api/logs")
    def logs(n: int = 200):
        return {"lines": logbuf.RING.tail(max(1, min(400, n)))}

    @app.get("/api/health")
    def health(request: Request):
        r = R(request)
        return {"ok": True, "gateway": r.link.state, "mode": r.ctl.mode, "simulated": settings.simulate}

    # ------------------------------------------------------------------ control
    @app.post("/api/normal")
    async def normal(request: Request):
        await R(request).ctl.normal()
        return {"ok": True}

    @app.post("/api/scene/{name}")
    async def scene(request: Request, name: str):
        r = R(request)
        if name not in r.scenes.scenes:
            raise HTTPException(404, "unknown scene")
        try:
            await r.ctl.apply_scene(name)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/stop")
    async def stop(request: Request, body: dict = Body(default={})):
        await R(request).ctl.stop(restore=bool(body.get("restore", True)))
        return {"ok": True}

    @app.post("/api/effect/start")
    async def effect_start(request: Request, body: dict = Body(...)):
        try:
            await R(request).ctl.start_effect(str(body.get("name", "")), body.get("params") or {})
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/params")
    def params(request: Request, body: dict = Body(...)):
        R(request).ctl.update_params(body)
        return {"ok": True}

    @app.post("/api/master")
    def master(request: Request, body: dict = Body(...)):
        b = body.get("brightness")
        c = body.get("cct")
        try:
            b = None if b is None else max(0.0, min(100.0, float(b)))
            c = None if c is None else max(0.0, min(1.0, float(c)))
        except (TypeError, ValueError):
            raise HTTPException(400, "brightness/cct must be numbers")
        R(request).ctl.set_master(b, c)
        return {"ok": True}

    @app.post("/api/fixture/{fid}")
    async def fixture(request: Request, fid: str, body: dict = Body(...)):
        def num(k, lo, hi):
            v = body.get(k)
            if v is None:
                return None
            try:
                return max(lo, min(hi, float(v)))
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} must be a number")
        on = body.get("on")
        try:
            await R(request).ctl.set_fixture(fid, num("level", 0, 100), num("cct", 0, 1),
                                             on if isinstance(on, bool) else None)
        except KeyError:
            raise HTTPException(404, "unknown fixture")
        return {"ok": True}

    @app.post("/api/restore-last")
    def restore_last(request: Request):
        return {"ok": R(request).ctl.restore_last()}

    @app.post("/api/restore-last/dismiss")
    def restore_dismiss(request: Request):
        R(request).ctl.dismiss_stale()
        return {"ok": True}

    # ------------------------------------------------------------------ music
    @app.get("/api/audio/devices")
    def audio_devices(request: Request):
        m = R(request).ctl.music
        return m.devices() if m else []

    @app.post("/api/music/start")
    async def music_start(request: Request, body: dict = Body(default={})):
        try:
            await R(request).ctl.start_music(body.get("params") or {})
        except Exception as e:  # noqa: BLE001
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/music/stop")
    async def music_stop(request: Request, body: dict = Body(default={})):
        await R(request).ctl.stop(restore=bool(body.get("restore", True)))
        return {"ok": True}

    @app.post("/api/music/calibrate")
    async def music_calibrate(request: Request, body: dict = Body(default={})):
        m = R(request).ctl.music
        if not m:
            raise HTTPException(409, "music unavailable")
        if m.calibrating:
            raise HTTPException(409, "already calibrating")
        try:
            seconds = float(body.get("seconds", 10))
        except (TypeError, ValueError):
            seconds = 10.0
        asyncio.get_running_loop().create_task(m.calibrate(seconds, body.get("device")))
        return {"ok": True}

    # ------------------------------------------------------------------ scenes
    @app.put("/api/scenes/{name}")
    def scene_put(request: Request, name: str, body: dict = Body(...)):
        if not name.strip() or len(name) > 40:
            raise HTTPException(400, "bad scene name")
        kind = body.get("kind", "static")
        if kind not in ("static", "effect", "music"):
            raise HTTPException(400, "bad scene kind")
        if kind == "effect" and body.get("effect") not in EFFECTS:
            raise HTTPException(400, "unknown effect")
        r = R(request)
        scene = {"kind": kind, "label": str(body.get("label") or name.title())[:40]}
        if kind == "static":
            def clean(d):
                out = {}
                if isinstance(d, dict):
                    if d.get("level") is not None:
                        out["level"] = max(0.0, min(100.0, float(d["level"])))
                    if d.get("cct") is not None:
                        out["cct"] = max(0.0, min(1.0, float(d["cct"])))
                return out
            try:
                scene["all"] = clean(body.get("all"))
                scene["fixtures"] = {f: clean(v) for f, v in (body.get("fixtures") or {}).items() if f in r.layout.fixtures}
            except (TypeError, ValueError):
                raise HTTPException(400, "bad scene values")
        elif kind == "effect":
            scene["effect"] = body["effect"]
            scene["params"] = sanitize(body.get("params"), set(r.layout.fixtures))
        else:
            scene["params"] = sanitize(body.get("params"), set(r.layout.fixtures))
        r.scenes.put(name, scene)
        return {"ok": True, "scene_order": r.scenes.order()}

    @app.post("/api/scenes/{name}/save-current")
    def scene_save_current(request: Request, name: str):
        if not name.strip() or len(name) > 40:
            raise HTTPException(400, "bad scene name")
        R(request).ctl.save_current_as(name)
        return {"ok": True, "scene_order": R(request).scenes.order()}

    @app.post("/api/scenes/{name}/reset")
    def scene_reset(request: Request, name: str):
        R(request).scenes.reset(name)
        return {"ok": True}

    @app.delete("/api/scenes/{name}")
    def scene_delete(request: Request, name: str):
        r = R(request)
        if not r.scenes.delete(name):
            raise HTTPException(400, "built-in scenes can be reset but not deleted")
        return {"ok": True, "scene_order": r.scenes.order()}

    # ------------------------------------------------------------------ simulator panel (simulate mode only)
    def _sim(request: Request):
        r = R(request)
        if not r.sim:
            raise HTTPException(404, "not running in simulator mode")
        return r.sim

    @app.post("/api/sim/disconnect")
    def sim_disconnect(request: Request, body: dict = Body(default={})):
        _sim(request).disconnect(float(body.get("seconds", 10)))
        return {"ok": True}

    @app.post("/api/sim/reconnect")
    def sim_reconnect(request: Request):
        _sim(request).reconnect()
        return {"ok": True}

    @app.post("/api/sim/latency")
    def sim_latency(request: Request, body: dict = Body(...)):
        _sim(request).latency_ms = max(0.0, min(2000.0, float(body.get("ms", 200))))
        return {"ok": True}

    # ------------------------------------------------------------------ lifecycle
    @app.post("/api/shutdown")
    async def shutdown(request: Request):
        r = R(request)
        r.exit_requested.set()
        if r.on_exit_request:
            asyncio.get_running_loop().call_later(0.3, r.on_exit_request)
        return {"ok": True}

    # ------------------------------------------------------------------ websocket + static
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        r: Runtime = sock.app.state.rt
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

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/connect", include_in_schema=False)
    def connect_page():
        return FileResponse(WEB / "connect.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(WEB / "sw.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    if (WEB / "icons").exists():
        app.mount("/icons", StaticFiles(directory=WEB / "icons"), name="icons")
    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
