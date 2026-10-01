"""Music director + profiles on the simulator, in virtual time.  SIMULATOR VERIFIED.

Architecture under test: audio -> features -> event detector -> bounded event queue -> profile -> scheduler -> gateway.
"""
import asyncio
import json
import time

import audio.director as director_mod
import pytest
from harness import settle, started, truth_levels
from vclock import run_virtual

from audio.analyzer import MusicEvent
from audio.profiles import Budget
from audio.sources import SilentSource


def test_budget_token_bucket():
    async def main():
        b = Budget(3.0, burst=3.0)
        assert b.take(3) and not b.take(1)            # burst used up
        await asyncio.sleep(1.0)
        assert b.take(3)                              # refilled at 3/s
        assert not b.take(5, debt=1.0) and b.take(5, debt=6.0)
        assert b.tokens < 0                           # debt is real, and is paid back
        await asyncio.sleep(3.0)
        assert b.available() > 0
        return b.denied

    assert run_virtual(main) == 2


def test_event_queue_is_bounded_and_drops_the_oldest():
    async def main():
        rt = await started(cfg_dir)
        m = rt.ctl.music
        for i in range(200):
            m._enqueue(MusicEvent("beat", float(i), 0.5))
        out = (m.queue.qsize(), m.events_dropped, m.queue.get_nowait().t)
        await rt.shutdown()
        return out

    size, dropped, oldest = run_virtual(main)
    assert size == 48 and dropped == 152 and oldest == 152.0     # the freshest 48 survive


def test_stale_beats_are_dropped_not_played():
    async def main():
        rt = await started(cfg_dir)
        m = rt.ctl.music
        await rt.ctl.start_music({"device": "demo", "profile": "beat"})
        n0 = m.profile.cues
        m.source._stop = True
        m._pump_task.cancel()
        await asyncio.sleep(1)
        for _ in range(10):                                # events far older than the staleness limit
            m._enqueue(MusicEvent("beat", time.monotonic() - 5.0, 0.9))
        await asyncio.sleep(2)
        out = (m.events_stale, m.profile.cues - n0)
        await rt.ctl.stop()
        await rt.shutdown()
        return out

    stale, cues = run_virtual(main)
    assert stale == 10 and cues == 0


@pytest.fixture(autouse=True)
def _cfg(cfg):
    global cfg_dir
    cfg_dir = cfg


@pytest.mark.parametrize("profile", ["beat", "club", "ambient"])
def test_profile_stays_inside_the_budget_and_restores(cfg, profile):
    async def main():
        rt = await started(cfg)
        await rt.ctl.apply_scene("CHILL")
        await settle(rt)
        before = {c: dict(v) for c, v in rt.sim.lamps.items()}
        await rt.ctl.start_music({"device": "demo", "profile": profile})
        n0, t0 = rt.sim.accepted, time.monotonic()
        await asyncio.sleep(75)                             # intro, build, drop of the demo track
        m = rt.ctl.music
        pub = m.public()
        out = dict(rate=(rt.sim.accepted - n0) / (time.monotonic() - t0), rejected=rt.sim.rejected,
                   frames=m.frames, fps=pub["stats"]["audio_fps"], cues=pub["stats"]["cues"], err=m.error,
                   mode=rt.ctl.mode, bpm=pub["bpm"], queue=len(rt.sched.pending), busiest=rt.sim.max_window_count)
        await rt.ctl.stop(restore=True)
        await settle(rt)
        out["restored"] = before == {c: dict(v) for c, v in rt.sim.lamps.items()}
        out["running_after"] = m.running
        await rt.shutdown()
        return out

    r = run_virtual(main)
    assert r["err"] == "" and r["mode"] == "music"
    assert r["fps"] > 30                                    # the audio side really runs at audio rate...
    assert r["rate"] <= 4.2, r                              # ...while the gateway never sees more than its budget
    assert r["rejected"] == 0 and r["busiest"] <= 5
    assert r["cues"] > 5, r                                 # and the lights do react
    assert r["queue"] <= 12
    assert r["restored"] and not r["running_after"]
    if profile != "ambient":
        assert r["bpm"] and abs(r["bpm"] - 124) < 12


