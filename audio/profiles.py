"""Musical events -> lighting cues, one profile per music style.

Everything here is *event driven* and paced by a command budget; audio frames never reach this module.
Each cue is submitted to the Scheduler (which coalesces, expires stale commands by TTL and enforces the
global 4 commands/s). A cue that doesn't fit the music budget is skipped as a whole - it is never half-sent.
"""
from __future__ import annotations

import math
import random
import time
from typing import TYPE_CHECKING

from audio.analyzer import MusicEvent
from core.effects import EffectContext
from core.model import DP_BRIGHT, DP_CCT, DP_ON, cct_to_raw, level_to_raw
from core.scheduler import Prio

if TYPE_CHECKING:
    from audio.analyzer import MusicAnalyzer


class Budget:
    """Token bucket for lamp commands. Units: gateway commands."""

    def __init__(self, rate: float, burst: float | None = None):
        self.rate = max(0.1, rate)
        self.cap = burst if burst is not None else max(2.0, rate)
        self.tokens = self.cap
        self._t = time.monotonic()
        self.spent = 0.0
        self.denied = 0

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.cap, self.tokens + (now - self._t) * self.rate)
        self._t = now

    def available(self) -> float:
        self._refill()
        return self.tokens

    def take(self, n: float, *, debt: float = 0.0) -> bool:
        """Spend n commands. `debt` lets rare big cues (a drop) borrow against the future."""
        self._refill()
        if self.tokens + debt >= n:
            self.tokens -= n
            self.spent += n
            return True
        self.denied += 1
        return False


class Profile:
    name = "base"
    label = "Base"

    def __init__(self, ctx: EffectContext, budget: Budget, analyzer: "MusicAnalyzer"):
        self.ctx, self.budget, self.an = ctx, budget, analyzer
        self.sched, self.layout = ctx.sched, ctx.layout
        self.cues = 0
        self.skipped = 0
        self.head: str | None = None
        self.pos = -1
        self.flashed: dict[str, float] = {}          # fid -> time it was lifted (so it can be returned)
        self.last_beat_cue = 0.0
        self.beat_n = 0

    # ---- helpers
    def _desired(self, fid: str) -> dict:
        return self.sched.desired.get(self.layout.fixtures[fid].cid, {})

    def cost(self, fid: str, level: float | None, cct: float | None, analog: bool = False) -> int:
        d = self._desired(fid)
        n = 0
        if level is not None:
            if level <= 0:
                n += 0 if d.get(DP_ON) is False else 1
            else:
                if d.get(DP_ON) is not True:
                    n += 1
                raw, cur = level_to_raw(level), d.get(DP_BRIGHT)
                if cur is None or abs(raw - cur) >= (25 if analog else 8):
                    n += 1
        if cct is not None:
            raw, cur = cct_to_raw(cct), d.get(DP_CCT)
            if cur is None or abs(raw - cur) >= (35 if analog else 12):
                n += 1
        return n

    def cue(self, items: list[tuple[str, float | None, float | None]], *, prio: Prio = Prio.NORMAL,
            ttl: float | None = 0.8, analog: bool = False, debt: float = 0.0) -> bool:
        """Send a group of light changes atomically with respect to the budget."""
        total = sum(self.cost(f, lv, cc, analog) for f, lv, cc in items)
        if total == 0:
            return True
        if not self.budget.take(total, debt=debt):
            self.skipped += 1
            return False
        for f, lv, cc in items:
            self.ctx.set(f, lv, cc, prio=prio, ttl=ttl, analog=analog)
        self.cues += 1
        return True

    def base(self) -> tuple[float, float]:
        return self.ctx.lo, self.ctx.cct_mid

    def begin(self) -> None:
        lo, cc = self.base()
        self.ctx.set_all(lo, cc, prio=Prio.HIGH)

    def stride(self, cmds_per_cue: float, bpm: float | None) -> int:
        """Smallest whole number of beats per cue that keeps the cue rate inside the music budget."""
        bpm = bpm or 120.0
        cues_per_s = bpm / 60.0
        return max(1, math.ceil(cues_per_s * cmds_per_cue / (self.budget.rate * 1.04)))

    def ring(self) -> list[str]:
        return self.ctx.ring()

    def step_head(self) -> tuple[str, str | None]:
        ring = self.ring()
        self.pos = (self.pos + self.ctx.sign()) % len(ring)
        head = ring[self.pos]
        prev, self.head = self.head, head
        return head, prev if prev != head else None

    def strength_level(self, s: float) -> float:
        return self.ctx.lo + (self.ctx.hi - self.ctx.lo) * (0.5 + 0.5 * max(0.0, min(1.0, s)))

    def on_event(self, ev: MusicEvent) -> None:
        pass

    def tick(self, now: float) -> None:
        pass

    def idle_return(self, now: float, after: float = 2.2) -> None:
        """If the beat stopped, put the last lit light back to its base look (never leave one stuck on)."""
        if self.head and now - self.last_beat_cue > after:
            lo, _ = self.base()
            if self.cue([(self.head, lo, None)], ttl=1.5, prio=Prio.HIGH, debt=1.0):
                self.head = None


