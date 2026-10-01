# Real-hardware checklist (do this tomorrow — about 40 minutes)

Everything else (scheduler logic, effects vs. the command budget, controller modes, STOP/restore, music engine on synthetic audio, API, auth, UI
layout, 30-minute Party + Music sessions, reconnect logic, randomised stress) is **SIMULATOR VERIFIED** and is not repeated here.
Only the things a simulator cannot know are listed. Tick each box; write a note on anything odd.

## 0 · Get it running (5 min)
- [ ] Pull / copy the new code over `E:\something to party`. **Keep** `config\` (your `tuya_devices.json`, `tuya_session.json`, `tuya_fixtures.json`, `ble_map.json`) and `.venv\`.
- [ ] Close the Smart Life app on your phone (it also holds a gateway connection).
- [ ] Double-click **START_LIGHTS.bat**. It installs any missing package by itself and opens the QR page.
- [ ] Run **ALLOW_PHONE_ACCESS.bat** once as Administrator (Windows Firewall, private network).

## 1 · Connection & truth (2 min)
- [ ] Top-right pill turns **Online** within ~15 s (it says *Reconnecting* first; that is normal). Status → *Address* is the gateway IP and there is **no** "Simulator" pill.
- [ ] Home room view matches the real lamps (on/off and roughly the brightness). The dimmest lamp (L6, raw 5) is still shown as on, not off.

## 2 · Identity & orientation (3 min) — the UI's map vs. your room
- [ ] Tap each light in the room view, set it to 100 %, then back: the **physical** lamp that reacts is the one drawn
      (LEFT track bed→door = **L6, L2, L4**; RIGHT track bed→door = **L5, L1, L3**).
- [ ] Brightness slider up = brighter. Colour slider left = **warm**, right = **cool** (the DP is inverted internally; this proves the UI is not).

## 3 · API smoke on the real gateway (3 min)
- [ ] `.venv\Scripts\python.exe tools\api_smoke.py` → **RESULT: ALL PASSED – REAL HARDWARE**. Note the *NORMAL settled* time and the final gateway stats (`failed` should be 0).
- [ ] While it runs, look at the lamps: chase/mirror in the right order; the room returns to the CHILL look after STOP.

## 4 · Scenes & feel (5 min)
- [ ] NORMAL, CHILL, CINEMA, PARTY look right. Edit NORMAL (Scenes → Edit) to the level you actually want, *Save & apply*.
- [ ] While PARTY runs, drag Speed / Intensity / Brightness: changes follow within about a second, no light gets stuck on.
- [ ] Stop & restore returns every lamp to exactly its previous look (including the dim L6 and any lamp that was off).

## 5 · Music (10 min) — the main unknown
- [ ] **Demo track** (Music → input *Demo track*): run BEAT, CLUB, AMBIENT for ~1 minute each. Lights move in time with the (silent) analysis visuals; nothing stays lit; the budget holds (Status → *Sending now* ≤ 4/s).
- [ ] **Real input**: pick your mic (or *Stereo Mix*), play music at normal volume, **Calibrate** (should say *Calibrated*; "Too quiet / No signal" = wrong device or volume).
- [ ] BPM readout is close to the song's tempo; the beat/lights **feel** on time (note any lag in ms-ish: none / slight / annoying).
- [ ] Pause the music for 5 s → lights settle (*silence*); resume → they pick up again. Stop & restore works.
- [ ] Try sensitivity low/high and note a good default for your room: ________

## 6 · Phone (5 min)
- [ ] iPhone: scan the QR code (or enter the PIN) → UI loads, pairs; Share → *Add to Home Screen* → open the new icon, enter the PIN once (iOS gives the Home-Screen app its own storage) → full screen.
- [ ] Sliders feel responsive; lock the phone for a minute, unlock: it reconnects by itself (no stale screen).
- [ ] A phone that is **not** paired (or a PIN typed wrong 5×) is refused / locked out.

## 7 · Resilience on the real network (7 min)
- [ ] During a Party: unplug the gateway's power for ~20 s. UI shows *Gateway offline / Reconnecting*; after power returns it goes **Online** on its own (≤ ~1 min) and controls work again. Note how long: ____ s
- [ ] During a Party: **close the console window** → the room is restored (the Windows close-handler). Start again.
- [ ] During a Party: double-click **STOP_LIGHTS.bat** → party stops, room restored, window closes.
- [ ] Change a light from the Smart Life app while the controller is idle → the UI picks it up within ~20 s. (And: does the app still work while the controller is running? ☐ yes ☐ no)
- [ ] Start it twice (double-click START_LIGHTS.bat again): second one says "already running" and does not break the first.

## 8 · Soak (15 min, can run while you do something else)
- [ ] Run Pulse or Chase for 15 minutes, then Music (any input) for 15 minutes. Status → *Sent / failed*: failed stays ~0; no light stuck; afterwards the Smart Life app still shows the gateway online.

---
Result: ☐ all good   ☐ issues (list): ______________________________________________
Anything that fails: copy the last lines of `logs\lights.log` and the Status page numbers.
