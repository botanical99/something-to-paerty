# HARDWARE_DISCOVERY.md — rev 3 (2026-10-01)

> **Status note:** this file records what was measured on the real hardware and is still the reference for it. The controller described in
> its last section ("Suggested next step") has since been built — see [README.md](README.md) and [REAL_HARDWARE_CHECKLIST.md](REAL_HARDWARE_CHECKLIST.md).

**Legend:** ✅ **VERIFIED on your real hardware** (measured/observed, and for visible effects, confirmed by you) · 👁 confirmed by your eyes · ⚠️ assumption / not yet verified · 📚 third-party claim.
No light, gateway or account was reset, unbound, re-paired, provisioned or updated. Every lamp I touched was restored to its previous state and read back.

---

## 1. Bottom line

| | |
|---|---|
| **Can the laptop control individual lights?** | ✅ **Yes — all six, individually**, locally over your LAN, **no Tuya Cloud at runtime** (cloud was used once, for the QR login that fetched the key). |
| **Direct BLE (bypassing the gateway)?** | ❌ **Not possible with what exists.** The lamps are gateway-managed mesh nodes: they don't advertise over BLE, there is no mesh key in your account data, and the only BLE-reachable device (the gateway itself) exposes no lamp controls. (§4) |
| **Control path that works** | Laptop → Wi-Fi → **WG-S gateway, Tuya LAN protocol 3.3, TCP 6668** → BLE mesh → lamp. |
| **Fast enough for effects?** | ✅ for a 3-light chase at **250 ms/step** (👁 clean). ⚠️ ceiling ≈ **4 commands/s** through one socket → whole-room per-light animation is limited; 100 ms effects are not realistic. (§6) |
| **One-time setup you did** | Smart Life QR login. That is all. No developer account. |

## 2. Account contents (✅ from the QR login)
7 devices: the gateway **"Kk modi"** (`wg2`, productKey `br85qgx86gsavad9`) and **6 lamps** (category `dj`, product `fkxcslivaluonzdp`, all online). **One local key is shared by all 7** — the lamps have *no key of their own*; they are addressed through the gateway by a mesh **node id (cid)**.

Datapoints (✅ read live from each lamp; ranges from the cloud spec 📚, behaviour verified 👁):

| DP | Meaning | Range / notes |
|---|---|---|
| `20` | on/off | bool |
| `21` | work mode | `"white"` (read-only for our purposes) |
| `22` | brightness | 10–1000. 👁 55 → 1000 was clearly brighter. |
| `23` | colour temperature | 0–1000. 👁 **1000 = WARM, 0 = COOL** (opposite of the common assumption). |
No RGB. Lamps 5 & 6 don't report DP 21.

## 3. The six fixtures (✅ identified by blink test, 👁 confirmed by you)

| # | App name | cid | Type | Physical location (your words, tidied) |
|---|---|---|---|---|
| L1 | Right | `01d82ce936` | long/linear | Right track, middle |
| L2 | lamp | `01d80ecedd` | long/linear | Left track, middle |
| L3 | Right | `01d80018a4` | spot | Right track, just ahead (front) of L1 |
| L4 | lamp 2 | `01ccc767c9` | spot | Left track, just ahead (front) of L2 |
| L5 | Right | `01ccc768fe` | spot | Right track, behind L1, near the bed |
| L6 | lamp 3 | `01ccc76813` | spot | Left track, behind L2, near the bed |

Saved to `config/tuya_fixtures.json` (git-ignored). Front→bed order: **RIGHT = L3 → L1 → L5; LEFT = L4 → L2 → L6.** (Interpretation "ahead = door side" is my reading of your descriptions — please correct if reversed.)

## 4. Direct BLE — what was tested and why it fails for the lamps
- ✅ **Correction to my earlier report:** the five `TY` devices I thought were your lights are **not in your account**. I decrypted each one's advert UUID against your account: only the gateway matched. They are neighbouring Tuya devices.
- ✅ The lamps never advertise on BLE; raw capture also showed zero Bluetooth-Mesh beacons/proxy adverts from anything.
- ✅ **BLE client works on real hardware:** `hardware/tuya_ble_direct.py` authenticated to the **gateway's** BLE service — GATT connect 2772 ms, device-info 296 ms, login 150 ms (≈3.3 s to be ready); reply "already paired", binding untouched.
- ✅ The gateway answers no datapoint queries over BLE (status request timed out) — it has no lamp controls there.
- ⚠️ Reaching the lamps over BLE would need the SIG-mesh network/app keys, which are not exposed by the login flow and could only be obtained by re-provisioning (not done, not recommended).
→ Per your rule, fell back to gateway LAN.

