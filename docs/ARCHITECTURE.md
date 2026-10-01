# Architecture notes

Bedroom lighting controller; optimised for reliability and fun, not for generality.

## Layers

```
web/ (PWA)  ──REST/WebSocket──►  api/server.py ──►  core/controller.py  (modes, scenes, STOP/restore, crash snapshot)
                                                       │            │
                                              core/effects.py   audio/director.py ─► audio/profiles.py
                                                       └─────┬──────┘
                                                      core/scheduler.py   (the ONLY writer)
                                                             │
                                          hardware/gateway_link.py  (supervisor: connect / reconnect / back-off)
                                             │                       │
                                   hardware/tuya_lan.py          hardware/simulator.py
                                   (tinytuya, REAL)              (SimGateway, SIMULATOR)
```

## Scheduler (core/scheduler.py)

* Commands keyed by `(lamp, datapoint)`. A newer request for the same key replaces the queued one (coalescing).
* Global rate limit (default 4/s, spacing measured between command *starts*).
* Priority: `CRITICAL` (NORMAL / STOP / shutdown) > `HIGH` (manual taps, restore, returns) > `NORMAL` (effects) > `LOW` (ambient).
* TTL: a cue that the budget could not deliver in time is dropped, so lights show something recent rather than lag the music.
  "Back to base" commands carry no TTL.
* Epochs: every mode change bumps the epoch; an effect holding an old epoch is rejected and its queued commands flushed.
* In-flight tracking: a write still on the wire counts as the lamp's state when new targets are compared (fixes a lost-return race
  found by the simulator), and a target that changed during a write is re-queued ("heal").
* Analog targets (pulse / breathing) are sent error-first with a dead-band; only visible differences use the budget.
* `capture()` / `restore()` are raw-exact (a lamp at raw brightness 5 comes back at 5).

## Controller (core/controller.py)

`idle | scene | effect | music | manual`. Entering an animation snapshots the room once (and writes `config/runtime_state.json` so a crash can be
undone from the UI). NORMAL cancels everything at CRITICAL priority. A manual tap stops the animation (no fighting).
After a (re)connect it re-reads every lamp; anything the user asked for while that was in progress is kept.

## Music (audio/)

1. `sources.py` – `SyntheticSource` (demo song, deterministic, ground truth for tests) or `SoundDeviceSource` (PortAudio thread → asyncio).
2. `features.py` – overlapped Hann FFT: RMS, peak, bass/mid/treble amplitude, per-band spectral flux (log-compressed, positive changes), centroid.
3. `analyzer.py` – adaptive level trackers (floor/ceiling in dB, seeded from calibration), adaptive-threshold peak picking with cool-downs,
   tempo from the autocorrelation of the onset envelope (80–180 BPM) with phase gating and a short flywheel, bass-dominance based DROP,
   BUILD / BREAKDOWN with hysteresis, silence gate, 2 Hz energy events.
4. `director.py` – owns the tasks: the pump (audio rate) pushes events into a **bounded queue that drops the oldest**; the mapper (event rate)
   skips stale events and calls the profile; calibration stores percentiles per device in `config/music_calibration.json`.
5. `profiles.py` – BEAT (ring chase, stride chosen so cue rate fits the budget), CLUB (mirrored pairs on the downbeat, half-bar chase,
   hat sparks if budget is spare, build ramp, drop ripple), AMBIENT (≈1 command/s drifting levels/colour). Each cue is priced in gateway
   commands and sent atomically against a token bucket (`music_budget`, default 3/s, so 1/s stays free for taps). Safety net: lights lit above the
   base glow that are not part of a live cue are put back.

## Simulator (hardware/simulator.py)

`SimGateway` = six lamps (DP20/22/23), ~200 ms jittered acknowledgement, one serialised socket, a command-rate ceiling (commands beyond it are
*not acknowledged* and counted), disconnect/reconnect. `SimLink` subclasses the real `AsyncGatewayLink`, so the supervisor, failure streak,
back-off and `on_online` re-sync are the production code. `tests/vclock.py` runs whole sessions in virtual time (30 minutes ≈ 12 s of CPU).

## Auth (api/auth.py)

Pure-ASGI middleware (covers the WebSocket): LAN allow-list → Host-header check → Origin check → PIN cookie / header. See README.
