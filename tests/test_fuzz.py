"""Randomised operation sequences with slow / jittery gateways and outages.  SIMULATOR VERIFIED.

Invariant: whatever the user does, when the dust settles the lamps hold exactly what the controller believes it asked for
(no stuck lights, no lost commands, no queued ghosts), and the gateway never saw more than its budget.
"""
import asyncio
import os
import random

import pytest
from harness import settle, started
from vclock import run_virtual

from core.model import DP_BRIGHT, DP_CCT, DP_ON

OPS = ["scene", "effect", "fixture", "master", "normal", "stop", "music", "params", "wait", "disconnect"]


@pytest.mark.parametrize("seed", range(int(os.environ.get("FUZZ_SEEDS", "8"))))
def test_random_sessions_converge(cfg, seed):
    rnd = random.Random(seed)

    async def main():
        rt = await started(cfg, latency_ms=rnd.choice([30, 200, 450, 800]))
        scenes = ["CHILL", "CINEMA", "ALL ON", "ALL OFF", "PARTY", "NORMAL"]
        effects = ["chase", "mirror", "pingpong", "pulse", "warmcool", "spark", "build", "alternate", "breathing"]
        fids = list(rt.layout.fixtures)
        for _ in range(60):
            op = rnd.choice(OPS)
            try:
                if op == "scene":
                    await rt.ctl.apply_scene(rnd.choice(scenes))
                elif op == "effect":
                    await rt.ctl.start_effect(rnd.choice(effects), {"speed": rnd.randint(0, 100), "intensity": rnd.randint(10, 100)})
                elif op == "fixture":
                    await rt.ctl.set_fixture(rnd.choice(fids), rnd.choice([0, 5, 30, 77, 100]), rnd.random())
                elif op == "master":
                    rt.ctl.set_master(rnd.randint(0, 100), rnd.random())
                elif op == "normal":
                    await rt.ctl.normal()
                elif op == "stop":
                    await rt.ctl.stop(restore=rnd.random() < 0.7)
                elif op == "music":
                    await rt.ctl.start_music({"device": "demo", "profile": rnd.choice(["beat", "club", "ambient"])})
                elif op == "params":
                    rt.ctl.update_params({"speed": rnd.randint(0, 100), "direction": rnd.choice(["forward", "reverse", "random"])})
                elif op == "disconnect":
                    rt.sim.disconnect(rnd.randint(3, 25))
            except (RuntimeError, ValueError):
                pass
            await asyncio.sleep(rnd.choice([0.05, 0.3, 1, 3, 8, 15]))
        await rt.ctl.stop(restore=rnd.random() < 0.5)
        rt.sim.reconnect()
        await asyncio.sleep(60)                         # let the supervisor reconnect and the queue drain
        await settle(rt, 120)
        s = rt.sched
        mism = []
        for fid, f in rt.layout.fixtures.items():
            want, got = s.desired.get(f.cid, {}), rt.sim.lamps[f.cid]
            for dp in (DP_ON, DP_BRIGHT, DP_CCT):
                if dp in want and want[dp] != got[dp]:
                    if dp != DP_ON and want.get(DP_ON) is False:
                        continue                         # an OFF lamp's hidden brightness / colour is irrelevant
                    mism.append((fid, dp, want[dp], got[dp]))
        out = dict(mism=mism, pending=len(s.pending), rejected=rt.sim.rejected, busiest=rt.sim.max_window_count,
                   state=rt.link.state, mode=rt.ctl.mode)
        await rt.shutdown()
        return out

    r = run_virtual(main)
    assert r["state"] == "online"
    assert r["pending"] == 0
    assert r["rejected"] == 0 and r["busiest"] <= 5
    assert not r["mism"], r
