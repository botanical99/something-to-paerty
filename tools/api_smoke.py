"""End-to-end smoke test through the REST API of a RUNNING controller.

    python tools/api_smoke.py                    # http://localhost:8080
    python tools/api_smoke.py --base http://localhost:8099 --quick

It labels itself: against `run.py --simulate` the result is SIMULATOR VERIFIED; against the real gateway it is the
REAL HARDWARE API smoke (the script cannot see the lamps - YOU judge the physical look; it checks state, timing and
that every mode returns the room to where it started). Exit code 0 = all checks passed.
"""
import argparse
import json
import sys
import time
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8080")
ap.add_argument("--quick", action="store_true", help="shorter holds (for the simulator)")
ARGS = ap.parse_args()
BASE = ARGS.base
HOLD = 4 if ARGS.quick else 8

results = []


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def state():
    return call("GET", "/api/state")


def levels(s):
    return {k: (round(v["level"]), v["on"]) for k, v in s["fixtures"].items()}


def settled(timeout=45):
    """Seconds until the gateway queue is empty (None on timeout)."""
    t = time.time()
    while time.time() - t < timeout:
        if state()["gateway"]["stats"]["queue"] == 0:
            time.sleep(0.8)
            if state()["gateway"]["stats"]["queue"] == 0:
                return round(time.time() - t, 1)
        time.sleep(0.15)
    return None


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))


s0 = state()
sim = s0["simulated"]
print("=" * 70)
print("  API SMOKE TEST  ->", BASE)
print("  target:", "SIMULATOR (SIMULATOR VERIFIED only - not hardware proof)" if sim else "REAL HARDWARE gateway")
print("=" * 70)
check("gateway online", s0["gateway"]["state"] == "online", s0["gateway"]["state"])
if s0["gateway"]["state"] != "online":
    sys.exit("gateway is not online - nothing else can be checked")

print("1) scenes")
call("POST", "/api/scene/CHILL")
t = settled()
chill = levels(state())
check("CHILL applied and queue drained", t is not None, f"{t}s")
check("every light is at the CHILL level", len({v for v in chill.values()}) == 1, str(chill))

print(f"2) PARTY (chase) for {HOLD}s, live change, STOP restores")
call("POST", "/api/scene/PARTY")
time.sleep(HOLD)
s = state()
check("mode is effect with a snapshot", s["mode"] == "effect" and s["has_snapshot"], f"{s['mode']}/{s['name']}")
rate = s["gateway"]["stats"]["rate_5s"]
check("command rate within the 4/s budget", rate <= 4.3, f"{rate}/s")
call("POST", "/api/params", {"speed": 85})
time.sleep(2)
check("live parameter change applied", state()["params"]["speed"] == 85)
call("POST", "/api/stop", {"restore": True})
t = settled()
after = levels(state())
check("STOP restored the CHILL look", after == chill, f"{after}")
check("mode is idle after STOP", state()["mode"] == "idle")

print("3) NORMAL during an effect")
call("POST", "/api/effect/start", {"name": "mirror", "params": {"speed": 60}})
time.sleep(HOLD)
t0 = time.time()
call("POST", "/api/normal")
t = settled()
s = state()
lv = levels(s)
check("NORMAL settled", t is not None, f"{t}s")
check("mode is scene/NORMAL, no effect left running", s["mode"] == "scene" and s["scene"] == "NORMAL")
check("all lights at the NORMAL level", len(set(lv.values())) == 1, str(lv))
time.sleep(5)
check("nothing fights NORMAL afterwards", levels(state()) == lv)

print("4) one light, then everything off/on")
call("POST", "/api/fixture/R2", {"level": 70})
settled()
check("single-light control", round(state()["fixtures"]["R2"]["level"]) == 70)
call("POST", "/api/scene/ALL%20OFF")
settled()
check("ALL OFF", all(not v["on"] for v in state()["fixtures"].values()))
call("POST", "/api/normal")
settled()

print("5) music (built-in demo track - exercises the whole audio -> lights path without a microphone)")
try:
    call("POST", "/api/music/start", {"params": {"device": "demo", "profile": "beat"}})
    time.sleep(HOLD + 4)
    m = state()["music"]
    check("music running, audio analysed at audio rate", m["running"] and m["stats"]["audio_fps"] > 20, f"{m['stats']['audio_fps']} fps")
    check("beats detected", m["beats"] > 3, f"{m['beats']} beats, bpm={m['bpm']}")
    rate = state()["gateway"]["stats"]["rate_5s"]
    check("gateway budget respected during music", rate <= 4.3, f"{rate}/s")
    call("POST", "/api/music/stop", {"restore": True})
    settled()
    check("music stopped and room restored", state()["mode"] == "idle" and levels(state()) == lv)
except Exception as e:  # noqa: BLE001
    check("music", False, str(e))

st = state()["gateway"]["stats"]
print(f"\nGateway stats: sent={st['sent']} failed={st['failed']} merged={st['coalesced']} expired={st['dropped_stale']} "
      f"ack median={st['ack_ms_median']} ms")
check("no failed gateway commands" if not sim else "failed commands counted", st["failed"] == 0 or sim, f"failed={st['failed']}")
print("\nRESULT:", "ALL PASSED" if all(results) else f"{results.count(False)} FAILED", "-", "SIMULATOR" if sim else "REAL HARDWARE (judge the look by eye)")
sys.exit(0 if all(results) else 1)