## 5. Gateway LAN path (✅)
- Gateway `192.168.100.171`, protocol **3.3** (3.4/3.5 are rejected). ✅ `subdev_query` lists all six cids online; per-lamp status reads return live DPs.
- ✅ Control confirmed on L1–L6 (off/on blink each, restored exactly), and on L1: brightness 👁 and CCT 👁.
- Operating rules learned: **one persistent socket, serialise everything, one DP per command, always close the socket.** After a close, reconnecting within seconds was unreliable (§6).

## 6. Measurements (✅ all from your hardware; "ack" = gateway acknowledged, not necessarily "lamp visibly changed")

| Metric | Result |
|---|---|
| Connection + sub-device list (3 tries) | 885 ms best; **2 of 3 took ≈13.7 s** (reconnect right after closing the previous socket). → keep one persistent connection; never reconnect per command. |
| On/off ack (n=10, L1) | min 143 / **median 201** / p90 342 / max 453 ms |
| Brightness ack, alternating 55↔300 (n=10) | median 12 ms (max 144) |
| CCT ack, alternating (n=10) | median 12 ms (max 23) |
| Later acks, same commands, varying load | 116–860 ms, median 180–310 ms |
| Repeated brightness commands, L1, 12 each | 1 Hz ✅, 2 Hz ✅, 4 Hz ✅ (0 errors). Asked for 8.3 Hz → **achieved 4.14 Hz**, acks 132–347 ms: the socket is ack-limited at **≈4 commands/s**. |
| 3-light on/off chase, right track, 9 steps | steps of 1.0 / 0.5 / 0.25 s kept schedule (≤76 ms overrun); 👁 you judged the second run **"clean at all speeds"**. |
| Persistent connection, 180 s, one command every 30 s | ✅ **Stayed usable: 5/5 commands acked (82–186 ms, median 122), 0 failures.** The heartbeat calls each took ≈5 s (the gateway doesn't answer tinytuya's heartbeat, so that is a timeout, not a liveness signal) — I did *not* verify idle timeouts beyond 30 s of silence. |
| Final state check | ✅ all six lamps read back identical to their starting values (on, same brightness/CCT). |

The ack variance (12 ms vs 100–800 ms for the same command type) means timing is **not deterministic**; the effects engine must tolerate jitter.

**Effect feasibility (honest reading):**

| Step time | Verdict |
|---|---|
| 1 s | ✅ any effect, all six lamps |
| 500 ms | ✅ chase/wave over 3 lamps verified; 6 lamps ≈ 2–3 writes/step is fine |
| 250 ms | ✅ **3-light chase verified 👁**; ⚠️ all-six simultaneous updates exceed the ~4 cmd/s budget |
| 100 ms | ❌ not realistic (≈ 0.4 commands per lamp per step across the room) |
Music mode should therefore use event-style cues (beat → advance one lamp, bass → brief brightness jump) with a global command budget of ≈ 3–4/s and no per-frame writes.

## 7. NOT verified yet (⚠️)
- That the gateway ack ≈ lamp action time (true visual latency was only judged by eye, not instrumented).
- Left-track timing/sync with right-track simultaneously; all six lamps under sustained load.
- Whether the Smart Life app / iPhone still controls lamps while our socket is open (the gateway tolerates few connections).
- Whether the state we read back is the lamp's real state or the gateway's cache (restores matched, but this is not proof).
- Behaviour after a gateway reboot / Wi-Fi drop / key change (re-run `tools/tuya_login.py --refresh` if lamps are ever re-paired; the key may change).
- Brightness-ramp or CCT-sweep effects visually (single steps only verified).

## 8. Security / state
- Secrets live only in `config/tuya_session.json` (login token) and `config/tuya_devices.json` (local key) — both git-ignored (✅ `git check-ignore`). The key appears in no other file; terminal output always masks it.
- To forget the login locally: delete `config/tuya_session.json`.
- Tools: `tools/tuya_login.py` (QR login/refresh) · `tools/tuya_correlate.py` · `tools/identify.py` · `tools/lan_readonly.py` · `tools/lan_bench.py` · `tools/visual_check.py` · `tools/ble_auth_test.py`; adapters `hardware/tuya_lan.py` (works) and `hardware/tuya_ble_direct.py` (works for the gateway only).

## 9. Suggested next step
Phase 3 on top of `hardware/tuya_lan.py`: a single async worker owning the socket, a **global ≈4 cmd/s budget with coalescing** (drop superseded writes), heartbeat + reconnect with back-off, track order from `tuya_fixtures.json`, brightness 10–1000, CCT 1000 = warm. Then `discover → list lights → click to blink/control` becomes the web UI's first screen.