def test_music_with_silence_leaves_the_lights_alone(cfg, monkeypatch):
    monkeypatch.setattr(director_mod, "make_source", lambda device, **kw: SilentSource())

    async def main():
        rt = await started(cfg)
        await rt.ctl.apply_scene("CHILL")
        await settle(rt)
        rt.sim.log.clear()
        await rt.ctl.start_music({"device": "demo", "profile": "club"})
        await asyncio.sleep(60)
        m = rt.ctl.music
        out = (m.profile.cues, m.public()["silent"], truth_levels(rt))
        await rt.ctl.stop(restore=True)
        await rt.shutdown()
        return out

    cues, silent, levels = run_virtual(main)
    assert cues == 0 and silent
    assert all(abs(v - 8) < 1.5 for _, v in levels.values()), levels      # just the base glow, no flicker


def test_profile_can_be_changed_live_and_sensitivity_applies(cfg):
    async def main():
        rt = await started(cfg)
        await rt.ctl.start_music({"device": "demo", "profile": "beat", "sensitivity": 30})
        await asyncio.sleep(10)
        m = rt.ctl.music
        first = m.profile
        rt.ctl.update_params({"profile": "ambient", "sensitivity": 90})
        await asyncio.sleep(3)
        out = (first.name, m.profile.name, m.analyzer.sensitivity, rt.ctl.params["profile"])
        await rt.ctl.stop()
        await rt.shutdown()
        return out

    assert run_virtual(main) == ("beat", "ambient", 90.0, "ambient")


def test_calibration_runs_saves_and_is_reused(cfg):
    async def main():
        rt = await started(cfg)
        m = rt.ctl.music
        cal = await m.calibrate(10, "demo")
        saved = json.loads((cfg / "music_calibration.json").read_text())
        await rt.ctl.start_music({"device": "demo"})
        used = m.analyzer.cal is not None
        pub = m.public()
        await rt.ctl.stop()
        await rt.shutdown()
        return cal, saved, used, pub["calibration"], m.calibrating

    cal, saved, used, pub_cal, still = run_virtual(main)
    assert cal.quality == "ok" and "demo" in saved and used and not still
    assert pub_cal["quality"] == "ok"


def test_calibration_while_running_uses_the_live_stream(cfg):
    async def main():
        rt = await started(cfg)
        await rt.ctl.start_music({"device": "demo"})
        m = rt.ctl.music
        task = asyncio.get_running_loop().create_task(m.calibrate(8, None))
        await asyncio.sleep(4)
        mid = (m.calibrating, 0 < m.calib_progress < 1)
        cal = await task
        await rt.ctl.stop()
        await rt.shutdown()
        return mid, cal.quality

    mid, q = run_virtual(main)
    assert mid == (True, True) and q == "ok"


def test_audio_source_failure_is_survived(cfg, monkeypatch):
    from audio.sources import SyntheticSource

    class Flaky(SyntheticSource):
        n = 0

        async def blocks(self):
            Flaky.n += 1
            if Flaky.n == 1:
                async for i, b in _enumerate(super().blocks()):
                    if i > 100:
                        raise OSError("device unplugged")
                    yield b
            else:
                async for b in super().blocks():
                    yield b

    async def _enumerate(gen):
        i = 0
        async for x in gen:
            yield i, x
            i += 1

    monkeypatch.setattr(director_mod, "make_source", lambda device, **kw: Flaky())

    async def main():
        rt = await started(cfg)
        await rt.ctl.start_music({"device": "demo", "profile": "beat"})
        await asyncio.sleep(3)
        mid_err = rt.ctl.music.error
        await asyncio.sleep(15)
        m = rt.ctl.music
        out = (mid_err, m.error, m.frames, m.running)
        await rt.ctl.stop()
        await rt.shutdown()
        return out

    mid_err, err, frames, running = run_virtual(main)
    assert "device unplugged" in mid_err and err == "" and running and frames > 300
