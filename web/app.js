/* Room Lights — mobile web app. Plain JS, no build step. */
"use strict";

// ----------------------------------------------------------------------------- helpers
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const lerp = (a, b, t) => a + (b - a) * t;
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem("lights." + k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("lights." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

let S = null;          // latest server state
let M = null;          // meta (layout, effects, scenes)
let view = "home";
let wsUp = false;
let lastMsg = 0;

function throttle(fn, ms) {
  let last = 0, timer = null, pending;
  return (...a) => {
    pending = a;
    const now = Date.now();
    const run = () => { last = Date.now(); timer = null; fn(...pending); };
    if (now - last >= ms) { clearTimeout(timer); run(); }
    else if (!timer) timer = setTimeout(run, ms - (now - last));
  };
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.className = "toast"), err ? 3600 : 2000);
}

async function api(method, path, body) {
  try {
    const r = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
    if (r.status === 401) { location.href = "/login"; return null; }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { toast(j.detail || "Something went wrong", true); return null; }
    return j;
  } catch (e) {
    toast("Controller not reachable", true);
    return null;
  }
}
const post = (p, b) => api("POST", p, b === undefined ? {} : b);

// A slider the user is touching must not be overwritten by the echo of its own updates.
const touched = {};
function bindRange(el, onInput, ms = 140) {
  const send = throttle(onInput, ms);
  const mark = () => (touched[el.id] = Date.now() + 900);
  el.addEventListener("pointerdown", mark);
  el.addEventListener("touchstart", mark, { passive: true });
  el.addEventListener("input", () => { mark(); send(+el.value); paintRange(el); });
  el.addEventListener("change", () => { onInput(+el.value); touched[el.id] = Date.now() + 500; });
}
function setRange(el, v) {
  if (!el || (touched[el.id] && touched[el.id] > Date.now())) return;
  if (+el.value !== Math.round(v)) el.value = Math.round(v);
  paintRange(el);
}
function paintRange(el) {
  if (el.classList.contains("plain")) el.style.setProperty("--fill", ((+el.value - +el.min) / (+el.max - +el.min)) * 100 + "%");
}

// colour of a lamp: cct 0 = warm .. 1 = cool
function cctRGB(c) {
  const W = [255, 160, 80], N = [255, 240, 214], C = [150, 205, 255];
  const [a, b, t] = c < 0.55 ? [W, N, c / 0.55] : [N, C, (c - 0.55) / 0.45];
  return a.map((v, i) => Math.round(lerp(v, b[i], t)));
}
const rgb = (a, al = 1) => `rgba(${a[0]},${a[1]},${a[2]},${al})`;
const pct = (v) => Math.round(v) + "%";

// two-tap confirmation instead of a browser confirm() dialog
function confirmTap(btn, armedLabel, action) {
  const label = btn.textContent;
  btn.onclick = () => {
    if (btn.dataset.armed) { clearTimeout(btn._t); btn.dataset.armed = ""; btn.textContent = label; action(); return; }
    btn.dataset.armed = "1"; btn.textContent = armedLabel;
    btn._t = setTimeout(() => { btn.dataset.armed = ""; btn.textContent = label; }, 3500);
  };
}

