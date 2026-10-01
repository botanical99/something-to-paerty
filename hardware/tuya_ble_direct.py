"""Direct BLE client for Tuya BLE (protocol v3, GATT service 0x1910) using bleak.

Status: experimental. Protocol behaviour follows the MIT-licensed reference
https://github.com/PlusPlus-ua/ha_tuya_ble (tuya_ble.py); this is an independent implementation.

Session:  connect -> DEVICE_INFO (login key) -> PAIR/auth (session key) -> DP commands.
The "pair" step here is the normal per-connection authentication; a device that is already
bound answers "already paired" (2) and its binding is NOT changed. We refuse to run it against
a device whose advertisement says it is unbound.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from struct import pack, unpack

from bleak import BleakClient
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CHAR_NOTIFY = "00002b10-0000-1000-8000-00805f9b34fb"
CHAR_WRITE = "00002b11-0000-1000-8000-00805f9b34fb"
SERVICE_UUID = "0000a201-0000-1000-8000-00805f9b34fb"
MFR_ID = 0x07D0
GATT_MTU = 20

F_DEVICE_INFO, F_PAIR, F_DPS, F_STATUS = 0x0000, 0x0001, 0x0002, 0x0003
RX_DP, RX_TIME_DP, RX_SIGN_DP, RX_SIGN_TIME_DP = 0x8001, 0x8003, 0x8004, 0x8005
RX_TIME1_REQ, RX_TIME2_REQ = 0x8011, 0x8012

DT_RAW, DT_BOOL, DT_VALUE, DT_STRING, DT_ENUM, DT_BITMAP = range(6)


class TuyaBleError(Exception):
    pass


# ---------------------------------------------------------------- crypto / framing helpers
def _cbc(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    c = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = c.encryptor() if encrypt else c.decryptor()
    return op.update(data) + op.finalize()


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def pack_varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | 0x80 if v else b)
        if not v:
            return bytes(out)


def unpack_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    for _ in range(4):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return result, pos
    raise TuyaBleError("bad varint")


# ---------------------------------------------------------------- advertisement decoding
def decode_advert(adv) -> dict | None:
    """Parse a bleak AdvertisementData for Tuya BLE fields (None if not Tuya)."""
    mfr = (adv.manufacturer_data or {}).get(MFR_ID)
    if not mfr or len(mfr) < 22:
        return None
    svc = (adv.service_data or {}).get(SERVICE_UUID, b"")
    return {
        "is_bound": bool(mfr[0] & 0x80), "flags": mfr[0], "protocol": mfr[1],
        "enc_uuid": bytes(mfr[6:22]),
        "svc_type": svc[0] if svc else None, "svc_payload": bytes(svc[1:]) if svc else b"",
    }


def try_decrypt_uuid(dec: dict, candidate_keys: list[bytes]) -> list[tuple[bytes, str]]:
    """Return [(key_material, decrypted_text)] for every candidate that yields printable ASCII."""
    out = []
    for km in candidate_keys:
        key = hashlib.md5(km).digest()
        try:
            txt = _cbc(key, key, dec["enc_uuid"], False).decode("ascii")
        except (UnicodeDecodeError, ValueError):
            continue
        if txt.isprintable():
            out.append((km, txt))
    return out


# ---------------------------------------------------------------- client
class TuyaBleClient:
    def __init__(self, address: str, uuid: str, local_key: str, device_id: str,
                 protocol_version: int = 3, is_bound: bool = True):
        if not is_bound:
            raise TuyaBleError("refusing to authenticate to an UNBOUND device (would bind it)")
        self.address = address
        self._uuid, self._device_id = uuid, device_id
        self._lk6 = local_key[:6].encode()
        self._login_key = hashlib.md5(self._lk6).digest()
        self._session_key = self._auth_key = None
        self._protocol = protocol_version
        self._client: BleakClient | None = None
        self._seq = 0
        self._waiters: dict[int, asyncio.Future] = {}
        self._buf = None
        self._exp_len = 0
        self._exp_pkt = 0
        self.dps: dict[int, tuple[int, object]] = {}
        self._dp_event = asyncio.Event()
        self.paired = False
        self.timings: dict[str, float] = {}
        self.disconnected = asyncio.Event()

    # -- connection
    async def connect(self, timeout: float = 15.0) -> None:
        t0 = time.perf_counter()
        self.disconnected.clear()
        self._client = BleakClient(self.address, timeout=timeout, disconnected_callback=lambda c: self.disconnected.set())
        await self._client.connect()
        t1 = time.perf_counter()
        await self._client.start_notify(CHAR_NOTIFY, self._on_notify)
        t2 = time.perf_counter()
        await self._request(F_DEVICE_INFO, b"")
        t3 = time.perf_counter()
        pair = bytearray(self._uuid.encode() + self._lk6 + self._device_id.encode())
        pair += b"\x00" * (44 - len(pair))
        await self._request(F_PAIR, bytes(pair))
        t4 = time.perf_counter()
        if not self.paired:
            raise TuyaBleError("device did not accept authentication")
        self.timings = {"gatt_connect_ms": (t1 - t0) * 1e3, "notify_ms": (t2 - t1) * 1e3,
                        "device_info_ms": (t3 - t2) * 1e3, "auth_ms": (t4 - t3) * 1e3, "total_ms": (t4 - t0) * 1e3}

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected and self.paired and not self.disconnected.is_set())

    async def disconnect(self) -> None:
        c, self._client = self._client, None
        self.paired = False
        if c and c.is_connected:
            try:
                await c.stop_notify(CHAR_NOTIFY)
            except Exception:  # noqa: BLE001
                pass
            await c.disconnect()

    # -- high-level
    async def read_state(self, quiet: float = 0.6, timeout: float = 6.0) -> dict[int, tuple[int, object]]:
        """Ask the device to report all datapoints; return them."""
        self.dps.clear()
        self._dp_event.clear()
        await self._request(F_STATUS, b"")
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            try:
                await asyncio.wait_for(self._dp_event.wait(), quiet)
                self._dp_event.clear()
                last = time.monotonic()
            except asyncio.TimeoutError:
                if last or self.dps:
                    break
        return dict(self.dps)

    async def set_dps(self, items: list[tuple[int, int, object]]) -> float:
        """items = [(dp_id, dp_type, value)]. Returns ack latency in ms."""
        data = bytearray()
        for dp_id, dp_type, value in items:
            raw = self._encode(dp_type, value)
            data += pack(">BBB", dp_id, dp_type, len(raw)) + raw
        t0 = time.perf_counter()
        await self._request(F_DPS, bytes(data))
        return (time.perf_counter() - t0) * 1e3

    @staticmethod
    def _encode(t: int, v) -> bytes:
        if t == DT_BOOL:
            return b"\x01" if v else b"\x00"
        if t == DT_VALUE:
            return pack(">i", int(v))
        if t == DT_ENUM:
            return pack(">B", int(v))
        if t == DT_STRING:
            return str(v).encode()
        return bytes(v)

    # -- framing
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _build(self, seq: int, code: int, data: bytes, response_to: int = 0) -> list[bytes]:
        key, flag = (self._login_key, 4) if code == F_DEVICE_INFO else (self._session_key, 5)
        raw = bytearray(pack(">IIHH", seq, response_to, code, len(data)) + data)
        raw += pack(">H", crc16(bytes(raw)))
        raw += b"\x00" * (-len(raw) % 16)
        iv = secrets.token_bytes(16)
        enc = bytes([flag]) + iv + _cbc(key, iv, bytes(raw), True)
        pkts, pos, n = [], 0, 0
        while pos < len(enc):
            p = bytearray(pack_varint(n))
            if n == 0:
                p += pack_varint(len(enc)) + bytes([self._protocol << 4])
            part = enc[pos:pos + GATT_MTU - len(p)]
            p += part
            pkts.append(bytes(p))
            pos += len(part)
            n += 1
        return pkts

    async def _send(self, code: int, data: bytes, response_to: int = 0, seq: int | None = None) -> int:
        seq = seq or self._next_seq()
        for p in self._build(seq, code, data, response_to):
            await self._client.write_gatt_char(CHAR_WRITE, p, response=False)
        return seq

    async def _request(self, code: int, data: bytes, timeout: float = 10.0):
        seq = self._next_seq()
        fut = asyncio.get_running_loop().create_future()
        self._waiters[seq] = fut
        await self._send(code, data, seq=seq)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise TuyaBleError(f"timeout waiting for response to code 0x{code:04x}") from None
        finally:
            self._waiters.pop(seq, None)

    # -- receive
    def _on_notify(self, _h, data: bytearray) -> None:
        try:
            n, pos = unpack_varint(data, 0)
            if n == 0:
                self._buf = bytearray()
                self._exp_len, pos = unpack_varint(data, pos)
                pos += 1  # protocol byte
                self._exp_pkt = 0
            elif n != self._exp_pkt or self._buf is None:
                self._buf = None
                return
            self._buf += data[pos:]
            self._exp_pkt = n + 1
            if len(self._buf) >= self._exp_len:
                buf, self._buf = bytes(self._buf), None
                self._parse(buf)
        except Exception:  # noqa: BLE001  (never raise into the BLE callback thread)
            self._buf = None

    def _parse(self, buf: bytes) -> None:
        flag = buf[0]
        key = {1: self._auth_key, 4: self._login_key, 5: self._session_key}.get(flag)
        if key is None:
            return
        raw = _cbc(key, buf[1:17], buf[17:], False)
        seq, resp_to, code, length = unpack(">IIHH", raw[:12])
        data = raw[12:12 + length]
        result = 0
        if code == F_DEVICE_INFO and len(data) >= 46:
            self._session_key = hashlib.md5(self._lk6 + data[6:12]).digest()
            self._auth_key = data[14:46]
            self.info = {"device_version": f"{data[0]}.{data[1]}", "protocol": f"{data[2]}.{data[3]}",
                         "hardware": f"{data[12]}.{data[13]}", "bound": data[5] != 0}
        elif code == F_PAIR and len(data) == 1:
            result = data[0]
            self.paired = result in (0, 2)  # 2 == already paired (normal for bound devices)
            if result == 2:
                result = 0
        elif code == F_STATUS and len(data) == 1:
            result = data[0]
        elif code in (RX_DP, RX_TIME_DP, RX_SIGN_DP, RX_SIGN_TIME_DP):
            self._rx_dps(code, data)
            asyncio.get_event_loop().create_task(self._ack_dp(code, seq, data))
        elif code == RX_TIME1_REQ:
            ts = str(int(time.time() * 1000)).encode() + pack(">h", -int(time.timezone / 36))
            asyncio.get_event_loop().create_task(self._send(code, ts, response_to=seq))
        elif code == RX_TIME2_REQ:
            t = time.localtime()
            d = pack(">BBBBBBBh", t.tm_year % 100, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, t.tm_wday,
                     -int(time.timezone / 36))
            asyncio.get_event_loop().create_task(self._send(code, d, response_to=seq))
        if resp_to and (fut := self._waiters.get(resp_to)) and not fut.done():
            if result == 0:
                fut.set_result((code, data))
            else:
                fut.set_exception(TuyaBleError(f"device returned error {result} for code 0x{code:04x}"))

    def _rx_dps(self, code: int, data: bytes) -> None:
        pos = 0
        if code in (RX_SIGN_DP, RX_SIGN_TIME_DP):
            pos = 3
        if code in (RX_TIME_DP, RX_SIGN_TIME_DP):
            t = data[pos]
            pos += 1 + (13 if t == 0 else 4)
        while len(data) - pos >= 4:
            dp_id, dp_type, ln = data[pos], data[pos + 1], data[pos + 2]
            pos += 3
            raw = data[pos:pos + ln]
            pos += ln
            if dp_type == DT_BOOL:
                val = int.from_bytes(raw, "big") != 0
            elif dp_type in (DT_VALUE, DT_ENUM):
                val = int.from_bytes(raw, "big", signed=True)
            elif dp_type == DT_STRING:
                val = raw.decode(errors="replace")
            else:
                val = bytes(raw).hex()
            self.dps[dp_id] = (dp_type, val)
        self._dp_event.set()

    async def _ack_dp(self, code: int, seq: int, data: bytes) -> None:
        try:
            if code in (RX_SIGN_DP, RX_SIGN_TIME_DP):
                await self._send(code, pack(">HBB", int.from_bytes(data[:2], "big"), data[2], 0), response_to=seq)
            else:
                await self._send(code, b"", response_to=seq)
        except Exception:  # noqa: BLE001
            pass
