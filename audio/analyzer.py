"""Features -> musical events. Adaptive and stateful, but knows nothing about lights or Tuya.

    Features (audio rate) --> MusicAnalyzer --> MusicEvent list (a handful per second at most)

Events:  beat | onset_mid | onset_treble | energy | build | drop | breakdown | silence | resume

Robustness comes from: level normalisation that adapts (and starts from the 10 s calibration),
adaptive onset thresholds (mean + k*std of the recent past), peak picking, per-event cooldowns,
hysteresis on section changes, and a tempo tracker that keeps the pulse through short gaps.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from audio.features import Features


@dataclass(slots=True)
class MusicEvent:
    kind: str
    t: float
    strength: float = 0.5
    meta: dict = field(default_factory=dict)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _db(x: float) -> float:
    return 20.0 * math.log10(x + 1e-6)


class LevelTracker:
    """dB level tracker with adaptive floor and ceiling -> 0..1."""

    def __init__(self, lo_db: float = -66.0, hi_db: float = -20.0, min_range: float = 22.0):
        self.lo, self.hi, self.min_range = lo_db, hi_db, min_range

    def seed(self, floor_amp: float, peak_amp: float) -> None:
        self.lo = _db(floor_amp)
        self.hi = max(_db(peak_amp), self.lo + self.min_range)

    def update(self, amp: float, dt: float) -> float:
        d = _db(amp)
        self.hi = max(d, self.hi - 1.2 * dt)                       # fast attack, slow release (1.2 dB/s)
        self.lo = min(d, self.lo + 0.5 * dt)                       # floor creeps up slowly, drops at once
        self.hi = max(self.hi, self.lo + self.min_range)
        return _clip((d - self.lo) / (self.hi - self.lo))


class OnsetPicker:
    """Adaptive-threshold peak picking with a cooldown."""

    def __init__(self, fps: float, history_s: float = 1.6, min_gap: float = 0.14):
        self.hist: deque[float] = deque(maxlen=max(8, int(history_s * fps)))
        self.min_gap = min_gap
        self.p2 = self.p1 = 0.0
        self.t1 = 0.0
        self.last_t = -9.0
        self.peak = 1e-6

    def update(self, t: float, x: float, k: float, floor: float, min_gap: float | None = None) -> float | None:
        """Feed one flux sample; returns an onset strength (0..1) for the PREVIOUS sample or None."""
        out = None
        n = len(self.hist)
        if n >= 6:
            mean = sum(self.hist) / n
            var = sum((h - mean) ** 2 for h in self.hist) / n
            thr = max(mean + k * math.sqrt(var), floor)
            c = self.p1
            if c > self.p2 and c >= x and c > thr and (self.t1 - self.last_t) >= (min_gap or self.min_gap):
                self.peak = max(c, self.peak * 0.995)
                out = _clip(0.35 + 0.65 * (c - thr) / max(self.peak - thr, 1e-9))
                self.last_t = self.t1
        self.peak = max(self.peak * 0.998, self.p1, 1e-6)
        self.hist.append(self.p1)
        self.p2, self.p1, self.t1 = self.p1, x, t
        return out


class BeatTracker:
    """Pulse tracker. Tempo from the autocorrelation of the onset envelope (80-180 BPM, with sub-multiple
    support so 8th-note bass lines don't fool it), phase from the best-scoring grid, then onsets are
    accepted only near the grid. A short flywheel keeps the pulse through a missing kick."""

    LAG_LO, LAG_HI = 0.33, 0.75          # seconds per beat (181 .. 80 BPM)

    def __init__(self, fps: float, window_s: float = 6.0):
        self.fps = fps
        self.env: deque[float] = deque(maxlen=max(16, int(window_s * fps)))
        self.times: deque[float] = deque(maxlen=self.env.maxlen)
        self.period: float | None = None
        self.anchor: float | None = None      # a time at which a beat is known to fall
        self.confidence = 0.0
        self.rejected = 0
        self.predicted = 0
        self.last_beat: float | None = None
        self._next_eval = 0.0
        self._low_conf_since: float | None = None

    # ---- called every frame with the onset-strength envelope
    def push(self, t: float, value: float) -> None:
        self.env.append(value)
        self.times.append(t)
        if t >= self._next_eval and len(self.env) >= int(3.0 * self.fps):
            self._next_eval = t + 0.5
            self._estimate(t)

    def _estimate(self, t: float) -> None:
        import numpy as np
        x = np.asarray(self.env, dtype=np.float64)
        n = len(x)
        xm = x - x.mean()
        ac = np.correlate(xm, xm, "full")[n - 1:] / np.arange(n, 0, -1)
        if ac[0] <= 1e-9:
            return
        lo, hi = int(self.LAG_LO * self.fps), int(self.LAG_HI * self.fps)
        lags = np.arange(lo, min(hi, n // 2 - 1) + 1)
        if lags.size < 3:
            return
        score = ac[lags] + 0.5 * ac[np.minimum(2 * lags, n - 1)] + 0.25 * ac[np.minimum(4 * lags, n - 1)]
        best = int(lags[int(np.argmax(score))])
        if 0 < best < n - 1:                                      # parabolic refinement
            y0, y1, y2 = ac[best - 1], ac[best], ac[best + 1]
            den = y0 - 2 * y1 + y2
            off = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
        else:
            off = 0.0
        self.confidence = float(ac[best] / ac[0])
        if self.confidence < 0.10:
            if self._low_conf_since is None:
                self._low_conf_since = t
            elif t - self._low_conf_since > 5.0:
                self.period = None
            return
        self._low_conf_since = None
        per = (best + max(-0.5, min(0.5, off))) / self.fps
        self.period = per if (self.period is None or abs(per / self.period - 1) > 0.15) else 0.6 * self.period + 0.4 * per
        self._find_phase(xm)

    def _find_phase(self, xm) -> None:
        if not self.period:
            return
        t = list(self.times)
        v = xm
        n = len(v)
        peaks = [(t[i], float(v[i])) for i in range(1, n - 1) if v[i] > v[i - 1] and v[i] >= v[i + 1] and v[i] > 0.5 * float(v.std())]
        if len(peaks) < 3:
            return
        per = self.period
        best, best_a = -1.0, None
        for a, _ in peaks[-24:]:
            sc = 0.0
            for tt, vv in peaks:
                d = ((tt - a + per / 2) % per) - per / 2
                if abs(d) < 0.15 * per:
                    sc += vv * (1 - abs(d) / (0.15 * per))
            if sc > best:
                best, best_a = sc, a
        if best_a is not None:
            if self.anchor is None or abs(((best_a - self.anchor + per / 2) % per) - per / 2) > 0.18 * per:
                self.anchor = best_a
            else:                                                 # same phase: nudge, don't jump
                self.anchor += 0.3 * (((best_a - self.anchor + per / 2) % per) - per / 2)

    # ---- decisions
    def min_gap(self) -> float:
        return 0.55 * self.period if self.period else 0.24

    def accept(self, t: float, strength: float) -> bool:
        if self.period and self.anchor is not None:
            k = round((t - self.anchor) / self.period)
            err = t - (self.anchor + k * self.period)
            if abs(err) > 0.2 * self.period:
                self.rejected += 1
                if self.rejected >= 6:                            # the music changed: free-run until re-locked
                    self.period, self.anchor, self.rejected = None, None, 0
                    self.last_beat = t
                    return True
                return False
            self.anchor += 0.25 * err                              # follow slow drifts
        self.rejected = 0
        self.predicted = 0
        self.last_beat = t
        return True

    def due_prediction(self, t: float) -> float | None:
        if not self.period or self.anchor is None or self.predicted >= 2 or self.last_beat is None:
            return None
        k = math.floor((self.last_beat - self.anchor) / self.period + 0.5) + 1 + self.predicted
        nxt = self.anchor + k * self.period
        if t >= nxt + 0.22 * self.period:
            self.predicted += 1
            self.last_beat = nxt
            return nxt
        return None

    @property
    def bpm(self) -> float | None:
        return round(60.0 / self.period, 1) if self.period and self.confidence >= 0.10 else None


@dataclass
class Calibration:
    device: str
    sr: int
    rms_floor: float
    rms_peak: float
    bass: tuple[float, float]
    mid: tuple[float, float]
    treble: tuple[float, float]
    flux_floor: float
    seconds: float
    clipped: bool = False
    quality: str = "ok"          # ok | too_quiet | no_signal | too_loud
    ts: float = 0.0

    def to_json(self) -> dict:
        return {"device": self.device, "sr": self.sr, "rms_floor": self.rms_floor, "rms_peak": self.rms_peak,
                "bass": list(self.bass), "mid": list(self.mid), "treble": list(self.treble),
                "flux_floor": self.flux_floor, "seconds": self.seconds, "clipped": self.clipped,
                "quality": self.quality, "ts": self.ts}

    @classmethod
    def from_json(cls, d: dict) -> "Calibration":
        return cls(d["device"], int(d["sr"]), d["rms_floor"], d["rms_peak"], tuple(d["bass"]), tuple(d["mid"]),
                   tuple(d["treble"]), d["flux_floor"], d["seconds"], d.get("clipped", False),
                   d.get("quality", "ok"), d.get("ts", 0.0))


def build_calibration(device: str, sr: int, frames: list[Features], seconds: float, ts: float = 0.0) -> Calibration:
    """Reduce ~10 s of typical music to floors/peaks (percentiles - robust to a stray bump)."""
    def pct(vals: list[float], p: float) -> float:
        s = sorted(vals)
        return s[min(len(s) - 1, int(p * (len(s) - 1)))] if s else 0.0

    rms = [f.rms for f in frames]
    floor, peak = pct(rms, 0.08), pct(rms, 0.97)
    quality = "ok"
    if peak < 0.002:
        quality = "no_signal"
    elif peak < 0.01 or (floor > 0 and peak / max(floor, 1e-6) < 2.0 and peak < 0.03):
        quality = "too_quiet"
    clipped = any(f.peak >= 0.99 for f in frames) and pct([f.peak for f in frames], 0.5) > 0.95
    if clipped:
        quality = "too_loud"
    return Calibration(
        device, sr, floor, peak,
        (pct([f.bass for f in frames], 0.08), pct([f.bass for f in frames], 0.97)),
        (pct([f.mid for f in frames], 0.08), pct([f.mid for f in frames], 0.97)),
        (pct([f.treble for f in frames], 0.08), pct([f.treble for f in frames], 0.97)),
        pct([f.flux for f in frames], 0.25), seconds, clipped, quality, ts)


class MusicAnalyzer:
    def __init__(self, fps: float, sensitivity: float = 60.0, calibration: Calibration | None = None):
        self.fps = fps
        self.sensitivity = sensitivity
        self.cal = calibration
        self.lv = {n: LevelTracker() for n in ("rms", "bass", "mid", "treble")}
        self.bass_pick = OnsetPicker(fps, min_gap=0.2)
        self.mid_pick = OnsetPicker(fps, min_gap=0.12)
        self.treble_pick = OnsetPicker(fps, min_gap=0.10)
        self.full_pick = OnsetPicker(fps, min_gap=0.2)
        self.tempo = BeatTracker(fps)
        self.levels = {"rms": 0.0, "bass": 0.0, "mid": 0.0, "treble": 0.0, "energy": 0.0}
        self.centroid = 0.0
        self.section = "steady"
        self.beat_count = 0
        self.last_beat_t = -9.0
        self.silent = True
        self._silent_since: float | None = None
        self._last_t: float | None = None
        self._e_fast = self._e_slow = 0.0
        self._hist: deque[tuple[float, float, float]] = deque()    # (t, energy, bass-dominance) @ ~10 Hz
        self._ratio = 0.0
        self._t0: float | None = None
        self._next_slow = 0.0
        self._next_energy_event = 0.0
        self._last_drop = -99.0
        self._last_bass_onset = -99.0
        self._build_since: float | None = None
        self._break_since: float | None = None
        self._was_high = 0.0
        self._peak_rms_seen = 0.0
        self.apply_calibration(calibration)

    # ---- configuration
    def apply_calibration(self, cal: Calibration | None) -> None:
        self.cal = cal
        if cal and cal.quality in ("ok", "too_loud"):
            self.lv["rms"].seed(max(cal.rms_floor, 1e-5), max(cal.rms_peak, 2e-4))
            self.lv["bass"].seed(max(cal.bass[0], 1e-5), max(cal.bass[1], 2e-4))
            self.lv["mid"].seed(max(cal.mid[0], 1e-5), max(cal.mid[1], 2e-4))
            self.lv["treble"].seed(max(cal.treble[0], 1e-5), max(cal.treble[1], 2e-4))

    def set_sensitivity(self, s: float) -> None:
        self.sensitivity = _clip(float(s), 0.0, 100.0)

    @property
    def k(self) -> float:
        return 3.4 - 2.4 * (self.sensitivity / 100.0)          # sensitivity 0 -> 3.4 sigma, 100 -> 1.0 sigma

    @property
    def bpm(self) -> float | None:
        return self.tempo.bpm

    @property
    def silence_rms(self) -> float:
        base = self.cal.rms_floor * 1.6 if self.cal and self.cal.quality != "no_signal" else 0.0
        return max(0.0015, base)

    # ---- main entry: one Features frame in, 0..n events out
    def process(self, f: Features) -> list[MusicEvent]:
        ev: list[MusicEvent] = []
        t = f.t
        dt = 1.0 / self.fps if self._last_t is None else max(1e-3, min(0.5, t - self._last_t))
        self._last_t = t
        self._peak_rms_seen = max(self._peak_rms_seen * 0.9995, f.rms)

        # ---- silence gate (with hysteresis: 1.2 s of quiet to enter, immediate exit on clear sound)
        quiet = f.rms < self.silence_rms
        if quiet:
            if self._silent_since is None:
                self._silent_since = t
            if not self.silent and t - self._silent_since > 1.2:
                self.silent = True
                self.section = "silence"
                ev.append(MusicEvent("silence", t, 0.0))
        else:
            self._silent_since = None
            if self.silent and f.rms > self.silence_rms * 1.4:
                self.silent = False
                self.section = "steady"
                self._e_fast = self._e_slow = 0.0
                ev.append(MusicEvent("resume", t, 0.5))

        # ---- levels (always tracked so the visualiser stays alive)
        for name, amp in (("rms", f.rms), ("bass", f.bass), ("mid", f.mid), ("treble", f.treble)):
            v = self.lv[name].update(amp, dt)
            self.levels[name] = 0.0 if self.silent else v
        self.centroid = f.centroid
        e_raw = 0.0 if self.silent else 0.45 * self.levels["rms"] + 0.35 * self.levels["bass"] + 0.12 * self.levels["mid"] + 0.08 * self.levels["treble"]
        a_up, a_down = 1 - math.exp(-dt / 0.25), 1 - math.exp(-dt / 0.7)
        self._e_fast += (a_up if e_raw > self._e_fast else a_down) * (e_raw - self._e_fast)
        self._e_slow += (1 - math.exp(-dt / 3.0)) * (e_raw - self._e_slow)
        r_raw = f.bass / (f.bass + f.mid + f.treble + 1e-9) if not self.silent else 0.0
        self._ratio += (1 - math.exp(-dt / 0.4)) * (r_raw - self._ratio)          # how bass-dominated the sound is
        self.levels["energy"] = self._e_fast

        if self.silent:
            self.bass_pick.update(t, 0.0, 9.0, 1e9)
            return ev

        # ---- onsets
        fl_floor = (self.cal.flux_floor * 1.5 if self.cal else 0.0) + 0.004
        k = self.k
        bs = self.bass_pick.update(t, f.bass_flux, k, fl_floor, self.tempo.min_gap())
        ms = self.mid_pick.update(t, f.mid_flux, k + 0.4, fl_floor)
        ts = self.treble_pick.update(t, f.treble_flux, k + 0.4, fl_floor)
        fs = self.full_pick.update(t, f.flux, k, fl_floor, self.tempo.min_gap())
        self.tempo.push(t, f.bass_flux + 0.3 * (f.mid_flux + f.treble_flux))
        if bs is not None:
            self._last_bass_onset = t
        use_full = (t - self._last_bass_onset) > 2.5 and self._e_fast > 0.3     # no kick? follow the whole spectrum
        cand = bs if bs is not None else (fs if use_full else None)
        if cand is not None and self.tempo.accept(t, cand):
            self.beat_count += 1
            self.last_beat_t = t
            ev.append(MusicEvent("beat", t, cand, {"bpm": self.bpm, "n": self.beat_count, "predicted": False}))
        else:
            pt = self.tempo.due_prediction(t)
            if pt is not None and self._e_fast > 0.25:
                self.beat_count += 1
                self.last_beat_t = t
                ev.append(MusicEvent("beat", t, 0.45, {"bpm": self.bpm, "n": self.beat_count, "predicted": True}))
        if ms is not None:
            ev.append(MusicEvent("onset_mid", t, ms))
        if ts is not None:
            ev.append(MusicEvent("onset_treble", t, ts))

        # ---- slow section logic (10 Hz) + low-rate energy events
        if t >= self._next_slow:
            self._next_slow = t + 0.1
            self._hist.append((t, self._e_fast, self._ratio))
            while self._hist and t - self._hist[0][0] > 6.0:
                self._hist.popleft()
            ev.extend(self._sections(t, f))
        if t >= self._next_energy_event:
            self._next_energy_event = t + 0.5
            ev.append(MusicEvent("energy", t, self._e_fast, {"levels": dict(self.levels), "centroid": f.centroid}))
        return ev

    def _e_at(self, t: float, ago: float) -> float:
        target = t - ago
        for tt, e, _ in self._hist:
            if tt >= target:
                return e
        return self._hist[0][1] if self._hist else 0.0

    def _sections(self, t: float, f: Features) -> list[MusicEvent]:
        out: list[MusicEvent] = []
        e = self._e_fast
        if self._t0 is None:
            self._t0 = t
        if t - self._t0 < 2.5:                                       # levels are still settling
            return out
        prev = [(x, r) for tt, x, r in self._hist if t - 3.2 <= tt <= t - 1.0]
        prev_ratio = min((r for _, r in prev), default=self._ratio)
        # DROP: the bass takes over (kick returns) while the room is loud
        if (e > 0.5 and self._ratio > 0.40 and self._ratio - prev_ratio > 0.22
                and (t - self._last_drop) > 5.0 and t - self.last_beat_t < 0.6):
            self._last_drop = t
            self.section = "drop"
            self._build_since = self._break_since = None
            out.append(MusicEvent("drop", t, e))
            self._was_high = e
            return out
        # BUILD: energy climbing steadily with treble content, below peak level
        rise = e - self._e_at(t, 2.5)
        if self.section != "build" and rise > 0.18 and 0.18 < e < 0.85 and self.levels["treble"] > 0.25:
            self._build_since = t
            self.section = "build"
            out.append(MusicEvent("build", t, e))
        elif self.section == "build" and (rise < -0.02 or e > 0.9):
            self.section = "steady"
        # BREAKDOWN: fell from high to low and stayed there (hysteresis: 0.55 up / 0.3 down)
        if e > 0.55:
            self._was_high = max(self._was_high * 0.999, e)
            self._break_since = None
        elif e < 0.30 and self._was_high > 0.55 and self.section not in ("breakdown", "silence"):
            if self._break_since is None:
                self._break_since = t
            elif t - self._break_since > 1.2:
                self.section = "breakdown"
                self._was_high = 0.0
                out.append(MusicEvent("breakdown", t, e))
        if self.section == "drop" and t - self._last_drop > 3.0 and e < 0.7:
            self.section = "steady"
        if self.section == "breakdown" and e > 0.4:
            self.section = "steady"
        return out