// ----------------------------------------------------------------------------- navigation
function show(v) {
  view = v;
  $$(".view").forEach((el) => el.classList.toggle("active", el.id === "v-" + v));
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  window.scrollTo(0, 0);
  if (v === "music") { loadDevices(); startViz(); } else stopViz();
  if (v === "status") { loadStatusExtras(); }
  if (v === "scenes") renderScenes();
  render();
}
$("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) show(b.dataset.v); });

// ----------------------------------------------------------------------------- bottom sheet
function openSheet(html) {
  $("#sheet").innerHTML = '<div class="grab"></div>' + html;
  $("#sheet").classList.add("show");
  $("#scrim").classList.add("show");
}
function closeSheet() {
  $("#sheet").classList.remove("show");
  $("#scrim").classList.remove("show");
  sheetFid = null;
  sheetScene = null;
}
$("#scrim").addEventListener("click", closeSheet);

// ----------------------------------------------------------------------------- ROOM (home)
const SVGNS = "http://www.w3.org/2000/svg";
let sheetFid = null;
const fxEls = {};

function svg(tag, attrs = {}, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

function buildRoom() {
  const el = $("#room");
  el.innerHTML = "";
  const defs = svg("defs", {}, el);
  const f = svg("filter", { id: "blur", x: "-100%", y: "-100%", width: "300%", height: "300%" }, defs);
  svg("feGaussianBlur", { stdDeviation: "11" }, f);
  svg("rect", { class: "floor", x: 6, y: 6, width: 328, height: 278, rx: 30 }, el);
  // door (top) and bed (bottom)
  svg("path", { d: "M134 6 H206", stroke: "rgba(255,255,255,.4)", "stroke-width": 4, "stroke-linecap": "round" }, el);
  svg("text", { class: "tag", x: 170, y: 26 }, el).textContent = "DOOR";
  svg("rect", { class: "bed", x: 112, y: 214, width: 116, height: 64, rx: 14 }, el);
  svg("rect", { class: "bed", x: 122, y: 221, width: 42, height: 20, rx: 7 }, el);
  svg("rect", { class: "bed", x: 176, y: 221, width: 42, height: 20, rx: 7 }, el);
  svg("text", { class: "tag", x: 170, y: 264 }, el).textContent = "BED";
  const X = { LEFT: 62, RIGHT: 278 };
  const Y = [212, 134, 62];                       // bed end -> door end
  for (const [track, ids] of Object.entries(M.layout.tracks)) {
    const x = X[track] ?? 170;
    svg("line", { class: "rail", x1: x, y1: 44, x2: x, y2: 250 }, el);
    ids.forEach((fid, i) => {
      const info = M.layout.fixtures[fid];
      const g = svg("g", { class: "fx", "data-fid": fid }, el);
      const long = info.kind === "linear";
      const y = Y[i];
      if (long) {
        svg("rect", { class: "glow", x: x - 14, y: y - 40, width: 28, height: 80, rx: 14, filter: "url(#blur)" }, g);
        svg("rect", { class: "body", x: x - 13, y: y - 35, width: 26, height: 70, rx: 13 }, g);
        svg("rect", { class: "ring", x: x - 20, y: y - 42, width: 40, height: 84, rx: 20 }, g);
      } else {
        svg("circle", { class: "glow", cx: x, cy: y, r: 22, filter: "url(#blur)" }, g);
        svg("circle", { class: "body", cx: x, cy: y, r: 18 }, g);
        svg("circle", { class: "ring", cx: x, cy: y, r: 26 }, g);
      }
      const id = svg("text", { class: "id", x, y }, g); id.textContent = info.light;
      const pc = svg("text", { class: "pct", x, y: y + (long ? 49 : 34) }, g);
      svg("rect", { x: x - 40, y: y - 44, width: 80, height: 92, fill: "transparent" }, g); // big tap target
      g.addEventListener("click", () => openFixture(fid));
      fxEls[fid] = { g, glow: $(".glow", g), body: $(".body", g), id, pc };
    });
    const t = svg("text", { class: "tracklbl", x, y: 36 }, el); t.textContent = track;
  }
}

function paintRoom() {
  if (!S) return;
  for (const [fid, st] of Object.entries(S.fixtures)) {
    const e = fxEls[fid];
    if (!e) continue;
    const on = st.on && st.level > 0;
    const col = cctRGB(st.cct);
    const k = clamp(st.level / 100, 0, 1);
    if (on) {
      e.body.style.fill = rgb(col, 0.28 + 0.72 * Math.pow(k, 0.55));
      e.body.style.stroke = rgb(col, 0.9);
      e.glow.style.fill = rgb(col);
      e.glow.style.opacity = (0.15 + 0.85 * k).toFixed(2);
      e.id.style.fill = k > 0.45 ? "#1a1206" : "#f2f4f9";
      e.pc.textContent = pct(st.level);
    } else {
      e.body.style.fill = "#141a27";
      e.body.style.stroke = "rgba(255,255,255,0.18)";
      e.glow.style.opacity = 0;
      e.id.style.fill = "#6b7388";
      e.pc.textContent = "off";
    }
    e.g.classList.toggle("pending", !!st.pending);
    e.g.classList.toggle("sel", sheetFid === fid);
  }
}

// ---- fixture sheet
function openFixture(fid) {
  sheetFid = fid;
  const info = M.layout.fixtures[fid];
  openSheet(`
    <div class="row" style="gap:14px;margin-bottom:6px">
      <div class="swatch" id="fxSw"></div>
      <div style="flex:1;min-width:0"><h2>${esc(info.light)} · ${esc(info.label)}</h2>
        <p class="sub" style="margin:2px 0 0">${info.kind === "linear" ? "Long light" : "Spot"} · ${esc(info.track.toLowerCase())} track</p></div>
      <div class="toggle" id="fxOn" role="switch" aria-label="On / off"></div>
    </div>
    <div class="ctl"><div class="row"><span class="lbl">Brightness</span><span class="val" id="fxBriV"></span></div>
      <input type="range" class="bri" id="fxBri" min="0" max="100" aria-label="Brightness"></div>
    <div class="ctl"><div class="row"><span class="lbl">Colour temperature</span><span class="val" id="fxCctV"></span></div>
      <input type="range" class="cct" id="fxCct" min="0" max="100" aria-label="Colour temperature"></div>
    <p class="sub" id="fxNote" style="margin:12px 2px 14px"></p>
    <button class="btn" id="fxDone">Done</button>`);
  const send = (b) => post("/api/fixture/" + fid, b);
  bindRange($("#fxBri"), (v) => send({ level: v }), 120);
  bindRange($("#fxCct"), (v) => send({ cct: v / 100 }), 120);
  $("#fxOn").onclick = () => { const on = !$("#fxOn").classList.contains("on"); send({ on }); };
  $("#fxDone").onclick = closeSheet;
  paintFixtureSheet();
  paintRoom();
}
function paintFixtureSheet() {
  if (!sheetFid || !S || !$("#fxBri")) return;
  const st = S.fixtures[sheetFid];
  const on = st.on && st.level > 0;
  $("#fxOn").classList.toggle("on", on);
  setRange($("#fxBri"), st.level);
  setRange($("#fxCct"), st.cct * 100);
  $("#fxBriV").textContent = on ? pct(st.level) : "Off";
  $("#fxCctV").textContent = st.cct < 0.33 ? "Warm" : st.cct > 0.66 ? "Cool" : "Neutral";
  $("#fxSw").style.setProperty("--sw", on ? rgb(cctRGB(st.cct), 0.25 + 0.75 * Math.pow(st.level / 100, 0.55)) : "#141a27");
  $("#fxNote").textContent = S.mode === "effect" || S.mode === "music" ? "Changing this light stops the running " + (S.mode === "music" ? "music" : "party") + " mode." : "";
}

// ----------------------------------------------------------------------------- HOME: scenes, master, banners
const SCENE_META = {
  NORMAL: ["normal", "Everyday light", true],
  CHILL: ["chill", "Low and warm"],
  CINEMA: ["cinema", "Almost dark"],
  PARTY: ["party", "Moving light"],
  MUSIC: ["music", "Follows the sound"],
  "ALL ON": ["allon", "Full brightness"],
  "ALL OFF": ["alloff", "Lights out"],
};
function buildScenes() {
  const main = $("#sceneMain");
  main.innerHTML = "";
  ["NORMAL", "CHILL", "CINEMA", "PARTY", "MUSIC"].forEach((n) => {
    if (!M.scenes[n]) return;
    const [cls, desc, wide] = SCENE_META[n];
    const b = document.createElement("button");
    b.className = "scene " + cls + (wide ? " wide" : "");
    b.dataset.scene = n;
    b.innerHTML = `<div><div class="nm">${esc(n)}</div><div class="ds">${esc(desc)}</div></div><span class="st" hidden></span>`;
    b.onclick = () => applyScene(n);
    main.appendChild(b);
  });
  const more = $("#sceneMore");
  more.innerHTML = "";
  M.scene_order.filter((n) => !["NORMAL", "CHILL", "CINEMA", "PARTY", "MUSIC"].includes(n)).forEach((n) => {
    const b = document.createElement("button");
    b.className = "chip";
    b.dataset.scene = n;
    b.textContent = M.scenes[n].label || n;
    b.onclick = () => applyScene(n);
    more.appendChild(b);
  });
}
async function applyScene(n) {
  if (navigator.vibrate) navigator.vibrate(8);
  const r = await post("/api/scene/" + encodeURIComponent(n));
  if (r) toast((M.scenes[n]?.label || n) + (n === "NORMAL" ? "" : " on"));
}

function paintHome() {
  if (!S) return;
  $$("[data-scene]").forEach((b) => {
    const on = S.scene === b.dataset.scene;
    b.classList.toggle("on", on);
    const st = $(".st", b);
    if (st) { st.hidden = !on; st.textContent = on && (S.mode === "effect" || S.mode === "music") ? "Running" : on ? "Active" : ""; }
  });
  setRange($("#masterBri"), S.master.brightness);
  setRange($("#masterCct"), S.master.cct * 100);
  $("#masterBriV").textContent = pct(S.master.brightness);
  $("#masterCctV").textContent = S.master.cct < 0.33 ? "Warm" : S.master.cct > 0.66 ? "Cool" : "Neutral";
  const run = $("#runBar");
  if (S.mode === "effect" || S.mode === "music") {
    const label = S.mode === "music" ? "Music · " + (S.music.profile || "").toUpperCase() : "Party · " + (M.effects.find((e) => e.name === S.name)?.label || S.name);
    run.innerHTML = `<div class="banner" style="margin-top:12px"><span><b>${esc(label)}</b> is running</span><button id="runStop">Stop &amp; restore</button></div>`;
    $("#runStop").onclick = () => post("/api/stop", { restore: true });
  } else run.innerHTML = "";
  // banners
  const b = [];
  if (S.stale_snapshot) b.push(`<div class="banner"><span>The last party ended unexpectedly. Put the lights back as they were?</span><button id="bRestore">Restore</button></div>`);
  if (S.gateway.state !== "online") b.push(`<div class="banner bad"><span><b>${S.gateway.state === "connecting" ? "Reconnecting to the gateway…" : "Gateway offline."}</b> Changes are queued and applied when it is back.</span></div>`);
  if (S.error) b.push(`<div class="banner bad"><span>${esc(S.error)}</span></div>`);
  $("#banners").innerHTML = b.join("");
  const br = $("#bRestore");
  if (br) br.onclick = async () => { await post("/api/restore-last"); toast("Restoring the previous look"); };
}

bindRange($("#masterBri"), (v) => post("/api/master", { brightness: v }), 140);
bindRange($("#masterCct"), (v) => post("/api/master", { cct: v / 100 }), 140);

// ----------------------------------------------------------------------------- PARTY
const PARTY_DEF = { effect: "chase", speed: 55, intensity: 85, direction: "forward", pattern: "left_right", max_brightness: 90, min_brightness: 8, cct_warm: 0.05, cct_cool: 0.65 };
let P = { ...PARTY_DEF, ...store.get("party", {}) };
const savePartyLocal = () => store.set("party", P);

function partyParams() {
  return { speed: P.speed, intensity: P.intensity, direction: P.direction, pattern: P.pattern, max_brightness: P.max_brightness,
    min_brightness: P.min_brightness, cct_warm: P.cct_warm, cct_cool: P.cct_cool };
}
const liveParty = throttle((k, v) => { if (S && S.mode === "effect") post("/api/params", { [k]: v }); }, 200);

function buildParty() {
  const g = $("#fxGrid");
  g.innerHTML = "";
  M.effects.forEach((e) => {
    const b = document.createElement("button");
    b.className = "fxcard";
    b.dataset.fx = e.name;
    b.innerHTML = `<div class="nm">${esc(e.label)}</div><div class="ds">${esc(e.description)}</div>`;
    b.onclick = () => selectEffect(e.name);
    g.appendChild(b);
  });
  segment($("#pDir"), [["forward", "Forward"], ["reverse", "Reverse"], ["random", "Random"]], () => P.direction, (v) => { P.direction = v; savePartyLocal(); liveParty("direction", v); paintParty(); });
  segment($("#pPattern"), [["left_right", "Left ↔ Right"], ["odd_even", "Odd ↔ Even"]], () => P.pattern, (v) => { P.pattern = v; savePartyLocal(); liveParty("pattern", v); paintParty(); });
  const num = (id, key, scale = 1) => bindRange($(id), (v) => { P[key] = v / scale; savePartyLocal(); liveParty(key, P[key]); paintParty(true); });
  num("#pSpeed", "speed"); num("#pInt", "intensity"); num("#pMax", "max_brightness"); num("#pMin", "min_brightness");
  dual("#pCctA", "#pCctB", "#pCctSel", (a, b) => { P.cct_warm = a / 100; P.cct_cool = b / 100; savePartyLocal(); liveParty("cct_warm", P.cct_warm); liveParty("cct_cool", P.cct_cool); paintParty(true); });
  $("#pSaveScene").onclick = async () => {
    const r = await api("PUT", "/api/scenes/PARTY", { kind: "effect", label: "Party", effect: P.effect, params: partyParams() });
    if (r) { M.scenes.PARTY = { kind: "effect", label: "Party", effect: P.effect, params: partyParams() }; toast("PARTY scene saved"); }
  };
}

function segment(el, items, get, set) {
  el.innerHTML = "";
  items.forEach(([v, t]) => {
    const b = document.createElement("button");
    b.textContent = t; b.dataset.v = v;
    b.onclick = () => { set(v); $$("button", el).forEach((x) => x.classList.toggle("on", x.dataset.v === get())); };
    el.appendChild(b);
  });
  $$("button", el).forEach((x) => x.classList.toggle("on", x.dataset.v === get()));
}

// two thumbs on one gradient: the lower never passes the upper
function dual(aSel, bSel, selSel, onChange) {
  const A = $(aSel), B = $(bSel), S_ = $(selSel);
  const upd = (fire) => {
    let a = +A.value, b = +B.value;
    if (a > b) { if (document.activeElement === A || A.dataset.last === "1") { a = b; A.value = a; } else { b = a; B.value = b; } }
    S_.style.left = a + "%"; S_.style.width = Math.max(1, b - a) + "%";
    S_.style.backgroundPosition = `${b - a >= 100 ? 0 : (a / (100 - (b - a))) * 100}% 0`;
    S_.style.backgroundSize = (10000 / Math.max(1, b - a)) + "% 100%";
    if (fire) onChange(a, b);
  };
  const t = throttle(() => upd(true), 160);
  [A, B].forEach((el) => {
    el.addEventListener("pointerdown", () => { touched[el.id] = Date.now() + 900; A.dataset.last = el === A ? "1" : "0"; });
    el.addEventListener("touchstart", () => { touched[el.id] = Date.now() + 900; A.dataset.last = el === A ? "1" : "0"; }, { passive: true });
    el.addEventListener("input", () => { touched[el.id] = Date.now() + 900; upd(false); t(); });
    el.addEventListener("change", () => upd(true));
  });
  A._upd = upd;
}
function setDual(aSel, bSel, a, b) {
  const A = $(aSel), B = $(bSel);
  if ((touched[A.id] || 0) > Date.now() || (touched[B.id] || 0) > Date.now()) return;
  A.value = Math.round(a * 100); B.value = Math.round(b * 100);
  A._upd(false);
}

function selectEffect(name) {
  P.effect = name;
  const def = M.effects.find((e) => e.name === name);
  if (def && !(S && S.mode === "effect")) { // nudge to the effect's own sensible defaults when idle
    P.speed = def.defaults.speed ?? P.speed;
    P.intensity = def.defaults.intensity ?? P.intensity;
    P.min_brightness = def.defaults.min_brightness ?? P.min_brightness;
    P.max_brightness = def.defaults.max_brightness ?? P.max_brightness;
  }
  savePartyLocal();
  if (S && S.mode === "effect") post("/api/effect/start", { name, params: partyParams() });
  paintParty();
}

function paintParty(skipSync) {
  if (!M) return;
  const running = S && S.mode === "effect";
  if (running && !skipSync) {            // mirror the live server values
    const p = S.params;
    P.effect = S.name;
    for (const k of ["speed", "intensity", "direction", "pattern", "max_brightness", "min_brightness", "cct_warm", "cct_cool"]) if (p[k] !== undefined && !(touched["p-" + k] > Date.now())) P[k] = p[k];
  }
  $$("#fxGrid .fxcard").forEach((b) => { b.classList.toggle("on", b.dataset.fx === P.effect); b.classList.toggle("live", running && S.name === b.dataset.fx); });
  setRange($("#pSpeed"), P.speed); setRange($("#pInt"), P.intensity); setRange($("#pMax"), P.max_brightness); setRange($("#pMin"), P.min_brightness);
  setDual("#pCctA", "#pCctB", P.cct_warm, P.cct_cool);
  $("#pSpeedV").textContent = pct(P.speed); $("#pIntV").textContent = pct(P.intensity);
  $("#pMaxV").textContent = pct(P.max_brightness); $("#pMinV").textContent = pct(P.min_brightness);
  $("#pCctV").textContent = `${Math.round(P.cct_warm * 100)}–${Math.round(P.cct_cool * 100)}`;
  $$("#pDir button").forEach((x) => x.classList.toggle("on", x.dataset.v === P.direction));
  $$("#pPattern button").forEach((x) => x.classList.toggle("on", x.dataset.v === P.pattern));
  $("#pPatternWrap").hidden = P.effect !== "alternate";
  const def = M.effects.find((e) => e.name === P.effect);
  $("#pHint").textContent = def ? `${def.cmds_per_step} lamp command${def.cmds_per_step > 1 ? "s" : ""} per step` : "";
  const a = $("#pActions");
  const key = running ? "run" : "idle";
  if (a.dataset.k !== key) {
    a.dataset.k = key;
    a.innerHTML = running
      ? '<button class="btn primary" id="pStop">Stop &amp; restore</button><button class="btn" id="pStopKeep">Stop here</button>'
      : '<button class="btn primary" id="pStart">Start party</button>';
    if (running) {
      $("#pStop").onclick = () => post("/api/stop", { restore: true });
      $("#pStopKeep").onclick = () => post("/api/stop", { restore: false });
    } else $("#pStart").onclick = async () => { if (navigator.vibrate) navigator.vibrate(10); const r = await post("/api/effect/start", { name: P.effect, params: partyParams() }); if (r) toast("Party started"); };
  }
}

// ----------------------------------------------------------------------------- MUSIC
const MU_DEF = { device: "", profile: "beat", sensitivity: 60, max_brightness: 90, min_brightness: 8, cct_warm: 0.05, cct_cool: 0.65 };
let MU = { ...MU_DEF, ...store.get("music", {}) };
const saveMusicLocal = () => store.set("music", MU);
const PROFILE_DESC = {
  beat: "One light follows the pulse around the room. Clean, steady, easy on the gateway.",
  club: "Pairs on the downbeat, ripples on the drop, a build that lights the room up piece by piece.",
  ambient: "Slow colour and brightness drift that follows the energy of the track. Calm.",
};
let devices = [];
const liveMusic = throttle((k, v) => { if (S && S.mode === "music") post("/api/params", { [k]: v }); }, 200);

function buildMusic() {
  segment($("#mProfile"), [["beat", "BEAT"], ["club", "CLUB"], ["ambient", "AMBIENT"]], () => MU.profile, (v) => { MU.profile = v; saveMusicLocal(); liveMusic("profile", v); paintMusic(); });
  bindRange($("#mSens"), (v) => { MU.sensitivity = v; saveMusicLocal(); liveMusic("sensitivity", v); paintMusic(true); });
  bindRange($("#mMax"), (v) => { MU.max_brightness = v; saveMusicLocal(); liveMusic("max_brightness", v); paintMusic(true); });
  dual("#mCctA", "#mCctB", "#mCctSel", (a, b) => { MU.cct_warm = a / 100; MU.cct_cool = b / 100; saveMusicLocal(); liveMusic("cct_warm", MU.cct_warm); liveMusic("cct_cool", MU.cct_cool); paintMusic(true); });
  $("#mDevice").onchange = (e) => { MU.device = e.target.value; saveMusicLocal(); liveMusic("device", MU.device); paintMusic(); };
  $("#mCalib").onclick = async () => {
    const r = await post("/api/music/calibrate", { device: MU.device || undefined, seconds: 10 });
    if (r) toast("Listening for 10 seconds…");
  };
}
async function loadDevices() {
  const d = await api("GET", "/api/audio/devices");
  if (!d) return;
  devices = d;
  const sel = $("#mDevice");
  sel.innerHTML = d.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}${x.default ? "  ★" : ""}</option>`).join("");
  if (!MU.device || !d.find((x) => x.id === MU.device)) MU.device = (d.find((x) => x.default) || d.find((x) => x.kind === "input") || d[0])?.id || "demo";
  sel.value = MU.device;
}

function paintMusic(skipSync) {
  if (!S || !M) return;
  const m = S.music;
  const running = S.mode === "music";
  if (running && !skipSync) {
    const p = S.params;
    for (const k of ["profile", "sensitivity", "max_brightness", "cct_warm", "cct_cool", "device"]) if (p[k] !== undefined && !(touched["m-" + k] > Date.now())) MU[k] = p[k];
  }
  $$("#mProfile button").forEach((x) => x.classList.toggle("on", x.dataset.v === MU.profile));
  $("#mProfileDesc").textContent = PROFILE_DESC[MU.profile] || "";
  setRange($("#mSens"), MU.sensitivity); setRange($("#mMax"), MU.max_brightness);
  setDual("#mCctA", "#mCctB", MU.cct_warm, MU.cct_cool);
  $("#mSensV").textContent = pct(MU.sensitivity); $("#mMaxV").textContent = pct(MU.max_brightness);
  $("#mCctV").textContent = `${Math.round(MU.cct_warm * 100)}–${Math.round(MU.cct_cool * 100)}`;
  if ($("#mDevice").value !== MU.device && devices.find((x) => x.id === MU.device)) $("#mDevice").value = MU.device;
  $("#mBpm").textContent = running && m.bpm ? Math.round(m.bpm) : "–";
  const sec = $("#mSection");
  const secName = !running ? "idle" : m.silent ? "silence" : m.section || "steady";
  sec.textContent = secName; sec.className = "tag " + secName;
  $("#mEvents").innerHTML = (m.recent || []).filter((e) => running).slice(-6).map((e) => `<span class="ev ${esc(e.kind)}">${esc(e.kind.replace("onset_", ""))}</span>`).join("");
  // calibration
  const cb = $("#mCalibBar");
  cb.hidden = !m.calibrating;
  $("i", cb).style.width = Math.round((m.calib_progress || 0) * 100) + "%";
  $("#mCalib").disabled = m.calibrating;
  $("#mCalib").textContent = m.calibrating ? "Listening…" : "Calibrate";
  const info = $("#mCalibInfo");
  if (m.calibrating) info.textContent = "Listening — keep the music playing at your normal volume.";
  else if (m.calibration) {
    const q = m.calibration.quality;
    const ok = q === "ok";
    const when = m.calibration.ts ? new Date(m.calibration.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
    info.innerHTML = `<b class="${ok ? "q-ok" : "q-bad"}">${ok ? "Calibrated" : q === "too_quiet" ? "Too quiet" : q === "no_signal" ? "No signal" : "Too loud"}</b> ${when ? "at " + when : ""}<br>${ok ? "Levels and thresholds adapt from here." : "Raise the volume or pick another input, then calibrate again."}`;
  } else info.textContent = "Play your music at the volume you normally use, then calibrate for 10 seconds.";
  if (m.error) info.innerHTML += `<br><b class="q-bad">${esc(m.error)}</b>`;
  const a = $("#mActions");
  const key = running ? "run" : "idle";
  if (a.dataset.k !== key) {
    a.dataset.k = key;
    a.innerHTML = running ? '<button class="btn primary" id="mStop">Stop &amp; restore</button>' : '<button class="btn primary" id="mStart">Start music</button>';
    if (running) $("#mStop").onclick = () => post("/api/music/stop", { restore: true });
    else $("#mStart").onclick = async () => {
      if (navigator.vibrate) navigator.vibrate(10);
      const r = await post("/api/music/start", { params: { profile: MU.profile, sensitivity: MU.sensitivity, device: MU.device, max_brightness: MU.max_brightness, cct_warm: MU.cct_warm, cct_cool: MU.cct_cool } });
      if (r) toast("Music started");
    };
  }
}

// live visualisation: 60 fps smoothing of ~7 Hz state updates
const viz = { raf: 0, v: { bass: 0, mid: 0, treble: 0, energy: 0 }, pulse: 0, lastBeats: 0, hue: 0.4 };
function startViz() { if (!viz.raf) viz.raf = requestAnimationFrame(drawViz); }
function stopViz() { cancelAnimationFrame(viz.raf); viz.raf = 0; }
function drawViz() {
  viz.raf = requestAnimationFrame(drawViz);
  const cv = $("#viz");
  if (!cv || !S) return;
  const dpr = Math.min(3, window.devicePixelRatio || 1);
  const w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const c = cv.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  const m = S.music, running = S.mode === "music";
  const tgt = running ? m.levels : { bass: 0, mid: 0, treble: 0, energy: 0 };
  for (const k of ["bass", "mid", "treble", "energy"]) viz.v[k] += (tgt[k] - viz.v[k]) * (tgt[k] > viz.v[k] ? 0.5 : 0.12);
  if (running && m.beats !== viz.lastBeats) { viz.pulse = 1; viz.lastBeats = m.beats; }
  viz.pulse *= 0.9;
  viz.hue += ((running ? clamp(viz.v.treble * 1.4, 0, 1) : 0.3) - viz.hue) * 0.05;
  const col = cctRGB(viz.hue);
  // bars
  const names = [["bass", "BASS"], ["mid", "MID"], ["treble", "HIGH"], ["energy", "ENERGY"]];
  const bw = Math.min(44, (w * 0.6) / 4 - 10), gap = 12, x0 = 12, base = h - 22, maxh = h - 42;
  names.forEach(([k, label], i) => {
    const x = x0 + i * (bw + gap), bh = Math.max(4, viz.v[k] * maxh);
    c.fillStyle = "rgba(255,255,255,0.06)"; roundRect(c, x, base - maxh, bw, maxh, 10); c.fill();
    const g = c.createLinearGradient(0, base, 0, base - bh);
    g.addColorStop(0, "rgba(255,138,61,0.95)"); g.addColorStop(1, rgb(col, 1));
    c.fillStyle = g; roundRect(c, x, base - bh, bw, bh, 10); c.fill();
    c.fillStyle = "#6b7388"; c.font = "700 9.5px -apple-system, sans-serif"; c.textAlign = "center";
    c.fillText(label, x + bw / 2, h - 7);
  });
  // beat orb
  const cx = w - (w - (x0 + 4 * (bw + gap))) / 2, cy = h / 2 - 6, r0 = Math.min(46, (w - (x0 + 4 * (bw + gap))) / 2 - 8);
  const r = r0 * (0.55 + 0.35 * viz.v.energy + 0.25 * viz.pulse);
  c.globalAlpha = running ? 1 : 0.35;
  const rg = c.createRadialGradient(cx, cy, 0, cx, cy, r * 2.2);
  rg.addColorStop(0, rgb(col, 0.9)); rg.addColorStop(0.35, rgb(col, 0.35)); rg.addColorStop(1, rgb(col, 0));
  c.fillStyle = rg; c.beginPath(); c.arc(cx, cy, r * 2.2, 0, 7); c.fill();
  c.fillStyle = rgb(col, 0.95); c.beginPath(); c.arc(cx, cy, r, 0, 7); c.fill();
  c.globalAlpha = 1;
  if (!running) { c.fillStyle = "rgba(255,255,255,0.35)"; c.font = "600 12px -apple-system, sans-serif"; c.textAlign = "center"; c.fillText("Not listening", cx, cy + r0 + 22); }
}
function roundRect(c, x, y, w, h, r) { c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }

// ----------------------------------------------------------------------------- SCENES + editor
let sheetScene = null;
function sceneSwatch(sc) {
  if (sc.kind === "effect") return "linear-gradient(135deg,#ff4fa3,#7a6bff)";
  if (sc.kind === "music") return "linear-gradient(135deg,#3fd4ff,#7a6bff)";
  const a = sc.all || {};
  const lv = a.level ?? 40, cc = a.cct ?? 0.4;
  if (lv <= 0 && !Object.values(sc.fixtures || {}).some((f) => (f.level || 0) > 0)) return "#141a27";
  return `linear-gradient(135deg, ${rgb(cctRGB(cc), 0.3 + 0.7 * Math.pow(lv / 100, 0.6))}, ${rgb(cctRGB(clamp(cc + 0.15, 0, 1)), 0.2 + 0.5 * Math.pow(lv / 100, 0.6))})`;
}
function sceneDesc(sc) {
  if (sc.kind === "effect") { const e = M.effects.find((x) => x.name === sc.effect); return "Party · " + (e ? e.label : sc.effect); }
  if (sc.kind === "music") return "Music · reacts to sound";
  const n = Object.keys(sc.fixtures || {}).length;
  return `Static · ${sc.all && sc.all.level !== undefined ? pct(sc.all.level) : "custom"}${n ? ` · ${n} custom light${n > 1 ? "s" : ""}` : ""}`;
}
function renderScenes() {
  if (!M) return;
  $("#sceneList").innerHTML = M.scene_order.map((n) => {
    const sc = M.scenes[n];
    return `<div class="item"><div class="sw" style="--g:${sceneSwatch(sc)}"></div><div class="tx"><b>${esc(sc.label || n)}</b><span>${esc(sceneDesc(sc))}</span></div>
      <button class="btn small" data-apply="${esc(n)}">Apply</button><button class="btn small" data-edit="${esc(n)}">Edit</button></div>`;
  }).join("");
  $$("[data-apply]").forEach((b) => (b.onclick = () => applyScene(b.dataset.apply)));
  $$("[data-edit]").forEach((b) => (b.onclick = () => openSceneEditor(b.dataset.edit)));
}
$("#sceneNew").onclick = () => {
  openSheet(`<h2>New scene</h2><p class="sub">Captures the room exactly as it looks right now. You can fine-tune it afterwards.</p>
    <input class="txt" id="newName" maxlength="30" placeholder="Name, e.g. Reading" autocomplete="off" autocapitalize="words">
    <div class="btns" style="margin-top:16px"><button class="btn primary" id="newGo">Create</button><button class="btn" id="newCancel">Cancel</button></div>`);
  $("#newCancel").onclick = closeSheet;
  $("#newGo").onclick = async () => {
    const name = $("#newName").value.trim();
    if (!name) { toast("Give it a name first", true); return; }
    const r = await post("/api/scenes/" + encodeURIComponent(name) + "/save-current");
    if (r) { await reloadMeta(); renderScenes(); openSceneEditor(name); }
  };
};
async function reloadMeta() { const m = await api("GET", "/api/meta"); if (m) { M = m; buildScenes(); } }

function openSceneEditor(name) {
  const sc = JSON.parse(JSON.stringify(M.scenes[name]));
  sheetScene = { name, sc, dirty: new Set() };
  const builtin = ["NORMAL", "CHILL", "CINEMA", "PARTY", "MUSIC", "ALL ON", "ALL OFF"].includes(name);
  let body = "";
  if (sc.kind === "static") {
    const rows = Object.entries(M.layout.fixtures).map(([fid, info]) => `
      <div class="fxrow"><div class="nm">${esc(info.light)}<small>${esc(info.label.replace(/ (spot|light)$/i, ""))}</small></div>
        <div><input type="range" class="bri" data-f="${fid}" data-k="level" min="0" max="100" aria-label="${esc(info.label)} brightness">
        <input type="range" class="cct" data-f="${fid}" data-k="cct" min="0" max="100" aria-label="${esc(info.label)} colour"></div></div>`).join("");
    body = `<div class="card" style="margin-top:10px"><div class="lbl" style="margin-bottom:4px">Every light</div>
      <div class="row"><span class="sub" style="margin:0">Brightness</span><span class="val" id="seAllBriV"></span></div>
      <input type="range" class="bri" id="seAllBri" min="0" max="100" aria-label="All brightness">
      <div class="row"><span class="sub" style="margin:0">Colour</span><span class="val" id="seAllCctV"></span></div>
      <input type="range" class="cct" id="seAllCct" min="0" max="100" aria-label="All colour"></div>
      <div class="sec">Individual lights</div><div class="card">${rows}</div>
      <button class="btn small" id="seCapture" style="margin-top:12px">Capture the room as it is now</button>`;
  } else if (sc.kind === "effect") {
    body = `<div class="card" style="margin-top:10px"><div class="lbl" style="margin-bottom:8px">Pattern</div>
      <select class="sel" id="seEffect">${M.effects.map((e) => `<option value="${e.name}">${esc(e.label)}</option>`).join("")}</select>
      <div class="ctl"><div class="row"><span class="lbl">Speed</span><span class="val" id="seSpV"></span></div><input type="range" class="plain" id="seSp" min="0" max="100"></div>
      <div class="ctl"><div class="row"><span class="lbl">Intensity</span><span class="val" id="seInV"></span></div><input type="range" class="plain" id="seIn" min="0" max="100"></div>
      <div class="ctl"><div class="row"><span class="lbl">Peak brightness</span><span class="val" id="seMxV"></span></div><input type="range" class="bri" id="seMx" min="5" max="100"></div></div>`;
  } else {
    body = `<div class="card" style="margin-top:10px"><div class="seg accent" id="seProfile"></div>
      <div class="ctl"><div class="row"><span class="lbl">Sensitivity</span><span class="val" id="seSeV"></span></div><input type="range" class="plain" id="seSe" min="0" max="100"></div></div>`;
  }
  openSheet(`<h2>${esc(sc.label || name)}</h2><p class="sub">${builtin ? "Built-in scene — edits are kept, Reset restores the original." : "Custom scene"}</p>${body}
    <div class="btns" style="margin-top:16px"><button class="btn primary" id="seSave">Save &amp; apply</button><button class="btn" id="seClose">Cancel</button></div>
    <div class="btns" style="margin-top:10px">${builtin ? '<button class="btn small danger" id="seReset">Reset to default</button>' : '<button class="btn small danger" id="seDelete">Delete</button>'}</div>`);
  wireSceneEditor();
  $("#seClose").onclick = closeSheet;
  $("#seSave").onclick = async () => { const r = await saveScene(true); if (r) closeSheet(); };
  if ($("#seReset")) $("#seReset").onclick = async () => { await post("/api/scenes/" + encodeURIComponent(name) + "/reset"); await reloadMeta(); renderScenes(); closeSheet(); toast("Reset"); };
  if ($("#seDelete")) confirmTap($("#seDelete"), "Tap again to delete", async () => { const r = await api("DELETE", "/api/scenes/" + encodeURIComponent(name)); if (r) { await reloadMeta(); renderScenes(); closeSheet(); } });
}
function wireSceneEditor() {
  const { sc } = sheetScene;
  if (sc.kind === "static") {
    const all = sc.all || (sc.all = {});
    const allL = $("#seAllBri"), allC = $("#seAllCct");
    allL.value = all.level ?? 40; allC.value = Math.round((all.cct ?? 0.4) * 100);
    const lab = () => { $("#seAllBriV").textContent = pct(+allL.value); $("#seAllCctV").textContent = +allC.value < 33 ? "Warm" : +allC.value > 66 ? "Cool" : "Neutral"; };
    lab();
    allL.oninput = () => { all.level = +allL.value; sc.fixtures = {}; lab(); syncRows(); };
    allC.oninput = () => { all.cct = +allC.value / 100; sc.fixtures = sc.fixtures || {}; lab(); syncRows(); };
    const syncRows = () => $$("#sheet [data-f]").forEach((el) => {
      const f = el.dataset.f, k = el.dataset.k, fx = (sc.fixtures || {})[f] || {};
      el.value = k === "level" ? (fx.level ?? all.level ?? 0) : Math.round((fx.cct ?? all.cct ?? 0.4) * 100);
    });
    syncRows();
    $$("#sheet [data-f]").forEach((el) => (el.oninput = () => {
      sc.fixtures = sc.fixtures || {};
      const f = el.dataset.f, cur = sc.fixtures[f] || { level: all.level ?? 0, cct: all.cct ?? 0.4 };
      cur[el.dataset.k] = el.dataset.k === "level" ? +el.value : +el.value / 100;
      sc.fixtures[f] = cur;
    }));
    $("#seCapture").onclick = async () => {
      const r = await post("/api/scenes/" + encodeURIComponent(sheetScene.name) + "/save-current");
      if (r) { await reloadMeta(); sheetScene.sc = JSON.parse(JSON.stringify(M.scenes[sheetScene.name])); openSceneEditor(sheetScene.name); toast("Captured the current room"); }
    };
  } else if (sc.kind === "effect") {
    const p = sc.params || (sc.params = {});
    $("#seEffect").value = sc.effect;
    $("#seEffect").onchange = (e) => (sc.effect = e.target.value);
    const bind = (id, vid, key, def) => { const el = $(id); el.value = p[key] ?? def; paintRange(el); const lab = () => ($(vid).textContent = pct(+el.value)); lab(); el.oninput = () => { p[key] = +el.value; lab(); paintRange(el); }; };
    bind("#seSp", "#seSpV", "speed", 55); bind("#seIn", "#seInV", "intensity", 85); bind("#seMx", "#seMxV", "max_brightness", 90);
  } else {
    const p = sc.params || (sc.params = {});
    segment($("#seProfile"), [["beat", "BEAT"], ["club", "CLUB"], ["ambient", "AMBIENT"]], () => p.profile || "beat", (v) => (p.profile = v));
    const el = $("#seSe"); el.value = p.sensitivity ?? 60; paintRange(el); $("#seSeV").textContent = pct(+el.value);
    el.oninput = () => { p.sensitivity = +el.value; $("#seSeV").textContent = pct(+el.value); paintRange(el); };
  }
}
async function saveScene(apply) {
  const { name, sc } = sheetScene;
  const r = await api("PUT", "/api/scenes/" + encodeURIComponent(name), sc);
  if (!r) return null;
  await reloadMeta(); renderScenes();
  if (apply) await applyScene(name);
  return r;
}

// ----------------------------------------------------------------------------- STATUS
function paintStatus() {
  if (!S) return;
  const g = S.gateway, st = g.stats;
  $("#stSub").textContent = S.simulated ? "Running against the built-in simulator." : "Local control of the gateway — no cloud involved.";
  $("#stKv").innerHTML = [
    ["Gateway", g.state === "online" ? "Online" : g.state === "connecting" ? "Connecting…" : "Offline"],
    ["Address", S.simulated ? "simulator" : g.ip],
    ["Reconnects", g.reconnects],
    ["Mode", S.mode + (S.name ? " · " + S.name : "")],
    ["Command budget", g.rate_limit + "/s"],
    ["Sending now", st.rate_5s + "/s"],
    ["Queued", st.queue],
    ["Sent / failed", `${st.sent} / ${st.failed}`],
    ["Merged / expired", `${st.coalesced} / ${st.dropped_stale}`],
    ["Gateway reply (median)", st.ack_ms_median != null ? st.ack_ms_median + " ms" : "–"],
    ["Uptime", fmtUp(S.uptime)],
  ].map(([a, b]) => `<span>${esc(a)}</span><span>${esc(b)}</span>`).join("");
  const sp = $("#simPanel");
  if (S.simulated && S.sim) {
    const cells = Object.entries(S.sim.fixtures).map(([fid, f]) => {
      const col = f.on && f.level > 0 ? rgb(cctRGB(f.cct), 0.25 + 0.75 * Math.pow(f.level / 100, 0.55)) : "#141a27";
      return `<div class="simcell" style="background:${col};color:${f.on && f.level > 40 ? "#1a1206" : "#f2f4f9"}">${esc(M.layout.fixtures[fid].light)}<br>${f.on ? Math.round(f.level) : "off"}</div>`;
    }).join("");
    sp.innerHTML = `<div class="sec">Simulator</div><div class="card">
      <p class="sub" style="margin:0">What the simulated lamps are really doing (after gateway delay). SIMULATOR ONLY — not hardware proof.</p>
      <div class="simgrid">${cells}</div>
      <div class="kv"><span>Accepted</span><span>${S.sim.accepted}</span><span>Rejected (rate limit)</span><span>${S.sim.rejected}</span><span>Busiest second</span><span>${S.sim.max_per_window} cmds</span><span>Gateway reachable</span><span>${S.sim.reachable ? "yes" : "no"}</span></div>
      <div class="btns" style="margin-top:12px"><button class="btn small" id="simDrop">Drop gateway 10 s</button><button class="btn small" id="simBack">Reconnect now</button></div></div>`;
    $("#simDrop").onclick = () => post("/api/sim/disconnect", { seconds: 10 });
    $("#simBack").onclick = () => post("/api/sim/reconnect");
  } else sp.innerHTML = "";
}
const fmtUp = (s) => (s >= 3600 ? Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " m" : s >= 60 ? Math.floor(s / 60) + " m " + (s % 60) + " s" : s + " s");
async function loadStatusExtras() {
  const [info, logs] = await Promise.all([api("GET", "/api/connect-info"), api("GET", "/api/logs?n=120")]);
  if (info) {
    $("#connectCard").innerHTML = `<div class="qr">${info.qr_svg}</div><div style="text-align:center;font-weight:700">${esc(info.url)}</div>
      ${info.pin ? `<div class="pinbig">${esc(info.pin)}</div>` : ""}<p class="sub" style="text-align:center;margin:6px 0 0">Scan with the camera, or enter the PIN.<br>Share → Add to Home Screen for the full-screen app.</p>`;
  }
  if (logs) { const el = $("#logs"); el.textContent = logs.lines.join("\n"); el.scrollTop = el.scrollHeight; }
}
confirmTap($("#btnRotate"), "Tap again: sign out every phone", async () => { const r = await post("/api/auth/rotate"); if (r) { toast("New PIN " + r.pin); loadStatusExtras(); } });
confirmTap($("#btnQuit"), "Tap again to stop the controller", async () => { await post("/api/shutdown"); toast("Stopping — the room is being restored"); });

