"""Gateway LAN adapter (tinytuya): one persistent, serialised socket to the WG-S; lamps addressed by cid.

REAL-HARDWARE VERIFIED so far: connect, subdev_query, per-child status reads (see HARDWARE_DISCOVERY.md).
Design rules learned from third-party reports and our own runs:
  * the gateway tolerates very few concurrent connections -> ONE socket, all calls serialised by a lock;
  * one DP per CONTROL call (bundled DP writes were unreliable for mesh lamps elsewhere);
  * always close(): a leaked socket blocks the gateway for a while.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import tinytuya

ROOT = Path(__file__).resolve().parent.parent

DP_ON, DP_MODE, DP_BRIGHT, DP_CCT = "20", "21", "22", "23"


def load_account() -> tuple[dict, list[dict]]:
    devs = json.loads((ROOT / "config" / "tuya_devices.json").read_text(encoding="utf-8"))
    return next(d for d in devs if not d["sub"]), [d for d in devs if d["sub"]]


class GatewayLan:
    is_simulated = False

    def __init__(self, ip: str, version: float = 3.3):
        self.gw, self.lamps = load_account()
        self.ip, self.version = ip, version
        self._dev: tinytuya.Device | None = None
        self._kids: dict[str, tinytuya.Device] = {}
        self._lock = threading.Lock()
        self.last_command_ms: float | None = None

    def connect(self, attempts: int = 4) -> list[str]:
        """Open the socket and return the online child cids. Raises if the gateway never answers."""
        for i in range(attempts):
            self._dev = tinytuya.Device(self.gw["id"], address=self.ip, local_key=self.gw["local_key"],
                                        version=self.version, persist=True, connection_timeout=5,
                                        connection_retry_limit=1)
            r = self._dev.subdev_query()
            if isinstance(r, dict) and "Error" not in r:
                return list((r.get("data") or {}).get("online", []))
            self._dev.close()
            time.sleep(8)
        raise ConnectionError("gateway did not answer (offline, wrong key, or its connection slots are busy)")

    def close(self) -> None:
        self._kids.clear()
        if self._dev:
            self._dev.close()
            self._dev = None

    def _child(self, cid: str) -> tinytuya.Device:
        if cid not in self._kids:
            self._kids[cid] = tinytuya.Device(cid, cid=cid, parent=self._dev, version=self.version)
        return self._kids[cid]

    def read(self, cid: str) -> dict:
        with self._lock:
            r = self._child(cid).status()
        if not isinstance(r, dict) or "dps" not in r:
            raise IOError(f"status failed for {cid}: {r}")
        return r["dps"]

    def write(self, cid: str, dp: str, value) -> float:
        """Set ONE datapoint. Returns the gateway's acknowledgement time in ms."""
        with self._lock:
            t = time.perf_counter()
            r = self._child(cid).set_value(dp, value)
            ms = (time.perf_counter() - t) * 1e3
        self.last_command_ms = ms
        if r is None:  # tinytuya returns None on a silent timeout
            raise IOError(f"no response for {cid} dp {dp}")
        if isinstance(r, dict) and r.get("Error"):
            raise IOError(f"write failed for {cid} dp {dp}: {r}")
        return ms
