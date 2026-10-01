"""All nine effects on the simulator.  SIMULATOR VERIFIED - the look on real lamps is a hardware check."""
import asyncio
import time

import pytest
from harness import settle, started
from vclock import run_virtual

from core.effects import EFFECTS
from core.model import DP_BRIGHT, level_to_raw

NAMES = list(EFFECTS)


def test_there_are_exactly_the_nine_expected_effects():
    assert NAMES == ["chase", "mirror", "pingpong", "pulse", "warmcool", "spark", "build", "alternate", "breathing"]


@pytest.mark.parametrize("name", NAMES)
def test_effect_runs_cleanly_within_the_command_budget(cfg, name):
    async def main():
        rt = await started(cfg)
        await rt.ctl.start_effect(name, {"speed": 60})
        n0, t0 = rt.sim.accepted, time.monotonic()
        await asyncio.sleep(45)
        rate = (rt.sim.accepted - n0) / (time.monotonic() - t0)
        out = dict(rate=rate, rejected=rt.sim.rejected, err=rt.ctl.last_error, mode=rt.ctl.mode,
                   queue=len(rt.sched.pending), sent=rt.sim.accepted - n0,
                   task_alive=rt.ctl._task is not None and not rt.ctl._task.done())
        await rt.ctl.stop(restore=True)
        await settle(rt)
        await rt.shutdown()
        return out

    r = run_virtual(main)
    assert r["err"] == "" and r["mode"] == "effect" and r["task_alive"]
    assert r["rejected"] == 0
    assert r["rate"] <= 4.3, r
    assert r["sent"] > 5, r                       # it actually animates
    assert r["queue"] <= 12


def test_chase_visits_every_light_in_ring_order(cfg):
    async def main():
        rt = await started(cfg)
        await settle(rt)
        rt.sim.log.clear()
        await rt.ctl.start_effect("chase", {"speed": 40})
        await asyncio.sleep(60)
        cid2fid = {f.cid: k for k, f in rt.layout.fixtures.items()}
        raised = [cid2fid[c] for _, c, dp, v in rt.sim.log if dp == DP_BRIGHT and v >= level_to_raw(40)]
        ring = rt.layout.ring
        await rt.ctl.stop(restore=True)
        await rt.shutdown()
        return raised, ring

    raised, ring = run_virtual(main)
    assert len(raised) >= 12 and set(raised) == set(ring), raised
    idx = [ring.index(f) for f in raised]
    steps = [(b - a) % 6 for a, b in zip(idx, idx[1:])]
    assert set(steps) == {1}, (raised, steps)      # strictly round the ring, bed-right -> ... -> bed-left


def test_mirror_chase_lights_mirrored_pairs(cfg):
    async def main():
        rt = await started(cfg)
        await settle(rt)
        rt.sim.log.clear()
        await rt.ctl.start_effect("mirror", {"speed": 30})
        await asyncio.sleep(60)
        cid2fid = {f.cid: k for k, f in rt.layout.fixtures.items()}
        raised = [cid2fid[c] for _, c, dp, v in rt.sim.log if dp == DP_BRIGHT and v >= level_to_raw(40)]
        pairs = [set(v) for v in rt.layout.pairs.values()]
        await rt.ctl.stop(restore=True)
        await rt.shutdown()
        return raised, pairs

    raised, pairs = run_virtual(main)
    assert len(raised) >= 8
    # lights are raised two at a time: each consecutive couple is one of the mirrored pairs
    for a, b in zip(raised[0::2], raised[1::2]):
        assert {a, b} in pairs, (a, b)


def test_live_parameter_change_does_not_restart(cfg):
    async def main():
        rt = await started(cfg)
        await rt.ctl.start_effect("chase", {"speed": 20})
        task = rt.ctl._task
        await asyncio.sleep(5)
        rt.ctl.update_params({"speed": 90, "direction": "reverse", "bogus": 1, "intensity": "nope"})
        await asyncio.sleep(2)
        out = (rt.ctl._task is task, rt.ctl.params["speed"], rt.ctl.params["direction"], "bogus" in rt.ctl.params,
               rt.ctl.params["intensity"])
        await rt.ctl.stop()
        await rt.shutdown()
        return out

    same, speed, direction, bogus, intensity = run_virtual(main)
    assert same and speed == 90 and direction == "reverse" and not bogus
    assert intensity == 70                         # invalid value was ignored, not applied
