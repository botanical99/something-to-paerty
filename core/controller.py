"""Lighting controller: owns the current mode and guarantees only one thing drives the lights.

Modes: idle | scene | effect | music | manual
  * entering effect/music snapshots the room once (raw-exact) so STOP can restore it;
  * every transition bumps the scheduler epoch -> the previous effect is rejected + cancelled;
  * NORMAL cancels everything, flushes everything except CRITICAL, then applies the normal scene.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable

from core.effects import EFFECTS, DEFAULT_PARAMS, EffectContext, effect_defaults
from core.model import Layout
from core.params import sanitize
from core.scenes import SceneStore
from core.scheduler import Prio, Scheduler

log = logging.getLogger("controller")


class LightingController:
    def __init__(self, layout: Layout, sched: Scheduler, scenes: SceneStore, state_file: Path | None = None):
        self.layout, self.sched, self.scenes = layout, sched, scenes
        self.state_file = Path(state_file) if state_file else None
        self.stale_snapshot: dict | None = self._read_state_file()   # left over from an unclean exit
        self.mode, self.name = "idle", ""
        self.scene = ""                      # name of the scene that produced the current look ('' = manual / none)
        self.params: dict = {}
        self.snapshot: dict | None = None
        self._task: asyncio.Task | None = None
        self.music = None                       # MusicDirector, attached by the app (optional)
        self.listeners: list[Callable[[], None]] = []
        self.last_error = ""
        self._housekeeping: asyncio.Task | None = None
        self.master = {"brightness": 35.0, "cct": 0.45}
        self.last_levels: dict[str, float] = {}      # last non-zero level per fixture (for "turn back on")

    # ------------------------------------------------------------------ crash-safe snapshot
    def _read_state_file(self) -> dict | None:
        try:
            if self.state_file and self.state_file.exists():
                d = json.loads(self.state_file.read_text(encoding="utf-8"))
                snap = d.get("snapshot")
                return snap if isinstance(snap, dict) and snap else None
        except (OSError, ValueError):
            pass
        return None

    def _write_state_file(self, snap: dict | None) -> None:
        if not self.state_file:
            return
        try:
            if snap:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps({"snapshot": snap, "ts": time.time()}), encoding="utf-8")
            elif self.state_file.exists():
                self.state_file.unlink()
        except OSError as e:
            log.debug("state file: %s", e)

    def restore_last(self) -> bool:
        """Put the room back the way it was before an animation that never got to restore (crash / power cut)."""
        snap, self.stale_snapshot = self.stale_snapshot, None
        self._write_state_file(None)
        if not snap:
            return False
        self.sched.restore(snap, Prio.HIGH)
        self.mode, self.name = "manual", ""
        self.notify()
        return True

    def dismiss_stale(self) -> None:
        self.stale_snapshot = None
        self._write_state_file(None)
        self.notify()

    # ------------------------------------------------------------------ plumbing
    def notify(self) -> None:
        for fn in self.listeners:
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    async def start(self) -> None:
        self._housekeeping = asyncio.get_running_loop().create_task(self._housekeeping_loop(), name="housekeeping")

    async def _cancel_runner(self, flush_below: Prio = Prio.NORMAL) -> None:
        self.sched.new_epoch(flush_below)
        t, self._task = self._task, None
        if self.music:
            await self.music.stop()
        if t:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _begin_animation(self) -> None:
        if self.mode not in ("effect", "music"):
            self.snapshot = self.sched.capture()
            self._write_state_file(self.snapshot)

    # ------------------------------------------------------------------ static scenes
    def _apply_static(self, scene: dict, prio: Prio) -> None:
        base = scene.get("all", {})
        for fid in self.layout.fixtures:
            s = {**base, **scene.get("fixtures", {}).get(fid, {})}
            lv, cc = s.get("level"), s.get("cct")
            self.sched.set_light(fid, lv, cc, prio=prio, tag="scene")

    async def normal(self) -> None:
        """Immediate: cancel everything, apply the configured NORMAL scene at CRITICAL priority."""
        await self._cancel_runner(Prio.HIGH)
        self.snapshot = None
        self._write_state_file(None)
        self._apply_static(self.scenes.get("NORMAL"), Prio.CRITICAL)
        self.mode, self.name, self.params, self.scene = "scene", "NORMAL", {}, "NORMAL"
        self._sync_master_from_scene(self.scenes.get("NORMAL"))
        self.notify()

    async def apply_scene(self, name: str) -> None:
        if name == "NORMAL":
            return await self.normal()
        sc = self.scenes.get(name)
        kind = sc.get("kind", "static")
        if kind == "effect":
            return await self.start_effect(sc["effect"], sc.get("params", {}), scene_name=name)
        if kind == "music":
            return await self.start_music(sc.get("params", {}), scene_name=name)
        await self._cancel_runner()
        self.snapshot = None
        self._write_state_file(None)
        self._apply_static(sc, Prio.HIGH)
        self.mode, self.name, self.params, self.scene = "scene", name, {}, name
        self._sync_master_from_scene(sc)
        self.notify()

    def _sync_master_from_scene(self, sc: dict) -> None:
        a = sc.get("all", {})
        if "level" in a:
            self.master["brightness"] = float(a["level"])
        if "cct" in a:
            self.master["cct"] = float(a["cct"])

    # ------------------------------------------------------------------ effects
    async def start_effect(self, name: str, params: dict | None = None, scene_name: str | None = None) -> None:
        if name not in EFFECTS:
            raise ValueError(f"unknown effect {name}")
        self._begin_animation()
        await self._cancel_runner()
        epoch = self.sched.epoch
        self.params = {**effect_defaults(name), **sanitize(params, set(self.layout.fixtures))}
        self.mode, self.name, self.scene = "effect", name, scene_name or ""
        ctx = EffectContext(self.layout, self.sched, epoch, self.params)
        self._task = asyncio.get_running_loop().create_task(self._run_effect(name, ctx), name=f"effect-{name}")
        self.notify()

    async def _run_effect(self, name: str, ctx: EffectContext) -> None:
        try:
            await EFFECTS[name].run(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("effect %s crashed", name)
            self.last_error = f"effect {name}: {e}"
            self.notify()

    def update_params(self, params: dict) -> None:
        """Live parameter change for the running effect / music (no restart)."""
        allowed = set(DEFAULT_PARAMS) | set(self.music.PARAM_KEYS if self.music else ())
        clean = {k: v for k, v in sanitize(params, set(self.layout.fixtures)).items() if k in allowed}
        if self.mode not in ("effect", "music"):
            return
        self.params.update(clean)
        if self.mode == "music" and self.music:
            self.music.update(clean)
        self.notify()

    # ------------------------------------------------------------------ music
    async def start_music(self, params: dict | None = None, scene_name: str | None = None) -> None:
        if self.music is None:
            raise RuntimeError("music mode unavailable (audio engine not loaded)")
        self._begin_animation()
        await self._cancel_runner()
        epoch = self.sched.epoch
        self.params = {**effect_defaults("chase"), **self.music.default_params(), **sanitize(params, set(self.layout.fixtures))}
        self.mode, self.name, self.scene = "music", "music", scene_name or ""
        try:
            await self.music.start(epoch, self.params)
        except Exception as e:  # noqa: BLE001
            log.warning("music could not start: %s", e)
            self.last_error = f"music: {e}"
            await self.stop(restore=True)
            raise RuntimeError(str(e)) from e
        self.last_error = ""
        self.notify()

    # ------------------------------------------------------------------ stop / manual
    async def stop(self, restore: bool = True) -> None:
        was_anim = self.mode in ("effect", "music")
        await self._cancel_runner()
        self.scene = ""
        if restore and was_anim and self.snapshot:
            self.sched.restore(self.snapshot, Prio.HIGH)
            self.mode, self.name = "idle", ""
        else:
            self.mode, self.name = ("manual", "") if was_anim else (self.mode, self.name)
        self.snapshot = None
        self._write_state_file(None)
        self.params = {}
        self.notify()

    def set_master(self, brightness: float | None = None, cct: float | None = None) -> None:
        if brightness is not None:
            self.master["brightness"] = float(brightness)
        if cct is not None:
            self.master["cct"] = float(cct)
        if self.mode in ("effect", "music"):
            if brightness is not None:
                self.params["max_brightness"] = max(5.0, float(brightness))
            if cct is not None:
                w = max(0.1, float(self.params.get("cct_cool", 0.65)) - float(self.params.get("cct_warm", 0.05)))
                lo = max(0.0, min(1.0 - w, float(cct) - w / 2))
                self.params["cct_warm"], self.params["cct_cool"] = round(lo, 3), round(lo + w, 3)
            if self.music:
                self.music.update(self.params)
        else:
            for fid in self.layout.fixtures:
                self.sched.set_light(fid, brightness, cct, prio=Prio.HIGH, tag="master")
            if self.mode in ("scene", "idle"):
                self.mode, self.name, self.scene = "manual", "", ""
        self.notify()

    async def set_fixture(self, fid: str, level: float | None, cct: float | None, on: bool | None = None) -> None:
        if fid not in self.layout.fixtures:
            raise KeyError(fid)
        if on is False:
            level = 0.0
        elif on is True and level is None:
            cur = self.sched.public_state()[fid]
            level = cur["level"] if cur["on"] and cur["level"] > 0 else self.last_levels.get(fid, 40.0)
        if level is not None and level > 0:
            self.last_levels[fid] = float(level)
        if self.mode in ("effect", "music"):
            await self.stop(restore=False)     # a manual tap ends the animation (no fighting)
        self.sched.set_light(fid, level, cct, prio=Prio.HIGH, tag="manual")
        if self.mode in ("scene", "idle"):
            self.mode, self.name, self.scene = "manual", "", ""
        self.notify()

    def save_current_as(self, name: str, label: str | None = None) -> None:
        """Store what the room is showing right now as a static scene."""
        snap = self.sched.capture()
        from core.model import raw_to_cct, raw_to_level
        fx = {fid: {"level": raw_to_level(s["bright_raw"]) if s["on"] else 0.0, "cct": raw_to_cct(s["cct_raw"])}
              for fid, s in snap.items()}
        self.scenes.put(name, {"kind": "static", "label": label or name.title(), "all": {}, "fixtures": fx})
        self.notify()

    # ------------------------------------------------------------------ housekeeping / shutdown
    async def _housekeeping_loop(self) -> None:
        """While idle, adopt changes made from the Tuya phone app so the UI stays truthful."""
        while True:
            await asyncio.sleep(20)
            try:
                if self.mode in ("effect", "music") or self.sched.pending or self.sched.link.state != "online":
                    continue
                before = self.sched.public_state()
                await self.sched.adopt_from_gateway()
                self.sched.adopt_known_as_desired()
                if self.sched.public_state() != before:
                    log.info("room changed outside this controller (phone app?) - state adopted")
                    self.notify()
            except Exception as e:  # noqa: BLE001
                log.debug("housekeeping: %s", e)

    async def on_gateway_online(self) -> None:
        """After (re)connect: learn the lamps' real state, then re-assert anything we still owe them."""
        try:
            owed = {c: dict(d) for c, d in self.sched.desired.items()}
            await self.sched.adopt_from_gateway()          # takes a second or two: the user may act meanwhile
            if self.mode in ("effect", "music"):
                pass                                       # the running effect keeps driving
            elif self.sched.pending or self.sched.desired != owed:
                self.sched.reassert()                      # something was asked of the lamps: it still stands
            else:
                self.sched.adopt_known_as_desired()
        except Exception as e:  # noqa: BLE001
            log.warning("post-connect sync failed: %s", e)
        self.notify()

    async def shutdown(self) -> None:
        """Leave the room in a sensible state: stop animation, restore the pre-animation look."""
        if self._housekeeping:
            self._housekeeping.cancel()
        try:
            await self.stop(restore=True)
            await asyncio.wait_for(self.sched.idle_event.wait(), 12)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ state for the UI
    def state(self) -> dict:
        return {
            "mode": self.mode, "name": self.name, "scene": self.scene, "params": self.params, "master": self.master,
            "has_snapshot": self.snapshot is not None, "error": self.last_error,
            "stale_snapshot": self.stale_snapshot is not None,
            "fixtures": self.sched.public_state(),
        }
