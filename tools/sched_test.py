"""Real-hardware test of the scheduler: coalescing, stale-dropping, priority, rate. Restores all lights.

    python tools/sched_test.py
"""
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.model import Layout  # noqa: E402
from core.scheduler import Prio, Scheduler  # noqa: E402
from hardware.gateway_link import AsyncGatewayLink  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


async def main():
    lay = Layout.load()
    link = AsyncGatewayLink(lay.gateway_ip, lay.gateway_version)
    sch = Scheduler(lay, link, rate=4.0)
    link.start()
    for _ in range(100):
        if link.state == "online":
            break
        await asyncio.sleep(0.3)
    assert link.state == "online", "gateway did not come online"
    await sch.adopt_from_gateway()
    sch.adopt_known_as_desired()
    original = sch.capture()
    print("original:", original)
    sch.start()
    log = []
    orig_write = link.write

    async def spy(cid, dp, value):
        log.append((time.monotonic(), lay.cid_to_fid[cid], dp, value))
        return await orig_write(cid, dp, value)
    link.write = spy

    try:
        print("\nT1 coalescing: R2 level 20,40,70 within one tick -> expect ONE brightness write (70)")
        n0 = len(log)
        for lv in (20, 40, 70):
            sch.set_light("R2", lv)
        await asyncio.sleep(1.5)
        sent = [(f, dp, v) for _, f, dp, v in log[n0:]]
        print("   sent:", sent, "coalesced counter:", sch.stats.coalesced)

        print("\nT2 stale dropping: 12 commands with 0.4 s TTL (budget allows ~2) -> rest dropped")
        n0, d0 = len(log), sch.stats.dropped_stale
        for i, f in enumerate(["R1", "R2", "R3", "LFT1", "LFT2", "LFT3"] * 2):
            sch.set_light(f, 10 + 8 * i, ttl=0.4, tag="burst")
        await asyncio.sleep(2.0)
        print(f"   sent {len(log) - n0}, dropped_stale +{sch.stats.dropped_stale - d0}")

        print("\nT3 priority: queue 6 NORMAL writes then 1 CRITICAL -> CRITICAL must be among the first sent")
        n0 = len(log)
        for f in ["R1", "R2", "R3", "LFT1", "LFT2", "LFT3"]:
            sch.set_light(f, 35, prio=Prio.NORMAL, tag="anim")
        sch.set_light("LFT2", 55, prio=Prio.CRITICAL, tag="critical")
        await asyncio.sleep(3.0)
        order = [(f, v) for _, f, dp, v in log[n0:] if dp == "22"]
        print("   brightness write order:", order)

        print("\nT4 epoch: effect holding an old epoch must be rejected")
        e = sch.epoch
        sch.new_epoch()
        print("   old-epoch write accepted?", sch.set_light("R1", 50, epoch=e), "(expect False)")

        print("\nT5 saturated rate: all 6 lights re-targeted every 100 ms for 12 s (queue never empty)")
        n0, t0 = len(log), time.monotonic()
        i = 0
        while time.monotonic() - t0 < 12:
            for k, f in enumerate(["R1", "R2", "R3", "LFT1", "LFT2", "LFT3"]):
                sch.set_light(f, 20 + ((i + k) % 4) * 15)
            i += 1
            await asyncio.sleep(0.1)
        dt = time.monotonic() - t0
        print(f"   sent {len(log) - n0} in {dt:.1f}s  ->  {(len(log) - n0) / dt:.2f}/s ; stats: {sch.stats.public()}")
    finally:
        print("\nrestoring original state")
        sch.new_epoch()
        sch.restore(original, Prio.CRITICAL)
        await asyncio.sleep(0.5)
        try:
            await asyncio.wait_for(sch.idle_event.wait(), 20)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(1.0)
        await sch.adopt_from_gateway()
        sch.desired.clear()
        now = sch.capture()
        ok = now == original
        print("restored to original (raw-exact):", ok)
        if not ok:
            print("  original:", original, "\n  now     :", now)
        await sch.stop()
        await link.close()


asyncio.run(main())
