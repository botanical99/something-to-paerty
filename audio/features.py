"""Audio -> features. Pure numpy, no I/O. Runs at the audio frame rate (~40-90 Hz); it never talks to lights."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

BANDS = {"bass": (30.0, 150.0), "mid": (150.0, 2000.0), "treble": (2000.0, 9000.0)}


@dataclass(slots=True)
class Features:
    t: float
    rms: float            # linear RMS of this block (0..1)
    peak: float           # max |sample| (clipping check)
    bass: float           # band amplitudes (linear)
    mid: float
    treble: float
    flux: float           # spectral flux, whole spectrum (log-compressed, positive changes only)
    bass_flux: float
    mid_flux: float
    treble_flux: float
    centroid: float       # Hz


class FeatureExtractor:
    """Overlapped Hann-windowed FFT (window = 2 x hop) -> RMS, band levels, per-band spectral flux, centroid."""

    def __init__(self, sr: int, hop: int):
        self.sr, self.hop, self.win = int(sr), int(hop), int(hop) * 2
        self.window = np.hanning(self.win).astype(np.float32)
        self._norm = 2.0 / float(self.window.sum())
        self.buf = np.zeros(self.win, np.float32)
        self.freqs = np.fft.rfftfreq(self.win, 1.0 / self.sr)
        nyq = self.sr / 2.0
        self.idx = {n: np.flatnonzero((self.freqs >= lo) & (self.freqs < min(hi, nyq))) for n, (lo, hi) in BANDS.items()}
        self._prev: np.ndarray | None = None

    @property
    def fps(self) -> float:
        return self.sr / self.hop

    def reset(self) -> None:
        self.buf[:] = 0
        self._prev = None

    def process(self, block: np.ndarray, t: float) -> Features:
        x = np.asarray(block, dtype=np.float32).reshape(-1)
        if x.size != self.hop:                       # tolerate odd device block sizes
            y = np.zeros(self.hop, np.float32)
            n = min(self.hop, x.size)
            y[:n] = x[:n]
            x = y
        self.buf[:-self.hop] = self.buf[self.hop:]
        self.buf[-self.hop:] = x
        rms = float(np.sqrt(np.mean(x * x)))
        peak = float(np.max(np.abs(x))) if x.size else 0.0
        mag = np.abs(np.fft.rfft(self.buf * self.window)) * self._norm
        lg = np.log1p(mag * 200.0)
        diff = np.zeros_like(lg) if self._prev is None else np.maximum(lg - self._prev, 0.0)
        self._prev = lg
        band = {n: float(np.sqrt(np.mean(mag[i] ** 2))) if i.size else 0.0 for n, i in self.idx.items()}
        bflux = {n: float(diff[i].mean()) if i.size else 0.0 for n, i in self.idx.items()}
        total = float(mag.sum())
        centroid = float((self.freqs * mag).sum() / total) if total > 1e-9 else 0.0
        return Features(t, rms, peak, band["bass"], band["mid"], band["treble"], float(diff.mean()),
                        bflux["bass"], bflux["mid"], bflux["treble"], centroid)
