"""30-minute Party and Music sessions on the simulator, in virtual time.

SIMULATOR VERIFIED: proves the software stays inside the gateway budget, survives a gateway outage,
never leaks tasks/queue growth, never logs an error and restores the room. It does NOT prove anything about
the real gateway or lamps - see REAL_HARDWARE_CHECKLIST.md.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

import pytest
from harness import settle, started
from vclock import run_virtual

from core.effects import EFFECTS

pytestmark = pytest.mark.endurance

MINUTES = 30
SUMMARY = Path(__file__).resolve().parent.parent / "reports" / "sim_endurance_summary.json"


def record(name: str, r: dict) -> None:
    """Keep the numbers (SIMULATOR results) next to the code for the docs."""
    try:
        data = json.loads(SUMMARY.read_text()) if SUMMARY.exists() else {}
    except ValueError:
        data = {}
    keep = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items() if k != "tasks"}
    keep["tasks_min_max"] = [min(r["tasks"]), max(r["tasks"])]
    keep["source"] = "SIMULATOR VERIFIED (virtual time) - not real hardware"
    data[name] = keep
    SUMMARY.parent.mkdir(exist_ok=True)
    SUMMARY.write_text(json.dumps(data, indent=2, default=str))


async def sample(rt, out, stop_flag):
    while not stop_flag.is_set():
        m = rt.ctl.music
        out["queue"] = max(out["queue"], len(rt.sched.pending))
        out["tasks"].append(len(asyncio.all_tasks()))
        out["mq"] = max(out["mq"], m.queue.qsize() if m else 0)
        await asyncio.sleep(10)


def test_party_30_minutes(cfg, caplog):
    caplog.set_level(logging.WARNING)

    async def main():
        rt = await started(cfg)
        await rt.ctl.apply_scene("CHILL")
        await settle(rt)
        before = {c: dict(v) for c, v in rt.sim.lamps.items()}
        out = {"queue": 0, "tasks": [], "mq": 0}
        stop = asyncio.Event()
        sampler = asyncio.get_running_loop().create_task(sample(rt, out, stop))
        n0, t0 = rt.sim.accepted, time.monotonic()
        names = list(EFFECTS)
        per = MINUTES * 60 / len(names)
        reconnects_before = 0
        for i, name in enumerate(names):
            await rt.ctl.start_effect(name, {"speed": 30 + (i * 13) % 60, "direction": ["forward", "reverse", "random"][i % 3]})
            await asyncio.sleep(per / 2)
            rt.ctl.update_params({"speed": 80, "intensity": 90})          # live tweaks mid-effect
            rt.ctl.set_master(brightness=70 if i % 2 else 40)
            if i == 3:
                reconnects_before = rt.link.reconnects
                rt.sim.disconnect(40)                                     # the gateway drops for 40 s mid-party
            if i == 6:
                await rt.ctl.normal()                                     # panic button in the middle of everything
                await asyncio.sleep(5)
            await asyncio.sleep(per / 2 - (5 if i == 6 else 0))
        elapsed = time.monotonic() - t0
        sent = rt.sim.accepted - n0
        await rt.ctl.stop(restore=True)
        await settle(rt)
        stop.set()
        await sampler
        after = {c: dict(v) for c, v in rt.sim.lamps.items()}
        res = dict(elapsed=elapsed, rate=sent / elapsed, rejected=rt.sim.rejected, busiest=rt.sim.max_window_count,
                   queue=out["queue"], tasks=out["tasks"], reconnects=rt.link.reconnects - reconnects_before,
                   failed=rt.sched.stats.failed, mode=rt.ctl.mode, err=rt.ctl.last_error, sent=sent,
                   restored_on_state=all(before[c]["20"] == after[c]["20"] for c in before))
        await rt.shutdown()
        return res

    r = run_virtual(main)
    record("party_30_min", r)
    assert r["elapsed"] >= MINUTES * 60 - 1
    assert r["rate"] <= 4.1, r                      # average command rate over 30 minutes
    assert r["rejected"] == 0 and r["busiest"] <= 5
    assert r["queue"] <= 14, r
    assert r["reconnects"] >= 1, r                  # the outage really happened and was recovered
    assert max(r["tasks"][-20:]) <= min(r["tasks"][:20]) + 6, (min(r["tasks"]), max(r["tasks"]))   # no task leak
    assert r["err"] == "" and r["restored_on_state"]
    assert r["sent"] > 3000
    assert not [x for x in caplog.records if x.levelno >= logging.ERROR], [x.getMessage() for x in caplog.records if x.levelno >= logging.ERROR]


def test_music_30_minutes(cfg, caplog):
    caplog.set_level(logging.WARNING)

    async def main():
        rt = await started(cfg)
        m = rt.ctl.music
        await rt.ctl.apply_scene("CHILL")
        await settle(rt)
        before = {c: dict(v) for c, v in rt.sim.lamps.items()}
        out = {"queue": 0, "tasks": [], "mq": 0}
        stop = asyncio.Event()
        sampler = asyncio.get_running_loop().create_task(sample(rt, out, stop))
        n0, t0 = rt.sim.accepted, time.monotonic()
        await rt.ctl.start_music({"device": "demo", "profile": "beat", "sensitivity": 60})
        cal = None
        reconnects_before = rt.link.reconnects
        for minute in range(MINUTES):
            await asyncio.sleep(60)
            if minute == 9:
                rt.ctl.update_params({"profile": "club"})
            if minute == 14:
                rt.sim.disconnect(45)                                     # outage while music is playing
            if minute == 19:
                rt.ctl.update_params({"profile": "ambient", "sensitivity": 85})
                cal = await m.calibrate(10, None)                         # calibrate against the live stream
            if minute == 24:
                rt.ctl.update_params({"profile": "club", "sensitivity": 40, "max_brightness": 60})
        elapsed = time.monotonic() - t0
        sent = rt.sim.accepted - n0
        pub = m.public()
        await rt.ctl.stop(restore=True)
        await settle(rt)
        stop.set()
        await sampler
        after = {c: dict(v) for c, v in rt.sim.lamps.items()}
        res = dict(elapsed=elapsed, rate=sent / elapsed, rejected=rt.sim.rejected, busiest=rt.sim.max_window_count,
                   queue=out["queue"], mq=out["mq"], tasks=out["tasks"], reconnects=rt.link.reconnects - reconnects_before,
                   frames=m.frames, stats=pub["stats"], err=m.error, cal=cal.quality if cal else None, sent=sent,
                   bpm=pub["bpm"], beats=pub["beats"], restored=before == after)
        await rt.shutdown()
        return res

    r = run_virtual(main)
    record("music_30_min", r)
    assert r["elapsed"] >= MINUTES * 60 - 1
    assert r["frames"] > MINUTES * 60 * 40            # audio side ran at audio rate the whole time (~43 fps)
    assert r["rate"] <= 4.1, r                        # while the gateway averaged <= its budget
    assert r["rejected"] == 0 and r["busiest"] <= 5
    assert r["queue"] <= 14 and r["mq"] <= 48
    assert r["reconnects"] >= 1
    assert max(r["tasks"][-20:]) <= min(r["tasks"][:20]) + 6
    assert r["cal"] == "ok" and r["err"] == ""
    assert r["stats"]["cues"] > 200 and r["beats"] > 1500 and r["sent"] > 2500, r
    assert r["restored"], "room not restored after the music session"
    assert not [x for x in caplog.records if x.levelno >= logging.ERROR], [x.getMessage() for x in caplog.records if x.levelno >= logging.ERROR]
