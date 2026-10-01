import json

import pytest

from core.model import Layout, cct_to_raw, level_to_raw, raw_to_cct, raw_to_level
from core.params import sanitize
from core.scenes import DEFAULT_SCENES, SceneStore
from core.settings import Settings


def test_unit_conversions_follow_the_verified_hardware_conventions():
    assert level_to_raw(100) == 1000 and level_to_raw(0) == 10
    assert all(10 <= level_to_raw(x) <= 1000 for x in range(0, 101))
    assert [level_to_raw(x) for x in range(0, 101)] == sorted(level_to_raw(x) for x in range(0, 101))
    assert cct_to_raw(0.0) == 1000 and cct_to_raw(1.0) == 0          # 1000 = WARM, 0 = COOL (verified on hardware)
    assert raw_to_cct(1000) == 0.0 and raw_to_cct(0) == 1.0
    for x in (5, 18, 35, 60, 90):
        assert abs(raw_to_level(level_to_raw(x)) - x) < 2.0
    assert level_to_raw(-5) == 10 and level_to_raw(500) == 1000 and cct_to_raw(7) == 0


def test_layout_matches_the_physical_topology(cfg):
    lay = Layout.load(cfg, simulate=True)
    assert lay.tracks == {"RIGHT": ["R1", "R2", "R3"], "LEFT": ["LFT1", "LFT2", "LFT3"]}
    light = {f.id: f.light for f in lay.fixtures.values()}
    assert light == {"R1": "L5", "R2": "L1", "R3": "L3", "LFT1": "L6", "LFT2": "L2", "LFT3": "L4"}
    assert lay.pairs["bed"] == ["R1", "LFT1"] and lay.pairs["middle"] == ["R2", "LFT2"] and lay.pairs["door"] == ["R3", "LFT3"]
    assert lay.ring == ["R1", "R2", "R3", "LFT3", "LFT2", "LFT1"]
    assert len({f.cid for f in lay.fixtures.values()}) == 6
    pub = json.dumps(lay.to_public())
    assert "cid" not in pub and "sim-" not in pub


def test_real_mode_refuses_to_start_without_the_secret_fixture_file(cfg):
    with pytest.raises(FileNotFoundError):
        Layout.load(cfg, simulate=False)


def test_scene_store_roundtrip_and_defaults(tmp_path):
    f = tmp_path / "scenes.json"
    s = SceneStore(f)
    assert set(DEFAULT_SCENES) <= set(s.scenes)
    s.put("Mine", {"kind": "static", "all": {"level": 10}})
    s.put("NORMAL", {"kind": "static", "all": {"level": 50, "cct": 0.1}, "fixtures": {}})
    s2 = SceneStore(f)
    assert "Mine" in s2.scenes and s2.get("NORMAL")["all"]["level"] == 50
    s2.reset("NORMAL")
    assert SceneStore(f).get("NORMAL")["all"]["level"] == DEFAULT_SCENES["NORMAL"]["all"]["level"]
    assert s2.delete("Mine") and not s2.delete("Mine") and not s2.delete("NORMAL")
    assert s2.order()[:5] == ["NORMAL", "CHILL", "CINEMA", "PARTY", "MUSIC"]


def test_corrupt_scene_file_falls_back_to_defaults(tmp_path):
    f = tmp_path / "scenes.json"
    f.write_text("{not json")
    assert SceneStore(f).get("CHILL")["kind"] == "static"


def test_sanitize_clamps_coerces_and_drops():
    out = sanitize({"speed": "250", "intensity": -3, "direction": "up", "pattern": "odd_even", "profile": "club",
                    "min_brightness": "abc", "max_brightness": 0, "cct_warm": 0.9, "cct_cool": 0.1, "evil": 1,
                    "fixtures": ["R1", "ZZ"], "device": 3, "sensitivity": float("nan")}, {"R1", "R2"})
    assert out == {"speed": 100.0, "intensity": 0.0, "pattern": "odd_even", "profile": "club", "max_brightness": 1.0,
                   "cct_warm": 0.1, "cct_cool": 0.9, "fixtures": ["R1"], "device": "3"}
    assert sanitize(None) == {} and sanitize({"fixtures": []}) == {"fixtures": None}


def test_settings_precedence(tmp_path, monkeypatch):
    (tmp_path / "settings.json").write_text(json.dumps({"port": 9001, "rate": 3.5, "simulate": True, "_note": "x"}))
    s = Settings.load(tmp_path)
    assert (s.port, s.rate, s.simulate) == (9001, 3.5, True)
    monkeypatch.setenv("LIGHTS_PORT", "9002")
    monkeypatch.setenv("LIGHTS_SIMULATE", "false")
    s = Settings.load(tmp_path)
    assert (s.port, s.simulate) == (9002, False)
    assert Settings.load(tmp_path, port=9003).port == 9003
    monkeypatch.setenv("LIGHTS_PORT", "")
    monkeypatch.setenv("LIGHTS_RATE", "fast")
    assert Settings.load(tmp_path).rate == 3.5                    # junk is ignored, not fatal
