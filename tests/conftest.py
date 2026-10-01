from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def cfg(tmp_path) -> Path:
    """An isolated config dir with the real topology and PLACEHOLDER lamp ids (simulator)."""
    d = tmp_path / "config"
    d.mkdir()
    for f in ("layout.json", "tuya_fixtures.example.json"):
        shutil.copy(ROOT / "config" / f, d / f)
    return d
