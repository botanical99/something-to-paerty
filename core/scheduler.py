"""Gateway command scheduler.

Everything that wants to change a light goes through here. Rules:
  * ONE writer, ONE persistent connection, global rate limit (default 4 commands/s).
  * Commands are keyed by (lamp, datapoint). A newer request for the same key REPLACES the
    queued one (coalescing) - stale intermediate values are never sent.
  * Commands can carry a TTL: if the budget didn't get to them in time they are dropped,
    so the lights always show something recent instead of lagging behind the music.
  * Priority classes: CRITICAL (NORMAL/STOP/shutdown) > HIGH > NORMAL (effects) > LOW (sparks, ambient).
  * Epochs: every mode change bumps the epoch; an effect holding an old epoch is rejected,
    so two effects can never fight over the same fixtures.
  * Analog targets (pulse / breathing) are sent error-first with a dead-band, so bandwidth goes
    where the visible difference is largest.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum

from core.model import DP_BRIGHT, DP_CCT, DP_ON, Layout, cct_to_raw, level_to_raw, raw_to_cct, raw_to_level
from hardware.gateway_link import AsyncGatewayLink, LinkDown

log = logging.getLogger("scheduler")


class Prio(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass
class Cmd:
    cid: str
    dp: str
    value: object
    prio: Prio
    first_ts: float
    ts: float
    deadline: float | None = None
    analog: bool = False
    attempts: int = 0
    tag: str = ""

    @property
    def key(self):
        return (self.cid, self.dp)


@dataclass
class Stats:
    sent: int = 0
    failed: int = 0
    coalesced: int = 0
    dropped_stale: int = 0
    dropped_noop: int = 0
    sent_times: deque = field(default_factory=lambda: deque(maxlen=64))
    ack_ms: deque = field(default_factory=lambda: deque(maxlen=64))
    queue_ms: deque = field(default_factory=lambda: deque(maxlen=64))
    max_queue: int = 0

    def rate(self, window: float = 5.0) -> float:
        now = time.monotonic()
        n = sum(1 for t in self.sent_times if now - t <= window)
        return round(n / window, 2)

    def public(self) -> dict:
        a = sorted(self.ack_ms)
        q = sorted(self.queue_ms)
        return {
            "sent": self.sent, "failed": self.failed, "coalesced": self.coalesced,
            "dropped_stale": self.dropped_stale, "dropped_noop": self.dropped_noop,
            "rate_5s": self.rate(), "max_queue": self.max_queue,
            "ack_ms_median": round(a[len(a) // 2]) if a else None,
            "ack_ms_p95": round(a[int(0.95 * (len(a) - 1))]) if a else None,
            "queue_ms_median": round(q[len(q) // 2]) if q else None,
            "queue_ms_p95": round(q[int(0.95 * (len(q) - 1))]) if q else None,
        }


DEADBAND = {DP_BRIGHT: 25, DP_CCT: 35}   # raw units; analog commands closer than this are skipped
DP_RANK = {DP_ON: 0, DP_BRIGHT: 1, DP_CCT: 2}


class Scheduler:
    def __init__(self, layout: Layout, link: AsyncGatewayLink, rate: float = 4.0):
        self.layout, self.link = layout, link
        self.rate = rate
        self.known: dict[str, dict[str, object]] = {}     # what the lamps are believed to be doing
        self.desired: dict[str, dict[str, object]] = {}   # what we last asked for
        self.pending: dict[tuple, Cmd] = {}
        self.epoch = 0
        self.stats = Stats()
        self._next_slot = 0.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.idle_event = asyncio.Event()
        self.idle_event.set()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def new_epoch(self, drop_below: Prio = Prio.NORMAL) -> int:
        """Called on every mode change. Rejects old effect tasks and flushes their queued commands."""
        self.epoch += 1
        self.flush(drop_below)
        return self.epoch

    def flush(self, min_prio: Prio = Prio.NORMAL) -> int:
        n = 0
        for k, c in list(self.pending.items()):
            if c.prio >= min_prio:
                self._revert_desired(c)
                del self.pending[k]
                n += 1
        return n

    def _revert_desired(self, c: Cmd) -> None:
        kv = self.known.get(c.cid, {})
        if c.dp in kv:
            self.desired.setdefault(c.cid, {})[c.dp] = kv[c.dp]

    # ------------------------------------------------------------------ public API
    def set_light(self, fid: str, level: float | None = None, cct: float | None = None, *,
                  prio: Prio = Prio.NORMAL, ttl: float | None = None, epoch: int | None = None,
                  analog: bool = False, tag: str = "") -> bool:
        """level 0..100 (0 = off), cct 0 warm..1 cool. Returns False if the caller's epoch is stale."""
        if epoch is not None and epoch != self.epoch:
            return False
        cid = self.layout.fixtures[fid].cid
        if level is not None:
            if level <= 0.0:
                self._submit(cid, DP_ON, False, prio, ttl, analog, tag)
                self.pending.pop((cid, DP_BRIGHT), None)
            else:
                self._submit(cid, DP_ON, True, prio, ttl, False, tag)
                self._submit(cid, DP_BRIGHT, level_to_raw(level), prio, ttl, analog, tag)
        if cct is not None:
            self._submit(cid, DP_CCT, cct_to_raw(cct), prio, ttl, analog, tag)
        self._wake.set()
        return True

    def set_many(self, targets: dict[str, tuple[float | None, float | None]], **kw) -> bool:
        ok = True
        for fid, (lv, cc) in targets.items():
            ok &= self.set_light(fid, lv, cc, **kw)
        return ok

    def _submit(self, cid, dp, value, prio, ttl, analog, tag) -> None:
        now = time.monotonic()
        self.desired.setdefault(cid, {})[dp] = value
        key = (cid, dp)
        cur = self.pending.get(key)
        if self.known.get(cid, {}).get(dp) == value:
            if cur:
                del self.pending[key]
                self.stats.coalesced += 1
            else:
                self.stats.dropped_noop += 1
            return
        deadline = now + ttl if ttl else None
        if cur:
            self.stats.coalesced += 1
            cur.value, cur.ts, cur.deadline, cur.analog, cur.tag = value, now, deadline, analog, tag
            cur.prio = min(cur.prio, prio) if cur.prio == Prio.CRITICAL else prio
        else:
            self.pending[key] = Cmd(cid, dp, value, prio, now, now, deadline, analog, 0, tag)
        self.idle_event.clear()
        self.stats.max_queue = max(self.stats.max_queue, len(self.pending))

    # ------------------------------------------------------------------ state helpers
    def capture(self) -> dict[str, dict]:
        """Raw-exact snapshot of what the room is (to be) showing, for later restoration."""
        snap = {}
        for fid, f in self.layout.fixtures.items():
            d = {**self.known.get(f.cid, {}), **self.desired.get(f.cid, {})}
            if DP_BRIGHT in d or DP_ON in d:
                snap[fid] = {"on": bool(d.get(DP_ON, True)), "bright_raw": int(d.get(DP_BRIGHT, 500)),
                             "cct_raw": int(d.get(DP_CCT, 500))}
        return snap

    def set_raw(self, fid: str, on: bool, bright_raw: int, cct_raw: int, *, prio: Prio = Prio.HIGH,
                tag: str = "") -> None:
        cid = self.layout.fixtures[fid].cid
        if on:
            self._submit(cid, DP_ON, True, prio, None, False, tag)
            self._submit(cid, DP_BRIGHT, int(bright_raw), prio, None, False, tag)
            self._submit(cid, DP_CCT, int(cct_raw), prio, None, False, tag)
        else:
            self._submit(cid, DP_ON, False, prio, None, False, tag)
        self._wake.set()

    def restore(self, snap: dict[str, dict], prio: Prio = Prio.HIGH) -> None:
        for fid, s in snap.items():
            self.set_raw(fid, s["on"], s["bright_raw"], s["cct_raw"], prio=prio, tag="restore")

    def public_state(self) -> dict:
        out = {}
        for fid, f in self.layout.fixtures.items():
            d = {**self.known.get(f.cid, {}), **self.desired.get(f.cid, {})}
            on = bool(d.get(DP_ON, True))
            out[fid] = {"on": on, "level": raw_to_level(d.get(DP_BRIGHT, 10)) if on else 0.0,
                        "cct": raw_to_cct(d.get(DP_CCT, 500)),
                        "pending": any(k[0] == f.cid for k in self.pending)}
        return out

    async def adopt_from_gateway(self) -> None:
        """Re-read every lamp and take that as truth (used at start-up, after reconnect, and to pick up
        changes made from the Tuya phone app while idle)."""
        states = await self.link.read_all([f.cid for f in self.layout.fixtures.values()])
        for cid, dps in states.items():
            self.known[cid] = {k: v for k, v in dps.items() if k in (DP_ON, DP_BRIGHT, DP_CCT)}

    def adopt_known_as_desired(self) -> None:
        for cid, kv in self.known.items():
            self.desired[cid] = dict(kv)

    def reassert(self, prio: Prio = Prio.HIGH) -> int:
        n = 0
        for cid, dd in self.desired.items():
            for dp, v in dd.items():
                if self.known.get(cid, {}).get(dp) != v:
                    self._submit(cid, dp, v, prio, None, False, "reassert")
                    n += 1
        self._wake.set()
        return n

    # ------------------------------------------------------------------ main loop
    def _score(self, c: Cmd, now: float) -> float:
        s = now - c.first_ts
        if c.analog:
            kv = self.known.get(c.cid, {}).get(c.dp)
            if isinstance(kv, (int, float)) and isinstance(c.value, (int, float)):
                s += 2.0 * abs(c.value - kv) / 1000.0 * 10
        if c.dp == DP_ON and c.value is True:
            s += 0.3  # switch on before changing brightness of the same lamp
        return s

    def _pick(self) -> Cmd | None:
        now = time.monotonic()
        best, best_key = None, None
        for k, c in list(self.pending.items()):
            if c.deadline is not None and now > c.deadline:
                self._revert_desired(c)
                del self.pending[k]
                self.stats.dropped_stale += 1
                continue
            kv = self.known.get(c.cid, {}).get(c.dp)
            if c.analog and isinstance(kv, (int, float)) and isinstance(c.value, (int, float)) \
                    and abs(c.value - kv) < DEADBAND.get(c.dp, 0):
                del self.pending[k]
                self.stats.dropped_noop += 1
                continue
            if kv == c.value:
                del self.pending[k]
                self.stats.dropped_noop += 1
                continue
            key = (c.prio, -self._score(c, now))
            if best_key is None or key < best_key:
                best, best_key = c, key
        if not self.pending:
            self.idle_event.set()
        return best

    async def _run(self) -> None:
        while True:
            try:
                cmd = self._pick()
                if cmd is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), 0.2)
                    except asyncio.TimeoutError:
                        pass
                    continue
                if self.link.state != "online":
                    await asyncio.sleep(0.25)   # keep commands queued (stale ones expire by TTL)
                    continue
                wait = self._next_slot - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                    cmd = self._pick()          # something more important may have arrived
                    if cmd is None or self.link.state != "online":
                        continue
                start = time.monotonic()
                self.pending.pop(cmd.key, None)
                try:
                    ms = await self.link.write(cmd.cid, cmd.dp, cmd.value)
                except Exception as e:  # noqa: BLE001
                    self.stats.failed += 1
                    if not isinstance(e, LinkDown):
                        cmd.attempts += 1
                    if cmd.key not in self.pending and cmd.attempts <= 5:
                        self.pending[cmd.key] = cmd    # retry unless a newer value arrived meanwhile
                    await asyncio.sleep(0.3)
                    continue
                self.known.setdefault(cmd.cid, {})[cmd.dp] = cmd.value
                self.stats.sent += 1
                self.stats.sent_times.append(time.monotonic())
                self.stats.ack_ms.append(ms)
                self.stats.queue_ms.append((start - cmd.first_ts) * 1000)
                self._next_slot = start + 1.0 / self.rate
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("scheduler loop error")
                await asyncio.sleep(0.5)
