"""Room model + unit conversions.

Internal units (what effects, scenes and the UI use):
    level : 0..100 %  (0 = off)
    cct   : 0.0 (warmest) .. 1.0 (coolest)

Device units (verified on the real lamps, see HARDWARE_DISCOVERY.md):
    DP20 on/off  | DP22 brightness 10..1000 | DP23 colour temp 0..1000 where 1000 = WARM
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"

DP_ON, DP_BRIGHT, DP_CCT = "20", "22", "23"
RAW_MIN, RAW_MAX = 10, 1000
GAMMA = 2.0  # perceptual curve: low slider values get finer steps


def level_to_raw(level: float) -> int:
    level = max(0.0, min(100.0, level))
    return int(round(RAW_MIN + (RAW_MAX - RAW_MIN) * (level / 100.0) ** GAMMA))


def raw_to_level(raw: float) -> float:
    x = max(0.0, (raw - RAW_MIN) / (RAW_MAX - RAW_MIN))
    return round(100.0 * x ** (1.0 / GAMMA), 1)


def cct_to_raw(cct: float) -> int:
    return int(round(1000 * (1.0 - max(0.0, min(1.0, cct)))))


def raw_to_cct(raw: float) -> float:
    return round(1.0 - max(0, min(1000, raw)) / 1000.0, 3)


@dataclass(frozen=True)
class Fixture:
    id: str
    cid: str
    label: str
    kind: str
    light: str  # L1..L6 alias from the identification step
    track: str


class Layout:
    def __init__(self, data: dict, cids: dict[str, str]):
        self.gateway_ip = data["gateway"]["ip"]
        self.gateway_version = float(data["gateway"].get("version", 3.3))
        self.tracks: dict[str, list[str]] = data["tracks"]
        track_of = {f: t for t, fs in self.tracks.items() for f in fs}
        self.fixtures: dict[str, Fixture] = {}
        for fid, f in data["fixtures"].items():
            self.fixtures[fid] = Fixture(fid, cids[f["light"]], f.get("label", fid), f.get("kind", "spot"),
                                         f["light"], track_of.get(fid, "?"))
        self.ring: list[str] = [f for f in data["ring"] if f in self.fixtures]
        self.pairs: dict[str, list[str]] = {k: [f for f in v if f in self.fixtures] for k, v in data["pairs"].items()}
        self.cid_to_fid = {f.cid: f.id for f in self.fixtures.values()}
        missing = set(self.fixtures) - set(self.ring)
        if missing:
            raise ValueError(f"layout ring is missing fixtures: {sorted(missing)}")

    @classmethod
    def load(cls, config_dir: Path | None = None, simulate: bool = False) -> "Layout":
        """Topology from layout.json; lamp ids from tuya_fixtures.json (git-ignored, real hardware only).
        In simulate mode the placeholder ids of tuya_fixtures.example.json are used instead."""
        cdir = Path(config_dir) if config_dir else CONFIG
        data = json.loads((cdir / "layout.json").read_text(encoding="utf-8"))
        fx_file = cdir / ("tuya_fixtures.example.json" if simulate else "tuya_fixtures.json")
        if not fx_file.exists():
            raise FileNotFoundError(f"{fx_file} is missing - run the identify step, or start in simulator mode")
        fx = json.loads(fx_file.read_text(encoding="utf-8"))
        cids = {k: v["cid"] for k, v in fx["lights"].items()}
        return cls(data, cids)

    def to_public(self) -> dict:
        """What the web UI may see (no ids/keys)."""
        return {
            "fixtures": {f.id: {"label": f.label, "kind": f.kind, "track": f.track, "light": f.light}
                         for f in self.fixtures.values()},
            "tracks": self.tracks, "ring": self.ring, "pairs": self.pairs,
        }