// ----------------------------------------------------------------------------- render + websocket
function render() {
  if (!S || !M) return;
  const lp = $("#linkPill"), lt = $("#linkTxt");
  const gs = S.gateway.state;
  lp.className = "pill " + (!wsUp ? "wait" : gs === "online" ? "ok" : gs === "connecting" ? "wait" : "bad");
  lt.textContent = !wsUp ? "Connecting" : gs === "online" ? "Online" : gs === "connecting" ? "Reconnecting" : "Gateway offline";
  $("#simPill").hidden = !S.simulated;
  paintRoom();
  paintFixtureSheet();
  if (view === "home") paintHome();
  else if (view === "party") paintParty();
  else if (view === "music") paintMusic();
  else if (view === "status") paintStatus();
}

let ws, retry = 0;
function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => { wsUp = true; retry = 0; $("#offline").classList.remove("show"); };
  ws.onmessage = (e) => {
    lastMsg = Date.now();
    try { const d = JSON.parse(e.data); if (d.type === "state") { S = d; render(); } } catch (err) { /* ignore */ }
  };
  ws.onclose = () => {
    wsUp = false; render();
    const wait = Math.min(5000, 400 * 2 ** retry++);
    setTimeout(() => { if (!wsUp) $("#offline").classList.add("show"); }, 2500);
    setTimeout(connect, wait);
  };
  ws.onerror = () => ws.close();
}
setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); if (wsUp && Date.now() - lastMsg > 15000) ws.close(); }, 8000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && (!ws || ws.readyState > 1)) connect(); });

async function boot() {
  M = await api("GET", "/api/meta");
  if (!M) { setTimeout(boot, 1500); return; }
  buildRoom(); buildScenes(); buildParty(); buildMusic();
  S = await api("GET", "/api/state");
  render();
  connect();
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) navigator.serviceWorker.register("/sw.js").catch(() => {});
  if (location.hash === "#music") show("music"); else if (location.hash === "#party") show("party");
}
boot();
