"""Run effects on the REAL lights for N seconds each, print scheduler stats, restore the room.

    python tools/effect_test.py chase,mirror --seconds 20 [--speed 30] [--intensity 70] [--direction reverse]
"""
import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.effects import EFFECTS, EffectContext, effect_defaults  # noqa: E402
from core.model import Layout  # noqa: E402
from core.scheduler import Prio, Scheduler  # noqa: E402
from hardware.gateway_link import AsyncGatewayLink  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("effects")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--lead", type=float, default=0, help="seconds to wait before each effect starts")
    for k in ("speed", "intensity", "min_brightness", "max_brightness"):
        ap.add_argument(f"--{k.replace('_', '-')}", type=float)
    ap.add_argument("--direction")
    ap.add_argument("--pattern")
    a = ap.parse_args()

    lay = Layout.load()
    link = AsyncGatewayLink(lay.gateway_ip, lay.gateway_version)
    sch = Scheduler(lay, link, rate=4.0)
    link.start()
    for _ in range(120):
        if link.state == "online":
            break
        await asyncio.sleep(0.3)
    assert link.state == "online", "gateway offline"
    await sch.adopt_from_gateway()
    sch.adopt_known_as_desired()
    original = sch.capture()
    sch.start()
    try:
        for name in a.effects.split(","):
            info = EFFECTS[name]
            params = effect_defaults(name)
            for k in ("speed", "intensity", "min_brightness", "max_brightness", "direction", "pattern"):
                v = getattr(a, k, None)
                if v is not None:
                    params[k] = v
            if a.lead:
                print(f"... {name} starts in {a.lead:.0f}s", flush=True)
                await asyncio.sleep(a.lead)
            s0 = (sch.stats.sent, sch.stats.dropped_stale, sch.stats.coalesced, sch.stats.failed)
            epoch = sch.new_epoch()
            ctx = EffectContext(lay, sch, epoch, params)
            print(f"== {info.label}  ({a.seconds:.0f}s)  params={ {k: params[k] for k in ('speed','intensity','direction')} }", flush=True)
            task = asyncio.create_task(info.run(ctx))
            await asyncio.sleep(a.seconds)
            sch.new_epoch()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            d = (sch.stats.sent - s0[0], sch.stats.dropped_stale - s0[1], sch.stats.coalesced - s0[2], sch.stats.failed - s0[3])
            print(f"   sent={d[0]} ({d[0] / a.seconds:.2f}/s) dropped_stale={d[1]} coalesced={d[2]} failed={d[3]} "
                  f"| ack_ms median={sch.stats.public()['ack_ms_median']} queue_ms median={sch.stats.public()['queue_ms_median']} "
                  f"p95={sch.stats.public()['queue_ms_p95']}", flush=True)
    finally:
        print("restoring room", flush=True)
        sch.new_epoch()
        sch.restore(original, Prio.CRITICAL)
        await asyncio.sleep(0.5)
        try:
            await asyncio.wait_for(sch.idle_event.wait(), 25)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(1.0)
        await sch.adopt_from_gateway()
        sch.desired.clear()
        print("restored raw-exact:", sch.capture() == original, flush=True)
        await sch.stop()
        await link.close()


asyncio.run(main())