class BeatProfile(Profile):
    """A single light follows the pulse around the ring. Steady, readable, kind to the budget."""
    name, label = "beat", "Beat"

    def on_event(self, ev: MusicEvent) -> None:
        if ev.kind != "beat":
            return
        self.beat_n += 1
        if self.beat_n % self.stride(2, self.an.bpm):
            return
        head, prev = self.step_head()
        lo, _ = self.base()
        tr = self.an.levels["treble"]
        cc = self.ctx.cct_a + (self.ctx.cct_b - self.ctx.cct_a) * min(1.0, 0.25 + tr)
        items: list[tuple[str, float | None, float | None]] = [(head, self.strength_level(ev.strength), cc)]
        if prev:
            items.append((prev, lo, None))
        if self.cue(items, ttl=0.7):
            self.last_beat_cue = time.monotonic()

    def tick(self, now: float) -> None:
        self.idle_return(now)


class ClubProfile(Profile):
    """Punchier: pair pulses on the downbeat, chase steps on the half bars, ripples on drops, lights
    building up through a build, sparks on hats when there is spare budget."""
    name, label = "club", "Club"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.pair_i = -1
        self.pair_prev: list[str] = []
        self.actions: list[tuple[float, str, object]] = []       # (due, kind, arg)
        self.built = 0
        self.last_build = 0.0
        self.last_spark = 0.0
        self.mode = "steady"

    def begin(self) -> None:
        super().begin()
        self.built = 0
        self.actions.clear()

    def _pairs(self) -> list[list[str]]:
        return self.ctx.pairs()

    def on_event(self, ev: MusicEvent) -> None:
        now = time.monotonic()
        k = ev.kind
        if k == "beat":
            self._on_beat(ev, now)
        elif k == "drop":
            self._ripple(up=True)
            self.actions = [a for a in self.actions if a[1] != "return"]
            self.actions.append((now + 1.6, "return", None))
            self.mode = "drop"
            self.built = 0
        elif k == "build":
            self.mode = "build"
            self.built = 0
            self.last_build = now
        elif k == "breakdown":
            self.mode = "breakdown"
            self.actions.append((now + 0.2, "return", None))
        elif k in ("silence",):
            self.actions.append((now, "return", None))
        elif k == "onset_treble" and self.mode in ("steady", "drop") and ev.strength > 0.55:
            self._spark(now)

    def _on_beat(self, ev: MusicEvent, now: float) -> None:
        if self.mode == "build":
            return                                           # the build is told by the ramp, not the pulse
        self.beat_n += 1
        s = self.stride(1.5, self.an.bpm)
        if self.beat_n % s:
            return
        lo, _ = self.base()
        n = self.beat_n // s
        lvl = self.strength_level(ev.strength)
        pairs = self._pairs()
        if n % 4 == 0 and pairs:                              # downbeat: a mirrored pair
            self.pair_i = (self.pair_i + 1) % len(pairs)
            cur = pairs[self.pair_i]
            items = [(f, lvl, self.ctx.cct_mid) for f in cur] + [(f, lo, None) for f in self.pair_prev if f not in cur]
            if self.cue(items, ttl=0.7):
                self.pair_prev = cur
                self.last_beat_cue = now
                self.head = None
        elif n % 2 == 0:                                      # half-bar: one light steps round the ring
            head, prev = self.step_head()
            items = [(head, lvl, None)] + ([(prev, lo, None)] if prev else [])
            if self.cue(items, ttl=0.7):
                self.last_beat_cue = now

    def _ripple(self, up: bool) -> None:
        lo, _ = self.base()
        hi = self.ctx.hi
        ring = self.ring()
        items = [(f, hi if up else lo, self.ctx.cct_b if up else self.ctx.cct_mid) for f in ring]
        # a drop may borrow up to 6 commands from the future budget; the cue itself is brightness-led
        self.cue([(f, lv, None) for f, lv, _ in items], prio=Prio.HIGH, ttl=2.5, debt=6.0)
        if up:
            self.cue([(ring[0], None, self.ctx.cct_b), (ring[-1], None, self.ctx.cct_b)], prio=Prio.NORMAL, ttl=2.0, debt=2.0)

    def _spark(self, now: float) -> None:
        if now - self.last_spark < 0.9 or self.budget.available() < 3.0:
            return
        ring = [f for f in self.ring() if f != self.head]
        if not ring:
            return
        f = random.choice(ring)
        lo, _ = self.base()
        if self.cue([(f, self.ctx.hi, None)], ttl=0.35):
            self.last_spark = now
            self.actions.append((now + 0.4, "spark_off", f))

    def tick(self, now: float) -> None:
        if self.mode == "build" and now - self.last_build > 1.1 and self.built < len(self.ring()):
            ring = self.ring()
            frac = (self.built + 1) / len(ring)
            lvl = self.ctx.lo + (self.ctx.hi - self.ctx.lo) * (0.35 + 0.4 * frac)
            if self.cue([(ring[self.built], lvl, self.ctx.cct_a + (self.ctx.cct_b - self.ctx.cct_a) * frac)], ttl=2.0, analog=False):
                self.built += 1
                self.last_build = now
        due, rest = [a for a in self.actions if a[0] <= now], [a for a in self.actions if a[0] > now]
        self.actions = rest
        lo, cc = self.base()
        for _, kind, arg in due:
            if kind == "spark_off":
                self.cue([(str(arg), lo, None)], prio=Prio.HIGH, ttl=1.2, debt=1.0)
            elif kind == "return":
                self.cue([(f, lo, cc) for f in self.ring()], prio=Prio.HIGH, ttl=3.0, debt=6.0)
                self.head, self.pair_prev, self.pos = None, [], -1
                if self.mode in ("drop", "breakdown"):
                    self.mode = "steady"
        if self.mode == "steady" or self.mode == "drop":
            self.idle_return(now)


