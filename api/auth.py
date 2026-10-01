"""LAN-only access control.

  1. Network gate: only loopback / private (RFC1918) / link-local clients are served at all.
  2. PIN pairing: a phone proves it knows the 6-digit PIN once (the QR code does that for you) and
     gets a long-lived signed cookie. The laptop itself (loopback) needs nothing.
  3. Brute-force brake: 5 wrong PINs from one address -> locked out for 60 s.

The PIN lives in config/auth.json (git-ignored). Nothing here uses the internet or Tuya Cloud.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from http.cookies import SimpleCookie
from pathlib import Path

COOKIE = "lights_session"
MAX_FAILS, LOCK_S = 5, 60.0
OPEN_PATHS = ("/login", "/api/login", "/pair", "/static/", "/manifest.webmanifest", "/sw.js", "/icons/", "/favicon.ico")


_LAN_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",   # IPv4 loopback / private / link-local
    "::1/128", "fc00::/7", "fe80::/10")]                                                 # IPv6 loopback / ULA / link-local


def _ip(host: str | None):
    try:
        ip = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def is_lan_address(host: str | None) -> bool:
    """Deliberately an explicit allow-list (Python's is_private also accepts documentation ranges)."""
    ip = _ip(host)
    return ip is not None and any(ip in n for n in _LAN_NETS)


def is_loopback(host: str | None) -> bool:
    ip = _ip(host)
    return ip is not None and ip.is_loopback


class Auth:
    def __init__(self, path: Path, *, require_pin: bool = True, lan_only: bool = True, trust_localhost: bool = True):
        self.path = Path(path)
        self.require_pin, self.lan_only, self.trust_localhost = require_pin, lan_only, trust_localhost
        self.pin, self.secret = self._load()
        self._fails: dict[str, list[float]] = {}

    def _load(self) -> tuple[str, str]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if d.get("pin") and d.get("secret"):
                return str(d["pin"]), str(d["secret"])
        except (OSError, ValueError):
            pass
        return self.rotate()

    def rotate(self) -> tuple[str, str]:
        """New PIN + new cookie secret: every paired phone is signed out."""
        pin, secret = f"{secrets.randbelow(1_000_000):06d}", secrets.token_hex(32)
        self.pin, self.secret = pin, secret
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"pin": pin, "secret": secret}), encoding="utf-8")
        except OSError:
            pass
        return pin, secret

    # ---- session token
    def token(self) -> str:
        return hmac.new(self.secret.encode(), b"lights-session-v1", hashlib.sha256).hexdigest()

    def cookie_ok(self, header: str | None) -> bool:
        if not header:
            return False
        c = SimpleCookie()
        try:
            c.load(header)
        except Exception:  # noqa: BLE001
            return False
        m = c.get(COOKIE)
        return m is not None and hmac.compare_digest(m.value, self.token())

    def pin_ok(self, pin: str | None) -> bool:
        return bool(pin) and hmac.compare_digest(str(pin), self.pin)

    # ---- brute-force brake
    def locked(self, ip: str) -> float:
        now = time.monotonic()
        fails = [t for t in self._fails.get(ip, []) if now - t < LOCK_S]
        self._fails[ip] = fails
        return LOCK_S - (now - fails[0]) if len(fails) >= MAX_FAILS else 0.0

    def fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.monotonic())

    def success(self, ip: str) -> None:
        self._fails.pop(ip, None)

    # ---- decisions for the ASGI middleware
    def decide(self, host: str | None, path: str, headers: dict[str, str]) -> str:
        """-> 'ok' | 'forbidden' | 'login' (pairing needed)"""
        if self.lan_only and not is_lan_address(host):
            return "forbidden"
        if any(path == p or (p.endswith("/") and path.startswith(p)) for p in OPEN_PATHS):
            return "ok"
        if not self.require_pin:
            return "ok"
        if self.trust_localhost and is_loopback(host):
            return "ok"
        if self.cookie_ok(headers.get("cookie")):
            return "ok"
        key = headers.get("x-lights-pin") or ""
        if not key:
            a = headers.get("authorization", "")
            if a.lower().startswith("bearer "):
                key = a[7:].strip()
        if key and self.pin_ok(key):
            return "ok"
        return "login"


class AuthMiddleware:
    """Pure ASGI (so it protects the WebSocket too)."""

    def __init__(self, app, auth: Auth):
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        client = scope.get("client")
        host = client[0] if client else None
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "/")
        verdict = self.auth.decide(host, path, headers)
        if verdict == "ok":
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return None
        if verdict == "forbidden":
            return await _respond(send, 403, b'{"detail":"LAN access only"}', "application/json")
        accepts_html = "text/html" in headers.get("accept", "") and not path.startswith("/api/")
        if accepts_html:
            return await _respond(send, 302, b"", "text/plain", [(b"location", b"/login")])
        return await _respond(send, 401, b'{"detail":"pairing required"}', "application/json")


async def _respond(send, status: int, body: bytes, ctype: str, extra: list | None = None):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store")] + (extra or [])})
    await send({"type": "http.response.body", "body": body})


