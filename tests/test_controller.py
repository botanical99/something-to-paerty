"""Controller: modes, STOP/restore, NORMAL, shutdown, crash recovery.  SIMULATOR VERIFIED."""
import asyncio
import json

from harness import settle, started, truth_levels
from vclock import run_virtual

from core.model import DP_BRIGHT


def test_stop_restores_the_exact_pre_party_look(cfg):
    async def main():
        rt = await started(cfg)
        await rt.ctl.apply_scene("CHILL")
        await settle(rt)
        before = {c: dict(v) for c, v in rt.sim.lamps.items()}
        await rt.ctl.start_effect("pulse")
        await asyncio.sleep(40)
        assert rt.ctl.mode == "effect"
        await rt.ctl.stop(restore=True)
        await settle(rt)
        after = {c: dict(v) for c, v in rt.sim.lamps.items()}
        out = (before, after, rt.ctl.mode, rt.ctl.snapshot)
        await rt.shutdown()
        return out

    before, after, mode, snap = run_virtual(main)
    assert before == after
    assert mode == "idle" and snap is None


def test_normal_cancels_everything_immediately(cfg):
    async def main():
        rt = await started(cfg)
        await rt.ctl.start_effect("alternate")
        await asyncio.sleep(12)
        await rt.ctl.normal()
        await settle(rt, 60)
        out = (rt.ctl.mode, rt.ctl.scene, truth_levels(rt), rt.ctl._task)
        await asyncio.sleep(20)                   # nothing may fight NORMAL afterwards
        out2 = truth_levels(rt)
        await rt.shutdown()
        return out, out2

    (mode, scene, levels, task), later = run_virtual(main)
    assert mode == "scene" and scene == "NORMAL" and task is None
    assert all(on and abs(v - 35) < 1.5 for on, v in levels.values()), levels
    assert later == levels


def test_effect_switch_never_lets_two_effects_fight(cfg):
    async def main():
        rt = await started(cfg)
        await rt.ctl.start_effect("chase")
        await asyncio.sleep(5)
        t1 = rt.ctl._task
        await rt.ctl.start_effect("spark")
        await asyncio.sleep(1)
        out = (t1.done(), rt.ctl.name, rt.sched.epoch)
        await rt.ctl.stop()
        await rt.shutdown()
        return out

    done, name, epoch = run_virtual(main)
    assert done and name == "spark" and epoch >= 2


def test_manual_tap_stops_animation_without_restoring(cfg):
    async def main():
        rt = await started(cfg)
        await rt.ctl.start_effect("chase")
        await asyncio.sleep(10)
        await rt.ctl.set_fixture("R2", 77, 0.2)
        await settle(rt)
        out = (rt.ctl.mode, truth_levels(rt)["R2"], rt.ctl._task)
        await rt.shutdown()
        return out

    mode, r2, task = run_virtual(main)
    assert mode == "manual" and task is None and abs(r2[1] - 77) < 1.5


def test_fixture_on_off_remembers_level(cfg):
    async def main():
        rt = await started(cfg)
        await rt.ctl.set_fixture("R1", 60, None)
        await settle(rt)
        await rt.ctl.set_fixture("R1", None, None, on=False)
        await settle(rt)
        off = truth_levels(rt)["R1"]
        await rt.ctl.set_fixture("R1", None, None, on=True)
        await settle(rt)
        on = truth_levels(rt)["R1"]
        await rt.shutdown()
        return off, on

    off, on = run_virtual(main)
    assert off[0] is False
    assert on[0] is True and abs(on[1] - 60) < 1.5


def test_shutdown_during_party_restores_the_room(cfg):
    async def main():
        rt = await started(cfg)
        await rt.ctl.apply_scene("CINEMA")
        await settle(rt)
        before = {c: dict(v) for c, v in rt.sim.lamps.items()}
        await rt.ctl.start_effect("breathing")
        await asyncio.sleep(30)
        await rt.shutdown()
        return before, {c: dict(v) for c, v in rt.sim.lamps.items()}

    before, after = run_virtual(main)
    assert {c: v["20"] for c, v in before.items()} == {c: v["20"] for c, v in after.items()}   # on/off exact
    for c, b in before.items():                    # lamps that were on come back exactly; an OFF lamp is just put back off
        if b["20"]:
            assert after[c] == b


def test_unclean_exit_leaves_a_snapshot_that_can_be_restored(cfg):
    async def first():
        rt = await started(cfg)
        await rt.ctl.apply_scene("CHILL")
        await settle(rt)
        await rt.ctl.start_effect("chase")
        await asyncio.sleep(10)
        # simulate a crash: no shutdown(), the file stays behind
        return json.loads((cfg / "runtime_state.json").read_text())

    state = run_virtual(first)
    assert state["snapshot"] and len(state["snapshot"]) == 6

    async def second():
        rt = await started(cfg)
        had = rt.ctl.stale_snapshot is not None
        # the lamps were left mid-chase by the "crash"
        for c in rt.sim.lamps.values():
            c[DP_BRIGHT] = 900
        await rt.sched.adopt_from_gateway()
        rt.sched.adopt_known_as_desired()
        ok = rt.ctl.restore_last()
        await settle(rt)
        out = (had, ok, truth_levels(rt), (cfg / "runtime_state.json").exists())
        await rt.shutdown()
        return out

    had, ok, levels, file_left = run_virtual(second)
    assert had and ok and not file_left
    assert all(abs(v - 18) < 1.5 for _, v in levels.values()), levels


def test_music_unavailable_start_fails_cleanly_and_restores(cfg):
    async def main():
        rt = await started(cfg)
        await rt.ctl.apply_scene("CHILL")
        await settle(rt)
        try:
            await rt.ctl.start_music({"device": "99999"})          # a device that cannot be opened
            started_ok = True
        except RuntimeError:
            started_ok = False
        await settle(rt)
        out = (started_ok, rt.ctl.mode, truth_levels(rt))
        await rt.shutdown()
        return out

    ok, mode, levels = run_virtual(main)
    assert not ok and mode != "music"
    assert all(abs(v - 18) < 1.5 for _, v in levels.values())
