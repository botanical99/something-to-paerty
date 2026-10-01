"""Browser smoke tests of the mobile UI at an iPhone viewport, against the simulator.  SIMULATOR VERIFIED.

Needs playwright + chromium (skipped otherwise):  python -m playwright install chromium
Screenshots are written to docs/screenshots/.
"""
import os
import socket
import threading
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from api.server import create_app  # noqa: E402
from core.settings import Settings  # noqa: E402

pytestmark = pytest.mark.e2e
# ruff: noqa: E501
SHOTS = Path(__file__).resolve().parents[2] / "docs" / "screenshots"
CHROME = next((p for p in (
    os.environ.get("CHROME_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
) if p and Path(p).exists()), None)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import shutil
    root = Path(__file__).resolve().parents[2]
    cfg = tmp_path_factory.mktemp("cfg")
    for f in ("layout.json", "tuya_fixtures.example.json"):
        shutil.copy(root / "config" / f, cfg / f)
    port = free_port()
    s = Settings.load(cfg, simulate=True, sim_latency_ms=40, log_dir=cfg / "logs", port=port, open_browser=False,
                      require_pin=False)
    srv = uvicorn.Server(uvicorn.Config(create_app(s), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    time.sleep(2.0)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=20)


@pytest.fixture(scope="module")
def page(server):
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"]) if CHROME else p.chromium.launch()
        ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
                            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                                       "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
        pg = ctx.new_page()
        pg.problems = []
        pg.on("pageerror", lambda e: pg.problems.append(f"pageerror: {e}"))
        pg.on("console", lambda m: m.type == "error" and pg.problems.append(f"console: {m.text}"))
        pg.goto(server + "/")
        pg.wait_for_selector(".fx[data-fid=R1]")
        pg.wait_for_function("document.querySelector('#linkTxt').textContent === 'Online'", timeout=15000)
        yield pg
        b.close()


def shot(pg, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    pg.evaluate("window.scrollTo(0, 0)")
    pg.wait_for_timeout(250)
    pg.screenshot(path=str(SHOTS / f"{name}.png"))


def tab(pg, v):
    pg.click(f'#tabs button[data-v="{v}"]')
    pg.wait_for_timeout(400)


def fixture_text(pg):
    return pg.evaluate("Object.fromEntries([...document.querySelectorAll('.fx')].map(g => [g.dataset.fid, g.querySelector('.pct').textContent]))")


def wait_levels(pg, text, timeout=20000):
    pg.wait_for_function("(t) => [...document.querySelectorAll('.fx .pct')].every(e => e.textContent === t)", arg=text, timeout=timeout)


def test_home_layout_and_room(page):
    assert page.locator(".fx").count() == 6
    assert page.locator("#sceneMain .scene").count() == 5
    names = page.locator("#sceneMain .scene .nm").all_inner_texts()
    assert names == ["NORMAL", "CHILL", "CINEMA", "PARTY", "MUSIC"]
    ids = page.evaluate("[...document.querySelectorAll('.fx .id')].map(e => e.textContent)")
    assert sorted(ids) == ["L1", "L2", "L3", "L4", "L5", "L6"]
    # LEFT track L6 -> L2 -> L4 (bed to door = bottom to top), RIGHT track L5 -> L1 -> L3
    pos = page.evaluate("Object.fromEntries([...document.querySelectorAll('.fx')].map(g => [g.querySelector('.id').textContent, [+g.querySelector('.id').getAttribute('x'), +g.querySelector('.id').getAttribute('y')]]))")
    assert pos["L6"][1] > pos["L2"][1] > pos["L4"][1] and pos["L5"][1] > pos["L1"][1] > pos["L3"][1]
    assert pos["L6"][0] < pos["L5"][0]
    assert page.locator("#simPill").is_visible()
    shot(page, "home")


def test_no_horizontal_overflow_and_touch_targets(page):
    for v in ("home", "party", "music", "scenes", "status"):
        tab(page, v)
        w = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        assert w[0] <= w[1], (v, w)
        small = page.evaluate("""() => [...document.querySelectorAll('.view.active button, .view.active input[type=range], .view.active select')]
            .filter(e => e.offsetParent !== null).map(e => [e.id || e.className || e.textContent.trim().slice(0, 20), e.getBoundingClientRect().height])
            .filter(([n, h]) => h < 38)""")
        assert not small, (v, small)
    tab(page, "home")


def test_scene_buttons_drive_the_lights(page):
    page.click('[data-scene="CHILL"]')
    wait_levels(page, "18%")
    assert "on" in page.get_attribute('[data-scene="CHILL"]', "class").split()
    page.click('[data-scene="CINEMA"]')
    page.wait_for_function("document.querySelector('.fx[data-fid=R2] .pct').textContent === 'off'", timeout=20000)
    page.click('[data-scene="NORMAL"]')
    wait_levels(page, "35%")
    shot(page, "home_normal")


def test_tap_a_fixture_and_drag_its_slider(page):
    page.click(".fx[data-fid=R2]")
    page.wait_for_selector("#sheet.show")
    assert "L1" in page.inner_text("#sheet h2")
    page.wait_for_timeout(600)                      # let the sheet finish sliding up before measuring it
    box = page.locator("#fxBri").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.8, box["y"] + box["height"] / 2)
    page.wait_for_function("(() => { const v = parseInt(document.querySelector('.fx[data-fid=R2] .pct').textContent); return v >= 74 && v <= 86; })()", timeout=20000)
    page.click("#fxOn")
    page.wait_for_function("document.querySelector('.fx[data-fid=R2] .pct').textContent === 'off'", timeout=20000)
    shot(page, "fixture_sheet")
    page.click("#fxDone")
    page.wait_for_selector("#sheet:not(.show)")
    page.click('[data-scene="NORMAL"]')
    wait_levels(page, "35%")


def test_party_start_live_change_stop_restore(page):
    tab(page, "party")
    assert page.locator("#fxGrid .fxcard").count() == 9
    page.click('#fxGrid .fxcard[data-fx="mirror"]')
    page.click("#pStart")
    page.wait_for_function("document.querySelector('#pActions #pStop') !== null", timeout=10000)
    page.wait_for_selector('#fxGrid .fxcard.live[data-fx="mirror"]')
    page.click('#pDir button[data-v="reverse"]')
    time.sleep(1)
    st = page.evaluate("fetch('/api/state').then(r => r.json())")
    assert st["mode"] == "effect" and st["params"]["direction"] == "reverse"
    shot(page, "party_running")
    page.click("#pStop")
    page.wait_for_function("document.querySelector('#pActions #pStart') !== null", timeout=10000)
    tab(page, "home")
    wait_levels(page, "35%", 30000)


def test_music_demo_track_shows_live_audio(page):
    tab(page, "music")
    page.wait_for_function("document.querySelectorAll('#mDevice option').length >= 1")
    assert "Demo" in page.inner_text("#mDevice")
    page.click('#mProfile button[data-v="club"]')
    page.click("#mStart")
    page.wait_for_function("document.querySelector('#mActions #mStop') !== null", timeout=10000)
    page.wait_for_function("+document.querySelector('#mBpm').textContent > 80", timeout=30000)
    bpm = int(page.inner_text("#mBpm"))
    assert 110 <= bpm <= 135
    assert page.locator("#mEvents .ev").count() > 0
    page.wait_for_timeout(1500)
    shot(page, "music_running")
    page.click("#mStop")
    page.wait_for_function("document.querySelector('#mActions #mStart') !== null", timeout=10000)
    tab(page, "home")


def test_music_calibration_flow(page):
    tab(page, "music")
    page.click("#mCalib")
    page.wait_for_function("!document.querySelector('#mCalibBar').hidden", timeout=5000)
    page.wait_for_function("document.querySelector('#mCalibInfo').textContent.includes('Calibrated')", timeout=40000)
    shot(page, "music_calibrated")
    tab(page, "home")


def test_scene_editor_saves_a_custom_scene(page):
    tab(page, "scenes")
    page.once("dialog", lambda d: d.accept("Reading"))
    page.click("#sceneNew")
    page.wait_for_selector("#sheet.show #seAllBri")
    page.fill("#seAllBri", "55")
    page.dispatch_event("#seAllBri", "input")
    shot(page, "scene_editor")
    page.click("#seSave")
    page.wait_for_selector("#sheet:not(.show)")
    page.wait_for_function("[...document.querySelectorAll('#sceneList .tx b')].some(b => b.textContent === 'Reading')")
    # clean up
    page.once("dialog", lambda d: d.accept())
    page.click('[data-edit="Reading"]')
    page.wait_for_selector("#sheet.show #seDelete")
    page.click("#seDelete")
    page.wait_for_selector("#sheet:not(.show)")
    tab(page, "home")


def test_status_page_has_qr_and_simulator_panel(page):
    tab(page, "status")
    page.wait_for_selector("#connectCard svg")
    assert page.locator("#simPanel .simcell").count() == 6
    assert "SIMULATOR ONLY" in page.inner_text("#simPanel")
    shot(page, "status")
    tab(page, "home")


def test_simulated_outage_is_visible_and_recovers(page):
    page.evaluate("fetch('/api/sim/disconnect', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({seconds: 14})})")
    # a tap during the outage: the commands fail, the link drops and the UI says so
    page.evaluate("fetch('/api/fixture/R1', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({level: 61})})")
    page.wait_for_function("document.querySelector('#linkTxt').textContent !== 'Online'", timeout=30000)
    page.wait_for_selector("#banners .banner.bad", timeout=5000)
    shot(page, "gateway_outage")
    page.wait_for_function("document.querySelector('#linkTxt').textContent === 'Online'", timeout=120000)
    # the command that was queued during the outage is delivered after the reconnect
    page.wait_for_function("document.querySelector('.fx[data-fid=R1] .pct').textContent === '61%'", timeout=60000)


def test_no_javascript_errors_during_the_whole_session(page):
    assert page.problems == []
