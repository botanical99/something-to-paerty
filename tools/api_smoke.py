"""Drive the running server through its REST API on the real lights and check the invariants.

    python tools/api_smoke.py
"""
import json
import time
import urllib.request

BASE = "http://localhost:8080"


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def state():
    return call("GET", "/api/state")


def levels(s):
    return {k: (round(v["level"]), v["on"]) for k, v in s["fixtures"].items()}


def wait_idle(timeout=15):
    t = time.time()
    while time.time() - t < timeout:
        s = state()
        if s["gateway"]["stats"]["queue"] == 0:
            time.sleep(0.6)
            if state()["gateway"]["stats"]["queue"] == 0:
                return time.time() - t
        time.sleep(0.15)
    return None


print("1) CHILL scene")
call("POST", "/api/scene/CHILL")
print("   settled in", wait_idle(), "s ->", levels(state()))
chill = levels(state())

print("2) PARTY (chase) for 8 s, then live speed change for 6 s")
call("POST", "/api/scene/PARTY")
time.sleep(8)
s = state()
print("   mode:", s["mode"], s["name"], "| snapshot:", s["has_snapshot"], "| rate:", s["gateway"]["stats"]["rate_5s"], "/s")
call("POST", "/api/params", {"speed": 85})
time.sleep(6)
print("   after speed 85 -> params.speed =", state()["params"]["speed"], "| rate:", state()["gateway"]["stats"]["rate_5s"], "/s")

print("3) STOP with restore -> must return to the CHILL look")
t0 = time.time()
call("POST", "/api/stop", {"restore": True})
dt = wait_idle()
after = levels(state())
print(f"   restored in {dt:.1f}s ->", after, "| matches CHILL:", after == chill, "| mode:", state()["mode"])

print("4) effect, then NORMAL during the effect (timing the return to normal)")
call("POST", "/api/effect/start", {"name": "mirror", "params": {"speed": 60}})
time.sleep(6)
t0 = time.time()
call("POST", "/api/normal")
dt = wait_idle()
s = state()
print(f"   NORMAL settled in {dt:.1f}s  mode={s['mode']}/{s['name']}  levels={levels(s)}")
print("   gateway stats:", s["gateway"]["stats"])
