"""Identify ONE lamp: read its state, blink it (off 1.5 s, then on), restore the exact previous state.

    python tools/identify.py <cid> [gateway_ip]

Only the lamp's own on/off datapoint is toggled; brightness/CCT are never touched and are verified
afterwards. If the lamp was already off it is turned on for 1.5 s and then off again.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hardware.tuya_lan import DP_ON, GatewayLan  # noqa: E402

cid = sys.argv[1]
g = GatewayLan(sys.argv[2] if len(sys.argv) > 2 else "192.168.100.171")
try:
    online = g.connect()
    if cid not in online:
        sys.exit(f"{cid} is not reported online: {online}")
    before = g.read(cid)
    was_on = bool(before.get(DP_ON))
    print("BEFORE", before, flush=True)
    acks = [g.write(cid, DP_ON, not was_on)]
    time.sleep(float(sys.argv[3]) if len(sys.argv) > 3 else 1.5)
    acks.append(g.write(cid, DP_ON, was_on))
    time.sleep(0.8)
    after = g.read(cid)
    print("AFTER ", after, flush=True)
    restored = after.get(DP_ON) == before.get(DP_ON) and after.get("22") == before.get("22") and after.get("23") == before.get("23")
    print(f"RESTORED={restored}  gateway_ack_ms={[round(a) for a in acks]}")
finally:
    g.close()
