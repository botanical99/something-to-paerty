"""Editable scenes, stored in config/scenes.json (git-ignored); defaults are built in.

Scene kinds:
  static : per-room look. {"all": {"level": %, "cct": 0..1}, "fixtures": {"R1": {"level":..,"cct":..}}}
           level 0 = off, cct 0 = warm .. 1 = cool. Fixture entries override "all".
  effect : {"effect": "chase", "params": {...}}
  music  : {"params": {...}}
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from core.model import CONFIG

log = logging.getLogger("scenes")
FILE = CONFIG / "scenes.json"

DEFAULT_SCENES: dict[str, dict] = {
    "NORMAL": {"kind": "static", "label": "Normal", "all": {"level": 35, "cct": 0.45}, "fixtures": {}},
    "CHILL": {"kind": "static", "label": "Chill", "all": {"level": 18, "cct": 0.0}, "fixtures": {}},
    "CINEMA": {"kind": "static", "label": "Cinema", "all": {"level": 0, "cct": 0.0},
               "fixtures": {"R1": {"level": 6, "cct": 0.0}, "LFT1": {"level": 6, "cct": 0.0},
                            "R3": {"level": 4, "cct": 0.0}, "LFT3": {"level": 4, "cct": 0.0}}},
    "ALL ON": {"kind": "static", "label": "All on", "all": {"level": 100, "cct": 0.5}, "fixtures": {}},
    "ALL OFF": {"kind": "static", "label": "All off", "all": {"level": 0}, "fixtures": {}},
    "PARTY": {"kind": "effect", "label": "Party", "effect": "chase", "params": {"speed": 55, "intensity": 85}},
    "MUSIC": {"kind": "music", "label": "Music", "params": {}},
}


class SceneStore:
    def __init__(self):
        self.scenes = copy.deepcopy(DEFAULT_SCENES)
        if FILE.exists():
            try:
                saved = json.loads(FILE.read_text(encoding="utf-8"))
                for k, v in saved.items():
                    self.scenes[k] = v
            except Exception as e:  # noqa: BLE001
                log.warning("could not read scenes.json (%s) - using defaults", e)

    def get(self, name: str) -> dict:
        return self.scenes[name]

    def save(self) -> None:
        FILE.write_text(json.dumps(self.scenes, indent=2), encoding="utf-8")

    def put(self, name: str, scene: dict) -> None:
        self.scenes[name] = scene
        self.save()

    def reset(self, name: str) -> None:
        if name in DEFAULT_SCENES:
            self.scenes[name] = copy.deepcopy(DEFAULT_SCENES[name])
            self.save()

    def public(self) -> dict:
        return self.scenes
