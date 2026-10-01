"""Runtime settings: built-in defaults < config/settings.json < LIGHTS_* environment variables."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    config_dir: Path = field(default_factory=lambda: ROOT / "config")
    log_dir: Path = field(default_factory=lambda: ROOT / "logs")
    host: str = "0.0.0.0"
    port: int = 8080
    simulate: bool = False          # True -> simulated gateway/lamps, never touches the network
    rate: float = 4.0               # global gateway command budget (commands / second)
    music_budget: float = 3.0       # share of that budget music mode may use (rest stays free for taps)
    lan_only: bool = True           # reject any client that is not on a private / loopback address
    require_pin: bool = True        # phones must pair with the PIN (QR code does it for you)
    trust_localhost: bool = True    # the laptop itself needs no PIN
    open_browser: bool = True
    sim_latency_ms: float = 200.0   # simulator: gateway acknowledgement time
    sim_realtime: bool = True       # simulator: honour latency in real time

    @classmethod
    def load(cls, config_dir: Path | None = None, **overrides) -> "Settings":
        s = cls()
        if config_dir is not None:
            s.config_dir = Path(config_dir)
        f = s.config_dir / "settings.json"
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                s._apply({k: v for k, v in data.items() if not k.startswith("_")})
            except (OSError, ValueError):
                pass
        env = {k[7:].lower(): v for k, v in os.environ.items() if k.startswith("LIGHTS_")}
        s._apply(env)
        s._apply({k: v for k, v in overrides.items() if v is not None})
        return s

    def _apply(self, data: dict) -> None:
        types = {f.name: f.type for f in fields(self)}
        for k, v in data.items():
            if k not in types or (isinstance(v, str) and not v.strip()):
                continue
            t = str(types[k])
            try:
                if "bool" in t:
                    v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
                elif "float" in t:
                    v = float(v)
                elif "int" in t:
                    v = int(v)
                elif "Path" in t:
                    v = Path(v)
                else:
                    v = str(v)
            except (TypeError, ValueError):
                continue
            setattr(self, k, v)
