"""Scheduler behaviour against the simulated gateway.  SIMULATOR VERIFIED."""
import asyncio
import time

from harness import settle, started, truth_levels
from vclock import run_virtual

from core.model import DP_BRIGHT, DP_CCT, DP_ON
from core.scheduler import Prio


def test_rate_limit_is_respected_when_saturated(cfg):
    async def main():
        rt = await started(cfg)
        s = rt.sched
        n0, t0 = rt.sim.accepted, time.monotonic()
        i = 0
        while time.monotonic() - t0 < 20:
            for f in rt.layout.fixtures:
                s.set_light(f, 20 + ((i * 7 + hash(f)) % 5) * 15)
            i += 1
            await asyncio.sleep(0.1)
        dt = time.monotonic() - t0
        rate = (rt.sim.accepted - n0) / dt
        out = (rate, rt.sim.rejected, rt.sim.max_window_count)
        await rt.shutdown()
        return out

    rate, rejected, busiest = run_virtual(main)
    assert rate <= 4.3, rate                      # global budget
    assert rate >= 3.0, rate                      # and it really uses it
    assert rejected == 0                          # the simulated gateway never had to refuse anything
    assert busiest <= 5


def test_coalescing_keeps_only_the_latest_value(cfg):
    async def main():
        rt = await started(cfg)
        s = rt.sched
        await settle(rt)
        before = rt.sim.accepted
        for v in (20, 30, 40, 50, 60, 70):
            s.set_light("R1", v)
        await settle(rt)
        out = (rt.sim.accepted - before, truth_levels(rt)["R1"], s.stats.coalesced)
        await rt.shutdown()
        return out

    sent, truth, coalesced = run_virtual(main)
    assert sent <= 2                              # not six
    assert abs(truth[1] - 70) < 1.5
    assert coalesced >= 4


def test_priority_critical_overtakes_queued_normal(cfg):
    async def main():
        rt = await started(cfg)
        s = rt.sched
        await settle(rt)
        rt.sim.log.clear()
        for f in ("R1", "R2", "R3", "LFT1", "LFT2"):
            s.set_light(f, 11, prio=Prio.NORMAL)
        s.set_light("LFT3", 77, prio=Prio.CRITICAL)
        await settle(rt)
        order = [(c, dp) for _, c, dp, _ in rt.sim.log]
        cid = rt.layout.fixtures["LFT3"].cid
        pos = next(i for i, (c, dp) in enumerate(order) if c == cid and dp == DP_BRIGHT)
        await rt.shutdown()
        return pos

    assert run_virtual(main) <= 1                 # sent first (or right behind the command already in flight)


def test_ttl_expires_stale_commands(cfg):
    async def main():
        rt = await started(cfg)
        s = rt.sched
        await settle(rt)
        for f in rt.layout.fixtures:              # far more than can be sent in 0.5 s
            s.set_light(f, 5, ttl=0.5)
            s.set_light(f, 95, ttl=0.5)
        await asyncio.sleep(6)
        out = s.stats.dropped_stale
        await rt.shutdown()
        return out

    assert run_virtual(main) > 0


def test_stale_epoch_is_rejected_and_new_epoch_flushes_queue(cfg):
    async def main():
        rt = await started(cfg)
        s = rt.sched
        await settle(rt)
        old = s.epoch
        assert s.set_light("R1", 80, epoch=old, prio=Prio.NORMAL)
        s.new_epoch()
        accepted_after = s.set_light("R1", 10, epoch=old)          # an old effect still running
        flushed = not s.pending
        await rt.shutdown()
        return accepted_after, flushed

    accepted_after, flushed = run_virtual(main)
    assert accepted_after is False
    assert flushed


def test_off_is_one_command_and_on_restores_brightness_order(cfg):
    async def main():
        rt = await started(cfg)
        s = rt.sched
        await settle(rt)
        rt.sim.log.clear()
        s.set_light("R2", 0)
        await settle(rt)
        offs = [(dp, v) for _, c, dp, v in rt.sim.log]
        rt.sim.log.clear()
        s.set_light("R2", 50)
        await settle(rt)
        ons = [dp for _, c, dp, v in rt.sim.log]
        await rt.shutdown()
        return offs, ons

    offs, ons = run_virtual(main)
    assert offs == [(DP_ON, False)]
    assert ons[0] == DP_ON and DP_BRIGHT in ons


def test_capture_restore_is_raw_exact(cfg):
    async def main():
        rt = await started(cfg)
        s = rt.sched
        # a level below the nominal minimum (like the real L6 at raw 5) must survive a round trip
        rt.sim.lamps[rt.layout.fixtures["LFT1"].cid][DP_BRIGHT] = 5
        await s.adopt_from_gateway()
        s.adopt_known_as_desired()
        snap = s.capture()
        for f in rt.layout.fixtures:
            s.set_light(f, 90, cct=0.9)
        await settle(rt)
        s.restore(snap)
        await settle(rt)
        out = {fid: dict(rt.sim.lamps[f.cid]) for fid, f in rt.layout.fixtures.items()}
        await rt.shutdown()
        return snap, out

    snap, truth = run_virtual(main)
    for fid, s in snap.items():
        assert truth[fid][DP_BRIGHT] == s["bright_raw"], fid
        assert truth[fid][DP_CCT] == s["cct_raw"], fid
        assert truth[fid][DP_ON] == s["on"], fid
    assert snap["LFT1"]["bright_raw"] == 5


def test_commands_wait_through_an_outage_and_apply_after_reconnect(cfg):
    async def main():
        rt = await started(cfg)
        await settle(rt)
        rt.sim.disconnect(25)
        await asyncio.sleep(1)
        rt.sched.set_light("R3", 66)
        mid_state = rt.link.state
        await asyncio.sleep(90)
        out = (mid_state, rt.link.state, truth_levels(rt)["R3"], rt.link.reconnects)
        await rt.shutdown()
        return out

    mid, end, truth, reconnects = run_virtual(main)
    assert end == "online"
    assert reconnects >= 2                        # the real supervisor reconnected
    assert abs(truth[1] - 66) < 1.5               # and the queued command was delivered
