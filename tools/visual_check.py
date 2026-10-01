"""Set ONE datapoint on ONE lamp, hold, then restore it. For eyeball checks.

    python tools/visual_check.py <cid> <dp> <value> <hold_seconds> [gateway_ip]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hardware.tuya_lan import GatewayLan  # noqa: E402

cid, dp, value, hold = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
g = GatewayLan(sys.argv[5] if len(sys.argv) > 5 else "192.168.100.171")
try:
    g.connect()
    before = g.read(cid)
    a1 = g.write(cid, dp, value)
    mid = g.read(cid)
    print(f"SET dp{dp}={value} ack={a1:.0f}ms  readback={mid}", flush=True)
    time.sleep(hold)
    a2 = g.write(cid, dp, before[dp])
    time.sleep(0.5)
    after = g.read(cid)
    print(f"RESTORED dp{dp}={before[dp]} ack={a2:.0f}ms  now={after}  ok={after.get(dp) == before[dp]}")
finally:
    g.close()
