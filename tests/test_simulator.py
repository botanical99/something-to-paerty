"""The simulator itself, so we can trust it as a test instrument.  SIMULATOR VERIFIED."""
import asyncio

import pytest
from vclock import run_virtual

from hardware.simulator import DP_BRIGHT, SimGateway, SimLink


def test_gateway_rejects_commands_beyond_its_rate_ceiling():
    async def main():
        g = SimGateway(["a"], latency_ms=0, realtime=False, max_per_window=5)
        res = []
        for _ in range(9):
            try:
                await g.write("a", DP_BRIGHT, 100)
                res.append("ok")
            except OSError:
                res.append("rejected")
            await asyncio.sleep(0.05)
        return res, g.rejected

    res, rej = run_virtual(main)
    assert res.count("ok") >= 5 and rej >= 1


def test_latency_and_serialisation():
    async def main():
        g = SimGateway(["a", "b"], latency_ms=200, jitter=0.0)
        t0 = asyncio.get_running_loop().time()
        await asyncio.gather(g.write("a", DP_BRIGHT, 1), g.write("b", DP_BRIGHT, 2))
        return asyncio.get_running_loop().time() - t0

    assert run_virtual(main) == pytest.approx(0.4, abs=0.01)         # one socket: replies queue up


def test_disconnect_and_reconnect_through_the_real_supervisor():
    async def main():
        g = SimGateway(["a"], latency_ms=10)
        link = SimLink(g)
        states = []
        link.on_state = states.append
        link.start()
        await asyncio.sleep(3)
        assert link.state == "online"
        g.disconnect(20)
        for _ in range(3):
            try:
                await link.write("a", DP_BRIGHT, 5)
            except Exception:  # noqa: BLE001
                pass
        mid = link.state
        await asyncio.sleep(60)
        end = link.state
        await link.write("a", DP_BRIGHT, 123)
        await link.close()
        return states, mid, end, g.lamps["a"][DP_BRIGHT], link.reconnects

    states, mid, end, value, reconnects = run_virtual(main)
    assert "offline" in states and "connecting" in states
    assert mid == "offline" and end == "online" and value == 123 and reconnects == 2


def test_state_is_what_was_written():
    async def main():
        g = SimGateway(["a"], latency_ms=0, realtime=False)
        await g.write("a", DP_BRIGHT, 777)
        return await g.read("a")

    assert run_virtual(main)[DP_BRIGHT] == 777
