"""Starts run.py exactly like START_LIGHTS.bat does, in a subprocess, with a FAKE local config.

The fake gateway address is unreachable on purpose: this checks that production start-up, the web UI, auth and
a graceful shutdown work and that a missing/dead gateway is handled calmly. It cannot check the real gateway.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


@pytest.fixture
def fake_real_config(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    layout = json.loads((ROOT / "config" / "layout.json").read_text())
    layout["gateway"]["ip"] = "127.0.0.1"                        # nothing listens on 6668 here -> connection refused
    (cfg / "layout.json").write_text(json.dumps(layout))
    fx = {"lights": {f"L{i}": {"cid": f"fakecid{i}"} for i in range(1, 7)}}
    (cfg / "tuya_fixtures.json").write_text(json.dumps(fx))
    devs = [{"id": "fakegateway", "local_key": "0123456789abcdef", "sub": False}] + \
           [{"id": f"fakecid{i}", "local_key": "0123456789abcdef", "sub": True} for i in range(1, 7)]
    (cfg / "tuya_devices.json").write_text(json.dumps(devs))
    return cfg


def test_real_mode_starts_serves_the_ui_and_stops_gracefully(fake_real_config, tmp_path):
    port = free_port()
    env = {**os.environ, "LIGHTS_CONFIG_DIR": str(fake_real_config), "LIGHTS_LOG_DIR": str(tmp_path / "logs"),
           "LIGHTS_NO_BROWSER": "1", "LIGHTS_PORT": str(port), "PYTHONUNBUFFERED": "1"}
    p = subprocess.Popen([sys.executable, "run.py", "--no-browser"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if get(base + "/api/health")[0] == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            pytest.fail("server never came up:\n" + (p.stdout.read() if p.poll() is not None else ""))
        h = json.loads(get(base + "/api/health")[1])
        assert h["ok"] and h["simulated"] is False and h["gateway"] in ("offline", "connecting")
        state = json.loads(get(base + "/api/state")[1])
        assert state["simulated"] is False and state["gateway"]["state"] != "online"
        assert get(base + "/")[0] == 200 and b"Room" in get(base + "/")[1]
        meta = json.loads(get(base + "/api/meta")[1])
        assert meta["simulated"] is False and len(meta["effects"]) == 9
        assert (fake_real_config / "auth.json").exists()              # PIN created on first start
        # commands are accepted (queued) even though the gateway is unreachable - and never crash the server
        req = urllib.request.Request(base + "/api/scene/CHILL", method="POST", headers={"Content-Type": "application/json"}, data=b"{}")
        assert urllib.request.urlopen(req, timeout=3).status == 200
        # a second START must not start a second controller
        again = subprocess.run([sys.executable, "run.py", "--no-browser"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        assert "already running" in again.stdout
        # graceful stop through the API (what STOP_LIGHTS.bat calls)
        req = urllib.request.Request(base + "/api/shutdown", method="POST", data=b"")
        assert json.loads(urllib.request.urlopen(req, timeout=3).read())["ok"]
        p.wait(timeout=60)
        assert p.returncode == 0
    finally:
        if p.poll() is None:
            p.kill()
    out = p.stdout.read()
    assert "Traceback" not in out, out[-2000:]
    log = (tmp_path / "logs" / "lights.log").read_text()
    assert "shutting down: restoring the room" in log


def test_real_mode_without_secret_config_fails_with_a_clear_message(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy(ROOT / "config" / "layout.json", cfg / "layout.json")
    env = {**os.environ, "LIGHTS_CONFIG_DIR": str(cfg), "LIGHTS_LOG_DIR": str(tmp_path / "logs"), "LIGHTS_PORT": str(free_port())}
    r = subprocess.run([sys.executable, "run.py", "--no-browser"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "tuya_fixtures.json" in (r.stdout + r.stderr)
