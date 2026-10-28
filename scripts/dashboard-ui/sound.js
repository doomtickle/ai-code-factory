// The sound layer (doomtickle/passdesk-ops#162): short synthesized cues for the three
// floor events floor.js announces on document, and (#192) for the two attention events
// app.js announces: a write held (the alert horn) and a job finished (a ding). Off until the SOUND key (or S) is
// pressed; the choice is kept in this browser. Display only: it listens, makes no
// request, loads no audio file and reads nothing from a cue event but its type, so
// agent-written text never reaches it. Tones come from Web Audio oscillators. The one other thing it reads is
// `sound` in the state's `look` (dashboard.toml's default, #196), once, through look.js's checks.
"use strict";

(() => {
  const key = document.getElementById("sound-key");
  if (!key) return;
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) { key.disabled = true; return; }
  const STORE = "fc1984-sound";
  const GAP = 0.25;   // seconds between cues: at most four a second, a burst coalesces to one trailing cue
  const FLOOR = ["passed", "docked", "confiscated"];
  const ATTENTION = ["held", "done"];
  const RANK = { passed: 0, done: 1, docked: 2, confiscated: 3, held: 4 };   // a burst plays its most serious cue

  let on = false, ctx = null, master = null, lastAt = -Infinity, pending = null, timer = 0;

  // One short note: a pitch (optionally gliding to `to`) under a fast attack and decay.
  function note(type, freq, to, at, len, peak) {
    const osc = ctx.createOscillator(), amp = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, at);
    if (to) osc.frequency.exponentialRampToValueAtTime(to, at + len);
    amp.gain.setValueAtTime(0.0001, at);
    amp.gain.exponentialRampToValueAtTime(peak, at + 0.01);
    amp.gain.exponentialRampToValueAtTime(0.0001, at + len);
    osc.connect(amp);
    amp.connect(master);
    osc.start(at);
    osc.stop(at + len + 0.02);
  }
  const CUES = {
    passed: t => note("sine", 880, 0, t, 0.09, 0.5),                                // soft blip
    confiscated: t => { note("sawtooth", 120, 55, t, 0.3, 0.45); note("sine", 70, 40, t, 0.25, 0.9); },   // low buzz and thud
    docked: t => { note("sine", 523.25, 0, t, 0.14, 0.5); note("sine", 783.99, 0, t + 0.11, 0.2, 0.5); },  // rising two-note chime
    done: t => note("sine", 1318.5, 0, t, 0.22, 0.45),                              // short high ding
    held: t => { note("square", 440, 0, t, 0.16, 0.4); note("square", 330, 0, t + 0.2, 0.16, 0.4); note("square", 440, 0, t + 0.4, 0.16, 0.4); note("square", 330, 0, t + 0.6, 0.3, 0.4); },   // alert horn
  };

  // Only a user gesture may create or resume the context (autoplay rules); a cue that
  // arrives while it isn't running is dropped, never queued for later.
  function wake() {
    try {
      if (!ctx) {
        ctx = new AC();
        master = ctx.createGain();
        master.gain.value = 0.18;   // quiet: this plays beside someone's work
        master.connect(ctx.destination);
      }
      if (ctx.state !== "running") { const p = ctx.resume(); if (p && p.catch) p.catch(() => {}); }
      return true;
    } catch (e) { return false; }
  }

  function play(name) {
    pending = null;
    lastAt = performance.now() / 1000;
    try { CUES[name](ctx.currentTime + 0.01); } catch (e) { /* no sound is fine */ }
  }
  function cue(name) {
    if (!on || !ctx || ctx.state !== "running") return;
    const wait = lastAt + GAP - performance.now() / 1000;
    if (wait <= 0) { play(name); return; }
    if (pending === null || RANK[name] > RANK[pending]) pending = name;
    if (!timer) timer = setTimeout(() => { timer = 0; if (on && pending) play(pending); }, wait * 1000);
  }

  function setOn(v, save) {
    on = v;
    key.setAttribute("aria-pressed", String(v));
    if (save) { try { localStorage.setItem(STORE, v ? "1" : "0"); } catch (e) { /* no storage: this tab only */ } }
    if (!v) {
      pending = null;
      clearTimeout(timer);
      timer = 0;
      if (ctx && ctx.state === "running") { const p = ctx.suspend(); if (p && p.catch) p.catch(() => {}); }
    }
  }

  key.addEventListener("click", () => {
    if (on) { setOn(false, true); return; }
    if (!wake()) { key.disabled = true; return; }
    setOn(true, true);
    // Confirm it works; the context may still be resuming, so skip the throttle and the state check.
    try { CUES.passed(ctx.currentTime + 0.01); lastAt = performance.now() / 1000; } catch (e) { /* silent */ }
  });
  try { if (localStorage.getItem(STORE) === "1") setOn(true, false); } catch (e) { /* no storage: off */ }
  // A remembered "on" stays silent until the first click or key press lets the browser start audio.
  for (const t of ["pointerdown", "keydown"]) document.addEventListener(t, () => { if (on && (!ctx || ctx.state !== "running")) wake(); }, { passive: true });

  // dashboard.toml's `sound = true` (look.js has checked it) turns it on once, from the first state, but only for a browser
  // that has never chosen: a remembered "off" or "on" wins. It starts as silent as a remembered "on" until a click or key.
  let looked = false;
  document.addEventListener("factory:state", e => {
    if (looked || !e.detail) return;
    looked = true;
    let chosen = true;
    try { chosen = localStorage.getItem(STORE) !== null; } catch (err) { /* no storage: nothing is remembered */ chosen = false; }
    if (!chosen && !on && window.FactoryLook && window.FactoryLook.read(e.detail.look).sound === true) setOn(true, false);
  });

  for (const name of FLOOR) document.addEventListener(`factory-floor:${name}`, () => cue(name));
  for (const name of ATTENTION) document.addEventListener(`factory-attention:${name}`, () => cue(name));
})();
