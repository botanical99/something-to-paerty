"""Latency / rate / persistence / sequence benchmark for the gateway LAN path (REAL hardware).

    python tools/lan_bench.py setup|latency|rate|persist|sequence|all [--persist-seconds N]

Safety: one serialised socket; escalating rates that stop at the first failure; every lamp's
original DPs are restored in `finally`. 'ack' = time until the gateway acknowledges the command,
which is NOT the same as the lamp visibly changing (BLE-mesh hop happens after the ack).
"""
import json
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hardware.tuya_lan import DP_BRIGHT, DP_CCT, DP_ON, GatewayLan  # noqa: E402

FIX = json.loads((ROOT / "config" / "tuya_fixtures.json").read_text(encoding="utf-8"))
L = {k: v["cid"] for k, v in FIX["lights"].items()}
IP = "192.168.100.171"
RESULTS: dict = {}


def stats(xs):
    xs = sorted(xs)
    if not xs:
        return {}
    return {"n": len(xs), "min": round(xs[0]), "median": round(st.median(xs)),
            "p90": round(xs[int(0.9 * (len(xs) - 1))]), "max": round(xs[-1]), "mean": round(st.mean(xs))}


def snapshot(g, names):
    return {n: g.read(L[n]) for n in names}


def restore(g, snap):
    for n, dps in snap.items():
        try:
            cur = g.read(L[n])
        except Exception:  # noqa: BLE001
            cur = {}
        for dp in (DP_BRIGHT, DP_CCT, DP_ON):  # state that matters, on/off last
            if dp in dps and cur.get(dp) != dps[dp]:
                g.write(L[n], dp, dps[dp])
                time.sleep(0.25)


def stage_setup():
    out = []
    for i in range(3):
        g = GatewayLan(IP)
        t = time.perf_counter()
        online = g.connect(attempts=2)
        out.append((time.perf_counter() - t) * 1e3)
        g.close()
        time.sleep(4)
    RESULTS["setup_connect_ms"] = stats(out)
    RESULTS["online_count"] = len(online)
    print("SETUP connect+subdev_query (ms):", RESULTS["setup_connect_ms"], "online:", len(online), flush=True)


def with_gateway(fn):
    g = GatewayLan(IP)
    g.connect()
    snap = snapshot(g, ["L1", "L3", "L5"])
    try:
        fn(g)
    finally:
        restore(g, snap)
        g.close()


def stage_latency(g):
    cid, o = L["L1"], g.read(L["L1"])
    res = {"on_off": [], "brightness": [], "cct": []}
    b0, c0 = o[DP_BRIGHT], o[DP_CCT]
    b_alt, c_alt = 300, (c0 + 300 if c0 <= 700 else c0 - 300)
    for i in range(10):
        res["on_off"].append(g.write(cid, DP_ON, i % 2 == 1)); time.sleep(1.5)
    g.write(cid, DP_ON, True); time.sleep(1)
    for i in range(10):
        res["brightness"].append(g.write(cid, DP_BRIGHT, b_alt if i % 2 == 0 else b0)); time.sleep(1.5)
    for i in range(10):
        res["cct"].append(g.write(cid, DP_CCT, c_alt if i % 2 == 0 else c0)); time.sleep(1.5)
    RESULTS["latency_ack_ms"] = {k: stats(v) for k, v in res.items()}
    print("LATENCY ack ms:", json.dumps(RESULTS["latency_ack_ms"]), flush=True)


def stage_rate(g):
    cid, o = L["L1"], g.read(L["L1"])
    b0 = o[DP_BRIGHT]
    rows = {}
    for interval in (1.0, 0.5, 0.25, 0.12):
        acks, errs, t_start = [], 0, time.perf_counter()
        nxt = t_start
        for i in range(12):
            try:
                acks.append(g.write(cid, DP_BRIGHT, 300 if i % 2 == 0 else b0))
            except Exception:  # noqa: BLE001
                errs += 1
            nxt += interval
            time.sleep(max(0.0, nxt - time.perf_counter()))
        wall = time.perf_counter() - t_start
        rows[str(interval)] = {"ack_ms": stats(acks), "errors": errs, "wall_s": round(wall, 2),
                               "achieved_hz": round(12 / wall, 2), "target_hz": round(1 / interval, 2)}
        print(f"RATE interval={interval}s ->", json.dumps(rows[str(interval)]), flush=True)
        if errs or (acks and st.mean(acks) > interval * 1000 * 0.9):
            print("RATE: stopping escalation (errors or acks slower than the interval)", flush=True)
            break
        time.sleep(2)
    RESULTS["rate"] = rows


def stage_persist(g, seconds):
    cid, o = L["L1"], g.read(L["L1"])
    b0 = o[DP_BRIGHT]
    t0, hb, cmd, fails, last_cmd = time.time(), [], [], 0, 0
    while time.time() - t0 < seconds:
        t = time.perf_counter()
        try:
            r = g._dev.heartbeat(nowait=False)
            ok = not (isinstance(r, dict) and r.get("Error"))
        except Exception:  # noqa: BLE001
            ok = False
        hb.append(((time.perf_counter() - t) * 1e3, ok))
        if not ok:
            fails += 1
        if time.time() - last_cmd > 30:
            try:
                cmd.append(g.write(cid, DP_BRIGHT, 300 if len(cmd) % 2 == 0 else b0))
            except Exception:  # noqa: BLE001
                fails += 1
            last_cmd = time.time()
        time.sleep(8)
    RESULTS["persist"] = {"seconds": seconds, "heartbeats": len(hb), "failures": fails,
                          "heartbeat_ms": stats([x for x, ok in hb if ok]), "command_ack_ms": stats(cmd)}
    print("PERSIST:", json.dumps(RESULTS["persist"]), flush=True)


def stage_sequence(g):
    chain = [L["L3"], L["L1"], L["L5"]]  # right track, front -> bed
    rows = {}
    for step in (1.0, 0.5, 0.25):
        for c in chain:
            g.write(c, DP_ON, False)
            time.sleep(0.3)
        t_start, late = time.perf_counter(), []
        nxt = t_start
        prev = None
        for i in range(9):  # 3 loops of 3 fixtures
            c = chain[i % 3]
            s = time.perf_counter()
            if prev:
                g.write(prev, DP_ON, False)
            g.write(c, DP_ON, True)
            late.append(max(0.0, (time.perf_counter() - s) * 1e3 - step * 1e3))
            prev = c
            nxt += step
            time.sleep(max(0.0, nxt - time.perf_counter()))
        wall = time.perf_counter() - t_start
        rows[str(step)] = {"wall_s": round(wall, 2), "expected_s": round(9 * step, 2),
                           "step_overrun_ms": stats(late)}
        print(f"SEQUENCE step={step}s ->", json.dumps(rows[str(step)]), flush=True)
        time.sleep(2)
    RESULTS["sequence"] = rows


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    psec = int(sys.argv[sys.argv.index("--persist-seconds") + 1]) if "--persist-seconds" in sys.argv else 180
    try:
        if which in ("setup", "all"):
            stage_setup()
        if which in ("latency", "all"):
            with_gateway(stage_latency)
        if which in ("rate", "all"):
            with_gateway(stage_rate)
        if which in ("sequence", "all"):
            with_gateway(stage_sequence)
        if which in ("persist", "all"):
            with_gateway(lambda g: stage_persist(g, psec))
    finally:
        out = ROOT / "reports" / "raw" / f"bench_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
        print("saved", out.name)
