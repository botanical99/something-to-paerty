"""Simulated gateway + lamps. SIMULATOR ONLY - nothing here has been verified against real hardware.

`SimLink` is an `AsyncGatewayLink` (so the REAL supervisor / reconnect / back-off / failure-streak
code runs unchanged) whose transport is a `SimGateway` instead of tinytuya.

The simulated gateway mimics what was measured on the real WG-S (HARDWARE_DISCOVERY.md):
  * ONE serialised conversation, acknowledgements take ~200 ms (jittered);
  * a hard command-rate ceiling: more than `max_per_window` commands arriving inside `window`
    seconds are NOT acknowledged (silent timeout, like the real gateway) and counted as `rejected`;
  * it can drop off the network (disconnect / reconnect) for a chosen time;
  * lamps keep DP20 on/off, DP22 brightness 10..1000, DP23 colour temperature 0..1000 (1000 = warm).
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import Any

from hardware.gateway_link import AsyncGatewayLink

DP_ON, DP_BRIGHT, DP_CCT = "20", "22", "23"


class SimGateway:
    is_simulated = True

    def __init__(self, cids: list[str], *, latency_ms: float = 200.0, jitter: float = 0.35,
                 max_per_window: int = 5, window: float = 1.0, realtime: bool = True, seed: int = 7):
        self.cids = list(cids)
        self.latency_ms, self.jitter, self.realtime = latency_ms, jitter, realtime
        self.max_per_window, self.window = max_per_window, window
        self.rng = random.Random(seed)
        self.lamps: dict[str, dict[str, Any]] = {
            c: {DP_ON: True, DP_BRIGHT: 350, DP_CCT: 450} for c in self.cids}
        self.reachable = True
        self._back_at = 0.0
        self._arrivals: deque[float] = deque()
        self._lock = asyncio.Lock()
        # statistics (what the tests / UI inspect)
        self.accepted = 0
        self.rejected = 0
        self.failed_unreachable = 0
        self.log: deque[tuple[float, str, str, Any]] = deque(maxlen=5000)
        self.t0 = time.monotonic()
        self.max_window_count = 0

    # ---- control from tests / the UI's simulator panel
    def disconnect(self, seconds: float | None = None) -> None:
        """Drop off the network (forever, or for `seconds`)."""
        self.reachable = False
        self._back_at = time.monotonic() + seconds if seconds else float("inf")

    def reconnect(self) -> None:
        self.reachable = True
        self._back_at = 0.0

    def _check_reachable(self) -> bool:
        if not self.reachable and time.monotonic() >= self._back_at:
            self.reachable = True
        return self.reachable

    async def _ack_delay(self) -> float:
        d = max(0.0, self.latency_ms / 1000.0 * (1 + self.rng.uniform(-self.jitter, self.jitter * 1.6)))
        if self.realtime and d:
            await asyncio.sleep(d)
        return d * 1000.0

    # ---- the gateway's wire behaviour
    async def connect(self) -> list[str]:
        await asyncio.sleep(0.4 if self.realtime else 0.05)
        if not self._check_reachable():
            raise ConnectionError("gateway did not answer (simulated: offline)")
        return list(self.cids)

    async def write(self, cid: str, dp: str, value: Any) -> float:
        async with self._lock:                                  # a single serialised socket
            if not self._check_reachable():
                self.failed_unreachable += 1
                await asyncio.sleep(1.0)                        # shortened version of tinytuya's 5 s timeout
                raise OSError(f"no response for {cid} dp {dp} (simulated: gateway unreachable)")
            now = time.monotonic()
            self._arrivals.append(now)
            while self._arrivals and now - self._arrivals[0] > self.window:
                self._arrivals.popleft()
            self.max_window_count = max(self.max_window_count, len(self._arrivals))
            if len(self._arrivals) > self.max_per_window:
                self.rejected += 1
                await asyncio.sleep(0.5)
                raise OSError(f"no response for {cid} dp {dp} (simulated: command rate exceeded)")
            ms = await self._ack_delay()
            if cid not in self.lamps:
                raise OSError(f"unknown child {cid}")
            self.lamps[cid][dp] = value
            self.accepted += 1
            self.log.append((now - self.t0, cid, dp, value))
            return ms

    async def read(self, cid: str) -> dict:
        async with self._lock:
            if not self._check_reachable():
                self.failed_unreachable += 1
                await asyncio.sleep(1.0)
                raise OSError(f"status failed for {cid} (simulated: gateway unreachable)")
            await self._ack_delay()
            return dict(self.lamps[cid])

    async def close(self) -> None:
        return None

    def truth(self) -> dict[str, dict]:
        return {c: dict(v) for c, v in self.lamps.items()}

    def stats(self) -> dict:
        return {"accepted": self.accepted, "rejected": self.rejected, "unreachable": self.failed_unreachable,
                "reachable": self._check_reachable(), "max_per_window": self.max_window_count,
                "latency_ms": self.latency_ms}


class _SimAdapter:
    """What AsyncGatewayLink expects of GatewayLan, backed by a SimGateway (async instead of threaded)."""

    is_simulated = True

    def __init__(self, sim: SimGateway):
        self.sim = sim
        self.last_command_ms: float | None = None

    async def connect(self, attempts: int = 1) -> list[str]:
        return await self.sim.connect()

    async def close(self) -> None:
        return None

    async def read(self, cid: str) -> dict:
        return await self.sim.read(cid)

    async def write(self, cid: str, dp: str, value) -> float:
        ms = await self.sim.write(cid, dp, value)
        self.last_command_ms = ms
        return ms


class SimLink(AsyncGatewayLink):
    """AsyncGatewayLink with a simulated transport: same supervisor, same state machine."""

    is_simulated = True

    def __init__(self, sim: SimGateway):
        super().__init__("simulator", 3.3)
        self.sim = sim

    def _make_gateway(self):
        return _SimAdapter(self.sim)

    async def _run(self, fn, *a):
        r = fn(*a)
        if asyncio.iscoroutine(r):
            r = await r
        return r

    async def _rediscover(self) -> None:      # nothing to discover in a simulation
        return None
