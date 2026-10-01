"""Audio sources: a built-in synthetic 'song' (demo / simulator / tests) and real input devices.

A source yields mono float32 blocks of `hop` samples. Real devices are optional (sounddevice);
nothing here ever talks to lights.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

import numpy as np

log = logging.getLogger("audio")

DEMO_ID = "demo"


# ===================================================================== synthetic song
class SyntheticSource:
    """Deterministic four-section 'club track' at a fixed BPM: intro -> build -> drop -> breakdown, repeating.

    Ground truth is exposed (`kick_times`) so tests can measure beat-detection accuracy.
    """

    SECTIONS = (("intro", 8), ("build", 8), ("drop", 32), ("breakdown", 8))     # (name, beats)

    def __init__(self, sr: int = 22050, hop: int = 512, bpm: float = 124.0, seed: int = 3, gain: float = 1.0,
                 noise: float = 0.002, realtime: bool = True):
        self.sr, self.hop, self.bpm, self.gain, self.noise, self.realtime = int(sr), int(hop), float(bpm), gain, noise, realtime
        self.rng = np.random.default_rng(seed)
        self.beat = 60.0 / bpm
        self._loop, self.kick_times, self.section_starts = self._render()
        self.duration = len(self._loop) / self.sr
        self._pos = 0
        self._stop = False
        self.t_start = 0.0

    # ---- synthesis
    def _env(self, n: int, tau: float) -> np.ndarray:
        return np.exp(-np.arange(n) / (tau * self.sr))

    def _kick(self) -> np.ndarray:
        n = int(0.3 * self.sr)
        t = np.arange(n) / self.sr
        f = 45 + 110 * np.exp(-t * 28)
        return 0.9 * np.sin(2 * np.pi * np.cumsum(f) / self.sr) * self._env(n, 0.09)

    def _snare(self, amp: float = 0.45) -> np.ndarray:
        n = int(0.18 * self.sr)
        t = np.arange(n) / self.sr
        return amp * (self.rng.standard_normal(n) * 0.7 + np.sin(2 * np.pi * 190 * t)) * self._env(n, 0.05)

    def _hat(self, amp: float = 0.22) -> np.ndarray:
        n = int(0.05 * self.sr)
        x = np.diff(self.rng.standard_normal(n + 1))
        return amp * x * self._env(n, 0.012)

    def _bass(self, f: float, length: float) -> np.ndarray:
        n = int(length * self.sr)
        t = np.arange(n) / self.sr
        attack = np.minimum(1.0, np.arange(n) / (0.02 * self.sr))
        return 0.22 * (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 2 * f * t)) * self._env(n, length * 0.8) * attack

    def _pad(self, n: int, amp: float, bright: float = 1.0) -> np.ndarray:
        t = np.arange(n) / self.sr
        x = sum(np.sin(2 * np.pi * f * t) for f in (220.0, 277.2, 329.6, 440.0 * bright))
        return amp * x / 4.0

    @staticmethod
    def _add(dst: np.ndarray, src: np.ndarray, at: float, sr: int) -> None:
        i = int(at * sr)
        if i < 0 or i >= len(dst):
            return
        j = min(len(dst), i + len(src))
        dst[i:j] += src[: j - i]

    def _render(self):
        sr, beat = self.sr, self.beat
        total_beats = sum(b for _, b in self.SECTIONS)
        n_total = int(round(total_beats * beat * sr))
        out = np.zeros(n_total + sr, np.float32)
        kicks: list[float] = []
        starts: dict[str, float] = {}
        kick, snare, hat = self._kick(), self._snare(), self._hat()
        b = 0
        for name, nb in self.SECTIONS:
            starts[name] = b * beat
            n = int(nb * beat * sr)
            i0 = int(b * beat * sr)
            if name == "intro":
                out[i0:i0 + n] += self._pad(n, 0.10)
                for k in range(nb * 2):
                    self._add(out, hat * 0.7, (b + k * 0.5) * beat, sr)
            elif name == "build":
                ramp = np.linspace(0.05, 0.28, n)
                out[i0:i0 + n] += self._pad(n, 0.12, 1.0) * ramp * 4
                noise = np.diff(self.rng.standard_normal(n + 1)).astype(np.float32) * np.linspace(0.04, 0.35, n)
                out[i0:i0 + n] += noise
                for k in range(nb * 4):                                 # accelerating snare roll
                    pos = (b + nb * (k / (nb * 4)) ** 0.8 * (nb / nb)) * beat
                    self._add(out, snare * (0.4 + 0.6 * k / (nb * 4)), pos, sr)
            elif name == "drop":
                for k in range(nb):
                    self._add(out, kick, (b + k) * beat, sr)
                    kicks.append((b + k) * beat)
                    self._add(out, hat, (b + k + 0.5) * beat, sr)
                    if k % 2 == 1:
                        self._add(out, snare, (b + k) * beat, sr)
                    self._add(out, self._bass(55.0 if (k // 4) % 2 == 0 else 62.0, beat * 0.45), (b + k + 0.5) * beat, sr)
                out[i0:i0 + n] += self._pad(n, 0.06, 1.5)
            else:                                                       # breakdown
                out[i0:i0 + n] += self._pad(n, 0.07)
            b += nb
        out[:sr] += out[n_total:n_total + sr]                       # let tails wrap around: seamless loop
        out = out[:n_total]
        peak = float(np.max(np.abs(out))) or 1.0
        out = (out / peak * 0.8).astype(np.float32)
        return out, kicks, starts

    # ---- source API
    def section_at(self, t: float) -> str:
        t = t % self.duration
        cur = "intro"
        for name, st in self.starts_sorted():
            if t >= st:
                cur = name
        return cur

    def starts_sorted(self):
        return sorted(self.section_starts.items(), key=lambda kv: kv[1])

    def kicks_between(self, t0: float, t1: float) -> list[float]:
        out = []
        cycle = int(t0 // self.duration)
        while cycle * self.duration <= t1:
            for k in self.kick_times:
                tt = cycle * self.duration + k
                if t0 <= tt <= t1:
                    out.append(tt)
            cycle += 1
        return out

    async def start(self) -> None:
        self._pos = 0
        self._stop = False
        self.t_start = time.monotonic()

    async def stop(self) -> None:
        self._stop = True

    async def blocks(self) -> AsyncIterator[np.ndarray]:
        n = 0
        t0 = time.monotonic()
        loop_len = len(self._loop)
        while not self._stop:
            due = t0 + n * self.hop / self.sr
            wait = due - time.monotonic()
            if wait > 0 and self.realtime:
                await asyncio.sleep(wait)
            elif n % 8 == 0:
                await asyncio.sleep(0)
            i = (n * self.hop) % loop_len
            blk = self._loop[i:i + self.hop]
            if blk.size < self.hop:
                blk = np.concatenate([blk, self._loop[: self.hop - blk.size]])
            x = blk * self.gain + self.rng.standard_normal(self.hop).astype(np.float32) * self.noise
            n += 1
            yield x


class SilentSource:
    """Room noise only (for tests of the silence gate)."""

    def __init__(self, sr: int = 22050, hop: int = 512, noise: float = 0.0004):
        self.sr, self.hop, self.noise = sr, hop, noise
        self.rng = np.random.default_rng(1)
        self._stop = False

    async def start(self) -> None:
        self._stop = False

    async def stop(self) -> None:
        self._stop = True

    async def blocks(self):
        n = 0
        t0 = time.monotonic()
        while not self._stop:
            await asyncio.sleep(max(0.0, t0 + n * self.hop / self.sr - time.monotonic()))
            n += 1
            yield self.rng.standard_normal(self.hop).astype(np.float32) * self.noise


# ===================================================================== real devices (optional)
_SD_CACHE: dict = {}


def _sd():
    if "m" not in _SD_CACHE:
        try:
            import sounddevice as sd  # type: ignore[import-not-found]
            _SD_CACHE["m"] = sd
        except Exception as e:  # noqa: BLE001  (missing PortAudio raises OSError, not ImportError)
            log.info("sounddevice unavailable: %s", e)
            _SD_CACHE["m"] = None
    return _SD_CACHE["m"]


def list_devices() -> list[dict]:
    """Input devices the UI can offer. The demo track is always available (no microphone needed)."""
    out = [{"id": DEMO_ID, "name": "Demo track (built-in, no microphone)", "kind": "demo", "default": False}]
    sd = _sd()
    if sd is None:
        return out
    try:
        default_in = sd.default.device[0] if sd.default.device else None
        hostapis = sd.query_hostapis()
        seen = set()
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) < 1:
                continue
            api = hostapis[d["hostapi"]]["name"] if d.get("hostapi") is not None else ""
            if "WDM-KS" in api:                       # unusable half the time, just noise in the list
                continue
            key = (d["name"], api)
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": str(i), "name": f"{d['name']} ({api})" if api else d["name"], "kind": "input",
                        "default": i == default_in})
    except Exception as e:  # noqa: BLE001
        log.warning("could not list audio devices: %s", e)
    return out


class SoundDeviceSource:
    def __init__(self, device: int | None, hop: int = 1024):
        self.hop, self.device = hop, device
        self.sr = 44100
        self._q: asyncio.Queue | None = None
        self._stream: Any = None
        self._loop: Any = None

    async def start(self) -> None:
        sd = _sd()
        if sd is None:
            raise RuntimeError("audio input unavailable: install 'sounddevice' (pip install sounddevice)")
        self._loop = asyncio.get_running_loop()
        self._q = asyncio.Queue(maxsize=64)
        info = sd.query_devices(self.device if self.device is not None else sd.default.device[0])
        self.sr = int(info["default_samplerate"])
        ch = 1 if info["max_input_channels"] < 2 else 2

        def cb(indata, frames, tinfo, status):          # PortAudio thread: only copy and hand over
            x = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
            try:
                self._loop.call_soon_threadsafe(self._put, x.astype(np.float32, copy=True))
            except RuntimeError:
                pass

        self._stream = sd.InputStream(device=self.device, channels=ch, samplerate=self.sr, blocksize=self.hop,
                                      dtype="float32", callback=cb)
        self._stream.start()

    def _put(self, x) -> None:
        q = self._q
        if q is None:
            return
        if q.full():
            try:
                q.get_nowait()                              # never block the audio thread; keep it fresh
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(x)

    async def stop(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:  # noqa: BLE001
                pass
        if self._q is not None:
            self._q.put_nowait(None)

    async def blocks(self):
        assert self._q is not None
        while True:
            x = await self._q.get()
            if x is None:
                return
            yield x


def make_source(device: str | None, *, realtime: bool = True):
    """device: 'demo' | '<index>' | None (system default input)."""
    if device in (DEMO_ID, "sim"):
        return SyntheticSource(realtime=realtime)
    if device in (None, "", "default"):
        return SoundDeviceSource(None)
    try:
        return SoundDeviceSource(int(device or 0))
    except ValueError:
        return SoundDeviceSource(None)
