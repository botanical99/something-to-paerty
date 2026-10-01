"""MusicDirector: the only object the rest of the app talks to for music.

    audio source -> FeatureExtractor -> MusicAnalyzer -> bounded event queue -> Profile -> Scheduler -> gateway
    (audio frame rate)                  (events, a few/s)                      (command budget)

The audio loop never awaits the lighting side: when the queue is full the OLDEST event is discarded, and
events that arrive late are dropped, so the lights are always reacting to *now*, never to a backlog.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Callable

from audio.analyzer import Calibration, MusicAnalyzer, MusicEvent, build_calibration
from audio.features import FeatureExtractor, Features
from audio.profiles import PROFILES, Budget, Profile, make_profile
from audio.sources import DEMO_ID, list_devices, make_source
from core.effects import EffectContext
from core.model import Layout
from core.scheduler import Scheduler

log = logging.getLogger("music")

STALE_EVENT_S = 0.45          # a beat/onset older than this is no longer worth a lamp command


class MusicDirector:
    PARAM_KEYS = ("profile", "sensitivity", "device")

    def __init__(self, layout: Layout, sched: Scheduler, ctl, *, calibration_file: Path | None = None,
                 budget_rate: float = 3.0, default_device: str = DEMO_ID):
        self.layout, self.sched, self.ctl = layout, sched, ctl
        self.cal_file = Path(calibration_file) if calibration_file else None
        self.budget_rate = budget_rate
        self.default_device = default_device
        self.running = False
        self.calibrating = False
        self.calib_progress = 0.0
        self.error = ""
        self.params: dict = {}
        self.epoch = -1
        self.device = default_device
        self.source = None
        self.fx: FeatureExtractor | None = None
        self.analyzer: MusicAnalyzer | None = None
        self.profile: Profile | None = None
        self.budget = Budget(budget_rate)
        self.queue: asyncio.Queue[MusicEvent] = asyncio.Queue(maxsize=48)
        self.events_total = 0
        self.events_dropped = 0
        self.events_stale = 0
        self.frames = 0
        self.recent: deque[dict] = deque(maxlen=12)
        self.cal: Calibration | None = None
        self._pump_task: asyncio.Task | None = None
        self._map_task: asyncio.Task | None = None
        self._collectors: list[Callable[[Features], None]] = []
        self._fps_t = time.monotonic()
        self._fps_n = 0
        self.audio_fps = 0.0
        self.beat_flash = 0.0

    # ------------------------------------------------------------------ discovery / calibration store
    def devices(self) -> list[dict]:
        return list_devices()

    def _load_cal(self, device: str) -> Calibration | None:
        if not self.cal_file or not self.cal_file.exists():
            return None
        try:
            d = json.loads(self.cal_file.read_text(encoding="utf-8")).get(device)
            return Calibration.from_json(d) if d else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_cal(self, cal: Calibration) -> None:
        if not self.cal_file:
            return
        try:
            data = json.loads(self.cal_file.read_text(encoding="utf-8")) if self.cal_file.exists() else {}
        except (OSError, ValueError):
            data = {}
        data[cal.device] = cal.to_json()
        self.cal_file.parent.mkdir(parents=True, exist_ok=True)
        self.cal_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def default_params(self) -> dict:
        return {"profile": "beat", "sensitivity": 60, "device": self.device or self.default_device}

    # ------------------------------------------------------------------ lifecycle
    async def start(self, epoch: int, params: dict) -> None:
        await self.stop()
        self.params, self.epoch, self.error = params, epoch, ""
        self.device = str(params.get("device") or self.default_device)
        self.budget = Budget(self.budget_rate)
        self.queue = asyncio.Queue(maxsize=48)
        self.events_total = self.events_dropped = self.events_stale = self.frames = 0
        self.recent.clear()
        src = make_source(self.device)
        await src.start()                                     # raises RuntimeError if the device can't be opened
        self.source = src
        self.fx = FeatureExtractor(src.sr, src.hop)
        self.cal = self._load_cal(self.device)
        self.analyzer = MusicAnalyzer(self.fx.fps, float(params.get("sensitivity", 60)), self.cal)
        ctx = EffectContext(self.layout, self.sched, epoch, params)
        self.profile = make_profile(str(params.get("profile", "beat")), ctx, self.budget, self.analyzer)
        self.profile.begin()
        self.running = True
        loop = asyncio.get_running_loop()
        self._pump_task = loop.create_task(self._pump(), name="music-pump")
        self._map_task = loop.create_task(self._mapper(), name="music-mapper")
        log.info("music started: device=%s profile=%s sensitivity=%s", self.device, self.profile.name,
                 params.get("sensitivity"))

    async def stop(self) -> None:
        self.running = False
        tasks, self._pump_task, self._map_task = [self._pump_task, self._map_task], None, None
        for t in tasks:
            if t:
                t.cancel()
        for t in tasks:
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        src, self.source = self.source, None
        if src is not None:
            try:
                await src.stop()
            except Exception:  # noqa: BLE001
                pass
        self.profile = None
        if self.analyzer:
            for k in self.analyzer.levels:
                self.analyzer.levels[k] = 0.0

    def update(self, p: dict) -> None:
        """Live changes from the UI (the controller already merged them into self.params)."""
        if not self.running or self.analyzer is None:
            return
        if "sensitivity" in p:
            self.analyzer.set_sensitivity(float(p["sensitivity"]))
        if "profile" in p and self.profile and p["profile"] != self.profile.name and p["profile"] in PROFILES:
            ctx = EffectContext(self.layout, self.sched, self.epoch, self.params)
            self.profile = make_profile(p["profile"], ctx, self.budget, self.analyzer)
            self.profile.begin()
        if "device" in p and str(p["device"]) != self.device:
            asyncio.get_running_loop().create_task(self._switch_device(str(p["device"])))

    async def _switch_device(self, device: str) -> None:
        try:
            await self.start(self.epoch, self.params)
        except Exception as e:  # noqa: BLE001
            self.error = f"could not open '{device}': {e}"

    # ------------------------------------------------------------------ audio loop (high rate, never blocks on lights)
    async def _pump(self) -> None:
        while True:
            src = self.source
            if src is None:
                return
            try:
                assert self.fx is not None and self.analyzer is not None
                async for blk in src.blocks():
                    t = time.monotonic()
                    f = self.fx.process(blk, t)
                    self.frames += 1
                    self._fps_n += 1
                    if t - self._fps_t >= 2.0:
                        self.audio_fps = round(self._fps_n / (t - self._fps_t), 1)
                        self._fps_t, self._fps_n = t, 0
                    for c in self._collectors:
                        c(f)
                    for ev in self.analyzer.process(f):
                        self._enqueue(ev)
                if not self.running:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.error = f"audio input problem: {e}"
                log.warning(self.error)
            # source ended unexpectedly (device unplugged?) -> reopen
            if not self.running:
                return
            await asyncio.sleep(2.0)
            try:
                old, new = src, make_source(self.device)
                await old.stop()
                await new.start()
                self.source = new
                self.fx = FeatureExtractor(new.sr, new.hop)
                sens = self.analyzer.sensitivity if self.analyzer else 60.0
                self.analyzer = MusicAnalyzer(self.fx.fps, sens, self.cal)
                if self.profile:
                    self.profile.an = self.analyzer
                self.error = ""
            except Exception as e:  # noqa: BLE001
                self.error = f"audio input lost, retrying: {e}"

    def _enqueue(self, ev: MusicEvent) -> None:
        self.events_total += 1
        if ev.kind == "beat":
            self.beat_flash = ev.strength
        if self.queue.full():
            try:
                self.queue.get_nowait()                       # drop the OLDEST: fresh events matter most
                self.events_dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(ev)
        if ev.kind != "energy":
            self.recent.append({"kind": ev.kind, "t": round(ev.t, 2), "s": round(ev.strength, 2)})

    # ------------------------------------------------------------------ lighting side (event rate)
    async def _mapper(self) -> None:
        while True:
            try:
                ev: MusicEvent | None = await asyncio.wait_for(self.queue.get(), 0.15)
            except asyncio.TimeoutError:
                ev = None
            try:
                now = time.monotonic()
                prof = self.profile
                if prof is None:
                    continue
                while ev is not None:
                    if ev.kind in ("beat", "onset_mid", "onset_treble") and now - ev.t > STALE_EVENT_S:
                        self.events_stale += 1
                    else:
                        prof.on_event(ev)
                    try:
                        ev = self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        ev = None
                prof.tick(now)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("music mapper error")
                await asyncio.sleep(0.2)

    # ------------------------------------------------------------------ calibration (~10 s of typical music)
    async def calibrate(self, seconds: float = 10.0, device: str | None = None) -> Calibration | None:
        if self.calibrating:
            return None
        device = str(device or self.device or self.default_device)
        self.calibrating, self.calib_progress, self.error = True, 0.0, ""
        frames: list[Features] = []
        sr = 44100
        try:
            seconds = max(3.0, min(30.0, float(seconds)))
            t_end = time.monotonic() + seconds
            if self.running and device == self.device and self.fx is not None:
                sr = self.fx.sr
                self._collectors.append(frames.append)
                try:
                    while time.monotonic() < t_end:
                        self.calib_progress = 1 - (t_end - time.monotonic()) / seconds
                        await asyncio.sleep(0.1)
                finally:
                    self._collectors.remove(frames.append)
            else:
                src = make_source(device)
                await src.start()
                try:
                    fx = FeatureExtractor(src.sr, src.hop)
                    sr = src.sr
                    gen = src.blocks()
                    async for blk in gen:
                        frames.append(fx.process(blk, time.monotonic()))
                        self.calib_progress = 1 - max(0.0, t_end - time.monotonic()) / seconds
                        if time.monotonic() >= t_end:
                            break
                    await gen.aclose()
                finally:
                    await src.stop()
            cal = build_calibration(device, sr, frames, seconds, ts=time.time())
            self.cal = cal
            self._save_cal(cal)
            if self.analyzer and self.running and device == self.device:
                self.analyzer.apply_calibration(cal)
            log.info("calibrated '%s': quality=%s floor=%.4f peak=%.4f", device, cal.quality, cal.rms_floor, cal.rms_peak)
            return cal
        except Exception as e:  # noqa: BLE001
            self.error = f"calibration failed: {e}"
            log.warning(self.error)
            return None
        finally:
            self.calibrating, self.calib_progress = False, 0.0

    # ------------------------------------------------------------------ for the UI
    def public(self) -> dict:
        an = self.analyzer
        prof = self.profile
        cal = self.cal if self.cal else self._load_cal(self.device)
        age = None
        if an and an.last_beat_t > 0 and self.running:
            age = round(time.monotonic() - an.last_beat_t, 2)
        return {
            "available": True, "running": self.running, "calibrating": self.calibrating,
            "calib_progress": round(self.calib_progress, 2), "error": self.error,
            "device": self.device, "profile": prof.name if prof else self.params.get("profile", "beat"),
            "profiles": [{"id": p.name, "label": p.label} for p in PROFILES.values()],
            "sensitivity": an.sensitivity if an else self.params.get("sensitivity", 60),
            "levels": {k: round(v, 3) for k, v in an.levels.items()} if (an and self.running) else
                      {"rms": 0, "bass": 0, "mid": 0, "treble": 0, "energy": 0},
            "bpm": an.bpm if (an and self.running) else None, "beats": an.beat_count if an else 0,
            "beat_age": age, "section": an.section if (an and self.running) else "",
            "silent": bool(an.silent) if (an and self.running) else True,
            "recent": list(self.recent)[-6:],
            "calibration": ({"quality": cal.quality, "ts": cal.ts, "device": cal.device} if cal else None),
            "stats": {"events": self.events_total, "dropped": self.events_dropped, "stale": self.events_stale,
                      "cues": prof.cues if prof else 0, "skipped": prof.skipped if prof else 0,
                      "budget_spent": round(self.budget.spent, 1), "audio_fps": self.audio_fps},
        }
