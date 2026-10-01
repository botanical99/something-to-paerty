"""Effects engine.

Designed around the measured hardware budget (~4 gateway commands/s for 6 lamps):
  * step effects change only the lamps that must change (1-4 commands per step) and the step
    length is automatically stretched so a step's commands fit the budget;
  * analog effects (pulse / breathing) describe a target curve; the scheduler sends the
    biggest visible differences first and skips imperceptible ones (quantised by bandwidth);
  * every effect runs in its own asyncio task bound to a scheduler epoch - when the epoch moves
    on (STOP / NORMAL / another effect) the old task is rejected and cancelled.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from core.model import Layout, level_to_raw
from core.scheduler import Prio, Scheduler

DEFAULT_PARAMS = {
    "speed": 50,            # 0..100
    "intensity": 70,        # 0..100 contrast between highlight and base
    "min_brightness": 8,    # % (base / dim level)
    "max_brightness": 90,   # % (highlight level)
    "cct_warm": 0.05,       # 0 = warmest .. 1 = coolest  (lower end of the range used)
    "cct_cool": 0.65,       # (upper end of the range used)
    "direction": "forward", # forward | reverse | random
    "pattern": "left_right",  # ALTERNATE: left_right | odd_even
    "fixtures": None,       # None = all, or list of fixture ids
}


class EffectContext:
    def __init__(self, layout: Layout, sched: Scheduler, epoch: int, params: dict):
        self.layout, self.sched, self.epoch = layout, sched, epoch
        self.params = params          # live: the controller mutates this dict while the effect runs
        self._next = time.monotonic()
        self._primed = False

    # ---- liveness
    @property
    def alive(self) -> bool:
        return self.epoch == self.sched.epoch

    # ---- parameters (re-read every step so sliders act live)
    def p(self, k):
        return self.params.get(k, DEFAULT_PARAMS.get(k))

    @property
    def lo(self) -> float:
        return float(min(self.p("min_brightness"), self.p("max_brightness") - 1))

    @property
    def hi(self) -> float:
        lo, mx = self.lo, float(self.p("max_brightness"))
        return lo + (mx - lo) * float(self.p("intensity")) / 100.0

    @property
    def cct_mid(self) -> float:
        return (float(self.p("cct_warm")) + float(self.p("cct_cool"))) / 2

    @property
    def cct_a(self) -> float:      # warm end of the chosen range
        return float(self.p("cct_warm"))

    @property
    def cct_b(self) -> float:      # cool end
        return float(self.p("cct_cool"))

    def base_period(self) -> float:
        """speed 0 -> 3.0 s per step, speed 100 -> ~0.36 s (before budget clamping)."""
        return 3.0 * (0.12 ** (float(self.p("speed")) / 100.0))

    def step_period(self, commands: int, base: float | None = None) -> float:
        """Never schedule steps faster than the gateway budget can deliver their commands."""
        floor = commands / self.sched.rate * 1.1
        return max(base if base is not None else self.base_period(), floor)

    # ---- topology helpers
    def selected(self) -> list[str]:
        sel = self.p("fixtures")
        return [f for f in self.layout.ring if not sel or f in sel]

    def ring(self) -> list[str]:
        r = self.selected()
        return r

    def pairs(self) -> list[list[str]]:
        sel = set(self.selected())
        out = [[f for f in self.layout.pairs[k] if f in sel] for k in ("bed", "middle", "door")]
        return [p for p in out if p]

    def sign(self) -> int:
        d = self.p("direction")
        if d == "reverse":
            return -1
        if d == "random":
            return random.choice((-1, 1))
        return 1

    # ---- output
    def set(self, fid: str, level: float | None = None, cct: float | None = None, *,
            prio: Prio = Prio.NORMAL, ttl: float | None = None, analog: bool = False) -> bool:
        return self.sched.set_light(fid, level, cct, prio=prio, ttl=ttl, epoch=self.epoch, analog=analog,
                                    tag="fx")

    def set_all(self, level: float | None, cct: float | None, fids: list[str] | None = None, **kw) -> None:
        for f in fids or self.selected():
            self.set(f, level, cct, **kw)

    def drive(self, targets: dict[str, tuple[float | None, float | None]], max_updates: int | None = None) -> None:
        """Analog update: submit the targets with the largest visible error first (optionally only N)."""
        scored = []
        for fid, (lv, cc) in targets.items():
            cid = self.layout.fixtures[fid].cid
            kb = self.sched.known.get(cid, {}).get("22")
            err = abs(level_to_raw(lv) - kb) if (lv is not None and isinstance(kb, (int, float))) else 1000
            scored.append((err, fid, lv, cc))
        scored.sort(reverse=True)
        for err, fid, lv, cc in scored[:max_updates]:
            self.set(fid, lv, cc, analog=True)

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(max(0.0, s))

    async def step(self, period: float) -> None:
        """Drift-free metronome: sleeps until the next step boundary (never bursts to catch up)."""
        now = time.monotonic()
        if not self._primed:
            self._next, self._primed = now, True
        self._next += period
        if self._next < now:
            self._next = now
        await asyncio.sleep(self._next - now)

    async def settle(self, timeout: float = 6.0) -> None:
        """Wait (bounded) until the base look has actually been delivered."""
        try:
            await asyncio.wait_for(self.sched.idle_event.wait(), timeout)
        except asyncio.TimeoutError:
            pass


# ======================================================================= effects
async def chase(ctx: EffectContext) -> None:
    """Bed-right -> middle-right -> door-right -> door-left -> middle-left -> bed-left, repeat."""
    ring = ctx.ring()
    ctx.set_all(ctx.lo, ctx.cct_mid)
    await ctx.settle()
    i, prev = -1, None
    while ctx.alive:
        ring = ctx.ring()
        i = (i + ctx.sign()) % len(ring)
        head = ring[i]
        ctx.set(head, ctx.hi, ctx.cct_mid)
        if prev and prev != head and prev in ring:
            ctx.set(prev, ctx.lo)
        prev = head
        await ctx.step(ctx.step_period(2))


async def mirror_chase(ctx: EffectContext) -> None:
    """L5+L6 -> L1+L2 -> L3+L4 and back. Pairs cost 4 commands per step."""
    ctx.set_all(ctx.lo, ctx.cct_mid)
    await ctx.settle()
    idx, prev = -1, None
    while ctx.alive:
        pairs = ctx.pairs()
        if not pairs:
            await ctx.sleep(1)
            continue
        n = len(pairs)
        path = list(range(n)) + list(range(n - 2, 0, -1))      # 0,1,2,1 for three pairs
        idx = (idx + ctx.sign()) % len(path)
        cur = path[idx]
        for f in pairs[cur]:
            ctx.set(f, ctx.hi, ctx.cct_mid)
        if prev is not None and prev != cur and prev < n:
            for f in pairs[prev]:
                ctx.set(f, ctx.lo)
        prev = cur
        await ctx.step(ctx.step_period(4))


async def ping_pong(ctx: EffectContext) -> None:
    """One light travels bed -> door -> bed along the ring path without wrapping."""
    ctx.set_all(ctx.lo, ctx.cct_mid)
    await ctx.settle()
    pos, d, prev = 0, 1, None
    while ctx.alive:
        ring = ctx.ring()
        if ctx.p("direction") == "reverse":
            ring = ring[::-1]
        head = ring[min(pos, len(ring) - 1)]
        ctx.set(head, ctx.hi, ctx.cct_mid)
        if prev and prev != head:
            ctx.set(prev, ctx.lo)
        prev = head
        if pos + d >= len(ring) or pos + d < 0:
            d = -d
        pos += d
        await ctx.step(ctx.step_period(2))


async def pulse(ctx: EffectContext) -> None:
    """Room pulse with a slight travelling offset; steps are quantised by the scheduler."""
    ctx.set_all(ctx.lo, ctx.cct_mid)
    await ctx.settle()
    t0 = time.monotonic()
    while ctx.alive:
        ring = ctx.ring()
        period = max(2.5, ctx.base_period() * 4)      # one pulse cycle
        t = time.monotonic() - t0
        tg = {}
        for k, f in enumerate(ring):
            ph = (t / period) - 0.10 * k
            tg[f] = (ctx.lo + (ctx.hi - ctx.lo) * (0.5 - 0.5 * math.cos(2 * math.pi * ph)), ctx.cct_mid)
        ctx.drive(tg)
        await ctx.sleep(0.25)


async def warm_cool_travel(ctx: EffectContext) -> None:
    """A cool-white 'head' travels round a warm room."""
    mid = (ctx.lo + ctx.hi) / 2
    ctx.set_all(mid, ctx.cct_a)
    await ctx.settle()
    i, prev = -1, None
    while ctx.alive:
        ring = ctx.ring()
        i = (i + ctx.sign()) % len(ring)
        head = ring[i]
        ctx.set(head, None, ctx.cct_b)
        if prev and prev != head and prev in ring:
            ctx.set(prev, None, ctx.cct_a)
        prev = head
        await ctx.step(ctx.step_period(2, ctx.base_period() * 1.2))


async def spark(ctx: EffectContext) -> None:
    """Random fixtures flash briefly and return."""
    ctx.set_all(ctx.lo, ctx.cct_mid)
    await ctx.settle()
    last = None
    while ctx.alive:
        ring = ctx.ring()
        n = 2 if float(ctx.p("intensity")) > 75 and len(ring) > 3 else 1
        picks = random.sample([f for f in ring if f != last] or ring, min(n, len(ring)))
        for f in picks:
            ctx.set(f, ctx.hi, ttl=0.45)
        await ctx.sleep(0.42)
        for f in picks:
            ctx.set(f, ctx.lo, prio=Prio.HIGH)         # the return must never starve
        last = picks[-1]
        gap = ctx.step_period(2 * n, ctx.base_period() * random.uniform(0.6, 1.5))
        await ctx.sleep(max(0.2, gap - 0.42))


async def build(ctx: EffectContext) -> None:
    """Light up one by one until the whole room is on, hold, collapse, repeat."""
    while ctx.alive:
        ctx.set_all(ctx.lo, ctx.cct_mid)
        await ctx.settle()
        ring = ctx.ring()
        if ctx.p("direction") == "reverse":
            ring = ring[::-1]
        elif ctx.p("direction") == "random":
            ring = random.sample(ring, len(ring))
        for f in ring:
            if not ctx.alive:
                return
            ctx.set(f, ctx.hi, ctx.cct_mid)
            await ctx.step(ctx.step_period(1))
        await ctx.sleep(ctx.base_period() * 2)          # hold fully lit
        ctx.set_all(ctx.lo, None)                        # collapse
        await ctx.settle(8)
        await ctx.sleep(ctx.base_period())


async def alternate(ctx: EffectContext) -> None:
    """Left/right (or odd/even) swap. Six commands per flip, so flips are never faster than ~1.7 s."""
    ctx.set_all(ctx.lo, ctx.cct_mid)
    await ctx.settle()
    state = False
    while ctx.alive:
        ring = ctx.ring()
        if ctx.p("pattern") == "odd_even":
            a = [f for k, f in enumerate(ring) if k % 2 == 0]
        else:
            a = [f for f in ring if ctx.layout.fixtures[f].track == "RIGHT"]
        b = [f for f in ring if f not in a]
        up, down = (a, b) if state else (b, a)
        for f in up:
            ctx.set(f, ctx.hi, ctx.cct_mid)
        for f in down:
            ctx.set(f, ctx.lo)
        state = not state
        await ctx.step(ctx.step_period(len(ring), ctx.base_period() * 1.5))


async def breathing_room(ctx: EffectContext) -> None:
    """Slow architectural movement: brightness and colour temperature drift gently around the room."""
    t0 = time.monotonic()
    while ctx.alive:
        ring = ctx.ring()
        period = 14 + 40 * (1 - float(ctx.p("speed")) / 100.0)      # 14..54 s per breath
        t = time.monotonic() - t0
        lo, hi = ctx.lo, ctx.lo + (float(ctx.p("max_brightness")) - ctx.lo) * 0.7
        tg = {}
        for k, f in enumerate(ring):
            ph = t / period - k / (2.0 * len(ring))
            lv = lo + (hi - lo) * (0.5 - 0.5 * math.cos(2 * math.pi * ph))
            cc = ctx.cct_a + (ctx.cct_b - ctx.cct_a) * (0.5 - 0.5 * math.cos(2 * math.pi * (ph * 0.5 + 0.25)))
            tg[f] = (lv, cc)
        ctx.drive(tg, max_updates=1)             # leaves most of the budget free for the phone app / manual taps
        await ctx.sleep(0.6)


@dataclass(frozen=True)
class EffectInfo:
    name: str
    label: str
    description: str
    cmds_per_step: int
    run: Callable[[EffectContext], Awaitable[None]]
    defaults: dict


EFFECTS: dict[str, EffectInfo] = {e.name: e for e in [
    EffectInfo("chase", "Chase", "One light travels bed-right to door-right, across, and back down the left.", 2, chase, {}),
    EffectInfo("mirror", "Mirror chase", "Pairs L5+L6, L1+L2, L3+L4 and back.", 4, mirror_chase, {"speed": 35}),
    EffectInfo("pingpong", "Ping pong", "Bounces along the ring without wrapping.", 2, ping_pong, {}),
    EffectInfo("pulse", "Pulse", "Room brightness swells and fades, quantised to the gateway budget.", 2, pulse, {"speed": 40}),
    EffectInfo("warmcool", "Warm / cool", "A cool-white head travels around a warm room.", 2, warm_cool_travel, {"intensity": 50}),
    EffectInfo("spark", "Spark", "Random fixtures flash and return.", 2, spark, {"speed": 55, "min_brightness": 5}),
    EffectInfo("build", "Build", "Lights come on one by one, hold, collapse.", 1, build, {}),
    EffectInfo("alternate", "Alternate", "Left/right or odd/even swap.", 6, alternate, {"speed": 30}),
    EffectInfo("breathing", "Breathing room", "Slow, premium brightness + colour drift.", 1, breathing_room,
               {"speed": 30, "min_brightness": 12, "max_brightness": 60, "intensity": 100}),
]}


def effect_defaults(name: str) -> dict:
    return {**DEFAULT_PARAMS, **EFFECTS[name].defaults}