class AmbientProfile(Profile):
    """Slow and elegant: one light at a time drifts to a level that follows the room's energy,
    colour temperature follows the spectrum. About one command a second at most."""
    name, label = "ambient", "Ambient"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.i = 0
        self.last = 0.0
        self.cct_s = 0.4
        self.lvl_s = 0.0

    def tick(self, now: float) -> None:
        if now - self.last < 1.0 / max(0.3, min(1.5, self.budget.rate / 3.0 * 1.2)):
            return
        e = self.an.levels["energy"]
        self.lvl_s += 0.5 * (e - self.lvl_s)
        centroid = self.an.centroid
        c = max(0.0, min(1.0, (math.log10(max(centroid, 100.0)) - 2.0) / 1.7))     # 100 Hz .. 5 kHz
        self.cct_s += 0.4 * (c - self.cct_s)
        ring = self.ring()
        if not ring:
            return
        self.i = (self.i + 1) % len(ring)
        f = ring[self.i]
        # lights further round the ring lag slightly: a slow wave rather than a flicker
        lvl = self.ctx.lo + (self.ctx.hi - self.ctx.lo) * (self.lvl_s ** 1.4) * (0.55 + 0.45 * (1 - self.i / max(1, len(ring))))
        cc = self.ctx.cct_a + (self.ctx.cct_b - self.ctx.cct_a) * self.cct_s
        if self.cue([(f, lvl, cc)], prio=Prio.LOW, ttl=2.5, analog=True):
            self.last = now

    def on_event(self, ev: MusicEvent) -> None:
        pass


PROFILES = {p.name: p for p in (BeatProfile, ClubProfile, AmbientProfile)}


def make_profile(name: str, ctx: EffectContext, budget: Budget, analyzer: "MusicAnalyzer") -> Profile:
    return PROFILES.get(name, BeatProfile)(ctx, budget, analyzer)
