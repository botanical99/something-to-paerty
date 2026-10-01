"""Validation for effect / music parameters coming from the UI (never trust the wire)."""
from __future__ import annotations

RANGES = {
    "speed": (0.0, 100.0), "intensity": (0.0, 100.0),
    "min_brightness": (0.0, 100.0), "max_brightness": (1.0, 100.0),
    "cct_warm": (0.0, 1.0), "cct_cool": (0.0, 1.0),
    "sensitivity": (0.0, 100.0),
}
CHOICES = {
    "direction": ("forward", "reverse", "random"),
    "pattern": ("left_right", "odd_even"),
    "profile": ("beat", "club", "ambient"),
}


def sanitize(p: dict | None, fixtures: set[str] | None = None) -> dict:
    """Keep only known keys, coerce to the right type, clamp to the legal range. Bad values are dropped."""
    out: dict = {}
    for k, v in (p or {}).items():
        if k in RANGES:
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if x != x:                       # NaN
                continue
            lo, hi = RANGES[k]
            out[k] = max(lo, min(hi, x))
        elif k in CHOICES:
            if v in CHOICES[k]:
                out[k] = v
        elif k == "fixtures":
            if v is None or v == []:
                out[k] = None
            elif isinstance(v, list) and all(isinstance(f, str) for f in v):
                ok = [f for f in v if fixtures is None or f in fixtures]
                out[k] = ok or None
        elif k == "device":
            if isinstance(v, (str, int)):
                out[k] = str(v)[:200]
    if "cct_warm" in out and "cct_cool" in out and out["cct_warm"] > out["cct_cool"]:
        out["cct_warm"], out["cct_cool"] = out["cct_cool"], out["cct_warm"]
    return out
