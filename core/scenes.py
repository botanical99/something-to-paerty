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
BUILTIN_ORDER = ["NORMAL", "CHILL", "CINEMA", "PARTY", "MUSIC", "ALL ON", "ALL OFF"]

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
    def __init__(self, path: Path | None = None):
        self.file = Path(path) if path else FILE
        self.scenes = copy.deepcopy(DEFAULT_SCENES)
        if self.file.exists():
            try:
                saved = json.loads(self.file.read_text(encoding="utf-8"))
                for k, v in saved.items():
                    self.scenes[k] = v
            except Exception as e:  # noqa: BLE001
                log.warning("could not read scenes.json (%s) - using defaults", e)

    def get(self, name: str) -> dict:
        return self.scenes[name]

    def save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.scenes, indent=2), encoding="utf-8")

    def put(self, name: str, scene: dict) -> None:
        self.scenes[name] = scene
        self.save()

    def reset(self, name: str) -> None:
        if name in DEFAULT_SCENES:
            self.scenes[name] = copy.deepcopy(DEFAULT_SCENES[name])
            self.save()

    def delete(self, name: str) -> bool:
        """Built-in scenes can be edited/reset but not deleted."""
        if name in DEFAULT_SCENES or name not in self.scenes:
            return False
        del self.scenes[name]
        self.save()
        return True

    def order(self) -> list[str]:
        return [n for n in BUILTIN_ORDER if n in self.scenes] + [n for n in self.scenes if n not in BUILTIN_ORDER]

    def public(self) -> dict:
        return self.scenes
