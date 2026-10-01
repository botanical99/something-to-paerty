"""LAN-only gate, PIN pairing, brute-force brake.  (Logic is hardware independent.)"""

import pytest
from fastapi.testclient import TestClient

from api.auth import Auth, is_lan_address
from api.server import create_app
from core.settings import Settings


def client_for(cfg, host, **kw):
    s = Settings.load(cfg, simulate=True, sim_latency_ms=0, sim_realtime=False, log_dir=cfg / "logs", open_browser=False, **kw)
    return TestClient(create_app(s), client=(host, 40000), follow_redirects=False)


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1", True), ("::1", True), ("192.168.1.20", True), ("10.0.0.5", True), ("172.16.4.4", True),
    ("169.254.1.1", True), ("::ffff:192.168.1.9", True),
    ("8.8.8.8", False), ("203.0.113.9", False), ("2001:4860:4860::8888", False), ("172.32.0.1", False),
    ("testclient", False), ("", False), (None, False),
])
def test_lan_address_classification(host, ok):
    assert is_lan_address(host) is ok


def test_public_addresses_are_refused_everywhere(cfg):
    with client_for(cfg, "203.0.113.9", require_pin=False) as c:
        for path in ("/", "/api/state", "/login", "/static/app.js", "/api/login"):
            assert c.get(path).status_code == 403, path
        with pytest.raises(Exception):
            with c.websocket_connect("/ws"):
                pass


def test_phone_needs_pairing_but_laptop_does_not(cfg):
    with client_for(cfg, "192.168.1.50") as phone:
        assert phone.get("/api/state").status_code == 401
        assert phone.post("/api/scene/CHILL").status_code == 401
        r = phone.get("/", headers={"accept": "text/html"})
        assert r.status_code == 302 and r.headers["location"] == "/login"
        assert phone.get("/login").status_code == 200                     # the login page itself is open
        assert phone.get("/static/app.css").status_code == 200
        with pytest.raises(Exception):
            with phone.websocket_connect("/ws"):
                pass
    with client_for(cfg, "127.0.0.1") as laptop:
        assert laptop.get("/api/state").status_code == 200


def test_pin_login_sets_a_cookie_that_unlocks_everything(cfg):
    with client_for(cfg, "192.168.1.50") as c:
        pin = c.app.state.auth.pin
        assert len(pin) == 6 and pin.isdigit()
        assert c.post("/api/login", json={"pin": "000000" if pin != "000000" else "111111"}).status_code == 401
        r = c.post("/api/login", json={"pin": pin})
        assert r.status_code == 200
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert c.get("/api/state").status_code == 200                     # TestClient keeps the cookie
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "state"
        c.post("/api/logout")
        assert c.get("/api/state").status_code == 401


def test_qr_pairing_link_and_header_token(cfg):
    with client_for(cfg, "192.168.1.60") as c:
        pin = c.app.state.auth.pin
        assert c.get("/pair?code=wrong").headers["location"].startswith("/login")
        r = c.get(f"/pair?code={pin}")
        assert r.status_code == 302 and r.headers["location"] == "/" and "lights_session" in r.headers["set-cookie"]
        assert c.get("/api/state").status_code == 200
    with client_for(cfg, "192.168.1.61") as c2:
        pin = c2.app.state.auth.pin
        assert c2.get("/api/state", headers={"X-Lights-Pin": pin}).status_code == 200
        assert c2.get("/api/state", headers={"Authorization": f"Bearer {pin}"}).status_code == 200
        assert c2.get("/api/state", headers={"X-Lights-Pin": "nope"}).status_code == 401


def test_brute_force_lockout(cfg):
    with client_for(cfg, "192.168.1.70") as c:
        pin = c.app.state.auth.pin
        bad = "999999" if pin != "999999" else "888888"
        codes = [c.post("/api/login", json={"pin": bad}).status_code for _ in range(5)]
        assert codes == [401] * 5
        assert c.post("/api/login", json={"pin": pin}).status_code == 429    # even the right PIN waits
        assert c.get(f"/pair?code={pin}").status_code == 429


def test_rotate_pin_signs_everyone_out(cfg):
    with client_for(cfg, "192.168.1.50") as c:
        c.post("/api/login", json={"pin": c.app.state.auth.pin})
        old = c.app.state.auth.pin
        assert c.post("/api/auth/rotate").status_code == 200
        assert c.app.state.auth.pin != old
        assert c.get("/api/state").status_code == 401


def test_pin_persists_across_restarts_and_never_in_git(cfg, tmp_path):
    a = Auth(cfg / "auth.json")
    b = Auth(cfg / "auth.json")
    assert a.pin == b.pin and a.secret == b.secret
    from pathlib import Path
    ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    for needle in ("config/auth.json", "config/tuya_*.json", ".env"):
        assert needle in ignore


def test_pin_can_be_disabled_for_lan(cfg):
    with client_for(cfg, "192.168.1.80", require_pin=False) as c:
        assert c.get("/api/state").status_code == 200


def test_cross_site_posts_and_dns_rebinding_are_refused(cfg):
    with client_for(cfg, "127.0.0.1") as c:                               # even the trusted laptop
        ok = {"host": "localhost:8080"}
        assert c.post("/api/normal", headers=ok).status_code == 200
        assert c.post("/api/normal", headers={**ok, "origin": "http://localhost:8080"}).status_code == 200
        assert c.post("/api/normal", headers={**ok, "origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/normal", headers={**ok, "origin": "null"}).status_code == 403
        assert c.delete("/api/scenes/x", headers={**ok, "origin": "http://attacker.test"}).status_code == 403
        assert c.get("/api/state", headers={"host": "rebind.attacker.com"}).status_code == 403   # DNS rebinding
        assert c.get("/api/state", headers={"host": "192.168.1.5:8080"}).status_code == 200
        assert c.get("/api/state", headers={"host": "my-laptop.local:8080"}).status_code == 200
        assert c.get("/api/state", headers={"host": "my-laptop:8080"}).status_code == 200
        assert c.get("/docs").status_code in (404, 401)                  # no interactive API explorer


def test_host_header_rules():
    from api.auth import host_header_ok
    for good in ("localhost:8080", "127.0.0.1:8080", "192.168.1.9", "[::1]:8080", "pc.local", "bedroom", ""):
        assert host_header_ok(good), good
    for bad in ("evil.com", "a.b.example.org:8080", "1.2.3.4.nip.io"):
        assert not host_header_ok(bad), bad
