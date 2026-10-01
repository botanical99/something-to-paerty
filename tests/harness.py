"""Builds the whole lighting stack on the simulator for tests."""
from __future__ import annotations

from pathlib import Path

from api.server import Runtime
from core.settings import Settings


def make_runtime(cfg: Path, *, latency_ms: float = 200.0, **kw) -> Runtime:
    s = Settings.load(cfg, simulate=True, sim_latency_ms=latency_ms, log_dir=cfg / "logs", **kw)
    s.require_pin = False
    return Runtime(s)


async def started(cfg: Path, **kw) -> Runtime:
    rt = make_runtime(cfg, **kw)
    await rt.start()
    await wait_online(rt)
    return rt


async def wait_online(rt: Runtime, timeout: float = 30.0) -> None:
    import asyncio
    import time
    t0 = time.monotonic()
    while rt.link.state != "online":
        if time.monotonic() - t0 > timeout:
            raise TimeoutError("simulated gateway never came online")
        await asyncio.sleep(0.1)
    await asyncio.sleep(1.0)        # let on_gateway_online sync finish


async def settle(rt: Runtime, timeout: float = 60.0) -> None:
    """Wait until every queued command has been delivered."""
    import asyncio
    import time
    t0 = time.monotonic()
    while rt.sched.pending or not rt.sched.idle_event.is_set():
        if time.monotonic() - t0 > timeout:
            raise TimeoutError(f"scheduler never drained ({len(rt.sched.pending)} pending)")
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)


def truth_levels(rt: Runtime) -> dict[str, tuple[bool, float]]:
    st = rt.sim_state()
    return {k: (v["on"], round(v["level"], 1)) for k, v in st["fixtures"].items()}
