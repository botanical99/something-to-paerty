"""Async wrapper around the proven hardware/tuya_lan.py: one persistent connection, failure
detection, automatic reconnect with back-off, optional gateway re-discovery if its IP changes.

All blocking tinytuya calls run on ONE dedicated thread, so the gateway only ever sees a single,
serialised conversation (it tolerates very few connections).
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable

log = logging.getLogger("link")


class LinkDown(Exception):
    pass


class AsyncGatewayLink:
    def __init__(self, ip: str, version: float = 3.3):
        self.ip, self.version = ip, version
        self._g: Any = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gateway")
        self.state = "offline"           # offline | connecting | online
        self.online_cids: list[str] = []
        self.fail_streak = 0
        self.reconnects = 0
        self.last_ok = 0.0
        self.last_error = ""
        self.on_online: Callable[[], Awaitable[None]] | None = None
        self.on_state: Callable[[str], None] | None = None
        self._task: asyncio.Task | None = None
        self._stop = False

    def _make_gateway(self):
        """Factory hook: the real adapter by default (imported lazily so the simulator never needs tinytuya)."""
        from hardware.tuya_lan import GatewayLan
        return GatewayLan(self.ip, self.version)

    # -- lifecycle
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._supervisor(), name="link-supervisor")

    async def close(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
        await self._drop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _set_state(self, s: str) -> None:
        if s != self.state:
            log.info("gateway link: %s -> %s", self.state, s)
            self.state = s
            if self.on_state:
                self.on_state(s)

    async def _run(self, fn, *a):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *a)

    # -- operations
    async def write(self, cid: str, dp: str, value) -> float:
        if self.state != "online" or self._g is None:
            raise LinkDown("gateway offline")
        try:
            ms = await self._run(self._g.write, cid, dp, value)
        except Exception as e:  # noqa: BLE001
            await self._failed(e)
            raise
        self.fail_streak, self.last_ok = 0, time.monotonic()
        return ms

    async def read(self, cid: str) -> dict:
        if self.state != "online" or self._g is None:
            raise LinkDown("gateway offline")
        try:
            dps = await self._run(self._g.read, cid)
        except Exception as e:  # noqa: BLE001
            await self._failed(e)
            raise
        self.fail_streak, self.last_ok = 0, time.monotonic()
        return dps

    async def read_all(self, cids: list[str]) -> dict[str, dict]:
        out = {}
        for c in cids:
            out[c] = await self.read(c)
        return out

    async def _failed(self, e: Exception) -> None:
        self.fail_streak += 1
        self.last_error = f"{type(e).__name__}: {e}"
        log.warning("gateway operation failed (%d in a row): %s", self.fail_streak, self.last_error)
        if self.fail_streak >= 2:
            await self._drop()

    async def _drop(self) -> None:
        g, self._g = self._g, None
        self._set_state("offline")
        if g:
            try:
                await self._run(g.close)
            except Exception:  # noqa: BLE001
                pass

    # -- supervisor: connect / reconnect / health-check
    async def _supervisor(self) -> None:
        backoff = 2.0
        failures = 0
        while not self._stop:
            if self.state == "offline":
                self._set_state("connecting")
                g = self._make_gateway()
                try:
                    online = await self._run(g.connect, 1)
                    self._g, self.online_cids = g, online
                    self.fail_streak, self.last_ok = 0, time.monotonic()
                    backoff, failures = 2.0, 0
                    self._set_state("online")
                    self.reconnects += 1
                    if self.on_online:
                        await self.on_online()
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"{type(e).__name__}: {e}"
                    try:
                        await self._run(g.close)
                    except Exception:  # noqa: BLE001
                        pass
                    self._set_state("offline")
                    failures += 1
                    if failures % 3 == 0:
                        await self._rediscover()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
            else:
                await asyncio.sleep(1.0)
                if self.state == "online" and time.monotonic() - self.last_ok > 12 and self.online_cids:
                    try:  # cheap read keeps the session honest and detects a dead socket
                        await self.read(self.online_cids[0])
                    except Exception:  # noqa: BLE001
                        pass

    async def _rediscover(self) -> None:
        """If the gateway's DHCP address changed, find it again by its Tuya id (UDP broadcast listen)."""
        try:
            from hardware.probe_lan import tinytuya_scan
            from hardware.tuya_lan import load_account
            gw_id = load_account()[0]["id"]
            found = await self._run(tinytuya_scan, 8)
            for d in found:
                if d.get("gwId") == gw_id and d.get("ip") and d["ip"] != self.ip:
                    log.warning("gateway moved %s -> %s", self.ip, d["ip"])
                    self.ip = d["ip"]
        except Exception as e:  # noqa: BLE001
            log.debug("rediscovery failed: %s", e)
