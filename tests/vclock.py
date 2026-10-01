"""Virtual time for fast, deterministic simulations.

`run_virtual(coro)` runs a coroutine on an event loop whose clock only advances when every task is
waiting - so "30 minutes" of lighting + audio activity costs seconds of CPU. `time.monotonic` is patched
to the same clock, so scheduler / simulator / audio code (which read it directly) stay consistent.

SIMULATOR VERIFIED only: this proves logic and budgets, never real-hardware behaviour.
"""
from __future__ import annotations

import asyncio
import selectors
import time


class VirtualClock:
    def __init__(self, start: float = 1000.0):
        self.t = start


class _FastForwardSelector(selectors.DefaultSelector):
    def __init__(self, clock: VirtualClock):
        super().__init__()
        self._clock = clock

    def select(self, timeout=None):
        events = super().select(0)
        if events:
            return events
        if timeout is None:                      # nothing scheduled: wait briefly for real I/O / threads
            return super().select(0.005)
        if timeout > 0:
            self._clock.t += timeout
        return []


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self, clock: VirtualClock):
        super().__init__(_FastForwardSelector(clock))
        self._vclock = clock

    def time(self) -> float:
        return self._vclock.t


def run_virtual(coro_fn, *, start: float = 1000.0):
    """coro_fn: a zero-arg callable returning a coroutine (built after the clock is patched)."""
    clock = VirtualClock(start)
    real = time.monotonic
    time.monotonic = lambda: clock.t           # type: ignore[assignment]
    loop = VirtualLoop(clock)
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()
            asyncio.set_event_loop(None)
            time.monotonic = real
