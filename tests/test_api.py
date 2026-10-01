"""REST + WebSocket API against the simulator (real time, zero latency).  SIMULATOR VERIFIED."""
import time

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.settings import Settings

PHONE = ("192.168.1.50", 50000)


def make_client(cfg, **kw):
    s = Settings.load(cfg, simulate=True, sim_latency_ms=0, sim_realtime=False, log_dir=cfg / "logs",
                      open_browser=False, **kw)
    return TestClient(create_app(s), client=PHONE)


@pytest.fixture
def client(cfg):
    with make_client(cfg, require_pin=False) as c:
        wait_online(c)
        yield c


def wait_online(c, timeout=15):
    t = time.time()
    while time.time() - t < timeout:
        s = c.get("/api/state").json()
        if s["gateway"]["state"] == "online":
            time.sleep(0.5)
            return s
        time.sleep(0.1)
    raise AssertionError("never online")


def wait_idle(c, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        if c.get("/api/state").json()["gateway"]["stats"]["queue"] == 0:
            time.sleep(0.3)
            if c.get("/api/state").json()["gateway"]["stats"]["queue"] == 0:
                return
        time.sleep(0.1)
    raise AssertionError("queue never drained")


def levels(c):
    return {k: round(v["level"]) for k, v in c.get("/api/state").json()["fixtures"].items()}


def test_meta_and_state_shapes(client):
    m = client.get("/api/meta").json()
    assert m["simulated"] is True and m["music_available"] is True
    assert [e["name"] for e in m["effects"]][:2] == ["chase", "mirror"] and len(m["effects"]) == 9
    assert set(m["layout"]["fixtures"]) == {"R1", "R2", "R3", "LFT1", "LFT2", "LFT3"}
    assert m["layout"]["tracks"]["LEFT"] == ["LFT1", "LFT2", "LFT3"]
    assert m["scene_order"][:5] == ["NORMAL", "CHILL", "CINEMA", "PARTY", "MUSIC"]
    s = client.get("/api/state").json()
    assert s["simulated"] is True and s["sim"]["reachable"] is True
    assert "id" not in str(s["fixtures"]) and "cid" not in str(m["layout"])      # no device ids leave the backend


def test_scenes_apply_and_normal(client):
    assert client.post("/api/scene/CHILL").status_code == 200
    wait_idle(client)
    assert set(levels(client).values()) == {18}
    assert client.get("/api/state").json()["scene"] == "CHILL"
    assert client.post("/api/scene/NOPE").status_code == 404
    client.post("/api/scene/CINEMA")
    wait_idle(client)
    lv = levels(client)
    assert lv["R2"] == 0 and lv["R1"] > 0
    client.post("/api/normal")
    wait_idle(client)
    assert set(levels(client).values()) == {35}


def test_fixture_control_and_validation(client):
    assert client.post("/api/fixture/R2", json={"level": 70, "cct": 0.2}).status_code == 200
    wait_idle(client)
    st = client.get("/api/state").json()
    assert round(st["fixtures"]["R2"]["level"]) == 70 and st["mode"] == "manual"
    assert client.post("/api/fixture/R2", json={"on": False}).status_code == 200
    wait_idle(client)
    assert client.get("/api/state").json()["fixtures"]["R2"]["on"] is False
    assert client.post("/api/fixture/NOPE", json={"level": 5}).status_code == 404
    assert client.post("/api/fixture/R2", json={"level": "bright"}).status_code == 400
    assert client.post("/api/fixture/R2", json={"level": 9999}).status_code == 200      # clamped, not trusted
    wait_idle(client)
    assert round(client.get("/api/state").json()["fixtures"]["R2"]["level"]) == 100


def test_master_slider(client):
    client.post("/api/scene/CHILL")
    wait_idle(client)
    assert client.post("/api/master", json={"brightness": 60}).status_code == 200
    wait_idle(client)
    assert set(levels(client).values()) == {60}
    assert client.post("/api/master", json={"brightness": "x"}).status_code == 400


def test_effect_lifecycle_and_live_params(client):
    client.post("/api/scene/CHILL")
    wait_idle(client)
    before = levels(client)
    assert client.post("/api/effect/start", json={"name": "nonsense"}).status_code == 400
    assert client.post("/api/effect/start", json={"name": "chase", "params": {"speed": 999, "direction": "sideways"}}).status_code == 200
    s = client.get("/api/state").json()
    assert s["mode"] == "effect" and s["params"]["speed"] == 100 and s["params"]["direction"] == "forward"   # clamped / rejected
    client.post("/api/params", json={"speed": 20, "direction": "reverse"})
    s = client.get("/api/state").json()
    assert s["params"]["speed"] == 20 and s["params"]["direction"] == "reverse"
    time.sleep(2)
    assert client.post("/api/stop", json={"restore": True}).status_code == 200
    wait_idle(client)
    assert levels(client) == before
    assert client.get("/api/state").json()["mode"] == "idle"


def test_music_endpoints(client):
    devs = client.get("/api/audio/devices").json()
    assert devs[0]["id"] == "demo"
    assert client.post("/api/music/start", json={"params": {"device": "demo", "profile": "club"}}).status_code == 200
    time.sleep(2.5)
    m = client.get("/api/state").json()["music"]
    assert m["running"] and m["profile"] == "club" and m["levels"]["rms"] > 0
    client.post("/api/params", json={"profile": "ambient", "sensitivity": 80})
    time.sleep(0.5)
    assert client.get("/api/state").json()["music"]["profile"] == "ambient"
    assert client.post("/api/music/stop", json={}).status_code == 200
    assert client.get("/api/state").json()["music"]["running"] is False
    assert client.post("/api/music/start", json={"params": {"device": "424242"}}).status_code == 409


def test_music_calibrate_endpoint(client):
    assert client.post("/api/music/calibrate", json={"device": "demo", "seconds": 3}).status_code == 200
    t = time.time()
    while time.time() - t < 15:
        m = client.get("/api/state").json()["music"]
        if m["calibration"] and not m["calibrating"]:
            break
        time.sleep(0.2)
    assert m["calibration"]["quality"] == "ok"


def test_scene_editor_crud(client, cfg):
    body = {"kind": "static", "label": "Reading", "all": {"level": 40, "cct": 0.3},
            "fixtures": {"R1": {"level": 90, "cct": 0.2}, "BOGUS": {"level": 1}}}
    assert client.put("/api/scenes/Reading", json=body).status_code == 200
    m = client.get("/api/meta").json()
    assert "Reading" in m["scene_order"] and "BOGUS" not in m["scenes"]["Reading"]["fixtures"]
    assert (cfg / "scenes.json").exists()
    client.post("/api/scene/Reading")
    wait_idle(client)
    lv = levels(client)
    assert lv["R1"] == 90 and lv["R2"] == 40
    assert client.put("/api/scenes/x", json={"kind": "effect", "effect": "nope"}).status_code == 400
    assert client.put("/api/scenes/Pink", json={"kind": "effect", "effect": "pulse", "params": {"speed": 77, "evil": 1}}).status_code == 200
    assert client.get("/api/meta").json()["scenes"]["Pink"]["params"] == {"speed": 77.0}
    assert client.delete("/api/scenes/Reading").status_code == 200
    assert client.delete("/api/scenes/NORMAL").status_code == 400                # built-ins can't be deleted
    client.put("/api/scenes/NORMAL", json={"kind": "static", "all": {"level": 50, "cct": 0.5}})
    client.post("/api/normal")
    wait_idle(client)
    assert set(levels(client).values()) == {50}
    client.post("/api/scenes/NORMAL/reset")
    client.post("/api/normal")
    wait_idle(client)
    assert set(levels(client).values()) == {35}


def test_save_current_room_as_scene(client):
    client.post("/api/fixture/R3", json={"level": 77, "cct": 0.1})
    wait_idle(client)
    r = client.post("/api/scenes/Snapshot/save-current")
    assert r.status_code == 200 and "Snapshot" in r.json()["scene_order"]
    sc = client.get("/api/meta").json()["scenes"]["Snapshot"]
    assert round(sc["fixtures"]["R3"]["level"]) == 77


def test_websocket_streams_live_state(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["gateway"]["state"] == "online"
        client.post("/api/scene/CHILL")
        seen_chill = False
        t = time.time()
        while time.time() - t < 10 and not seen_chill:
            msg = ws.receive_json()
            seen_chill = msg.get("scene") == "CHILL" and all(round(f["level"]) == 18 for f in msg["fixtures"].values())
        assert seen_chill
        client.post("/api/effect/start", json={"name": "pulse"})
        modes = set()
        for _ in range(60):
            modes.add(ws.receive_json()["mode"])
            if "effect" in modes:
                break
        assert "effect" in modes
        client.post("/api/stop", json={})


def test_simulator_panel_endpoints(client):
    assert client.post("/api/sim/disconnect", json={"seconds": 4}).status_code == 200
    time.sleep(0.3)
    assert client.get("/api/state").json()["sim"]["reachable"] is False
    client.post("/api/sim/reconnect")
    assert client.get("/api/state").json()["sim"]["reachable"] is True
    assert client.post("/api/sim/latency", json={"ms": 50}).status_code == 200


def test_connect_info_and_logs_and_health(client):
    j = client.get("/api/connect-info").json()
    assert j["qr_svg"].startswith("<svg") and j["url"].startswith("http://")
    assert isinstance(client.get("/api/logs").json()["lines"], list)
    assert client.get("/api/health").json()["ok"] is True
    for path in ("/", "/connect", "/login", "/manifest.webmanifest", "/sw.js", "/static/app.js", "/static/app.css",
                 "/icons/icon-192.png"):
        assert client.get(path).status_code == 200, path


def test_shutdown_endpoint_signals_exit(client):
    rt = client.app.state.rt
    called = []
    rt.on_exit_request = lambda: called.append(1)
    assert client.post("/api/shutdown").json()["ok"] is True
    assert rt.exit_requested.is_set()
    time.sleep(0.6)
    assert called == [1]


def test_graceful_shutdown_restores_the_room(cfg):
    with make_client(cfg, require_pin=False) as c:
        wait_online(c)
        c.post("/api/scene/CHILL")
        wait_idle(c)
        sim = c.app.state.rt.sim
        before = {k: dict(v) for k, v in sim.lamps.items()}
        c.post("/api/effect/start", json={"name": "chase", "params": {"speed": 90}})
        time.sleep(3)
    after = {k: dict(v) for k, v in sim.lamps.items()}       # lifespan exit ran rt.shutdown()
    assert before == after
