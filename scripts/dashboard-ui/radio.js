// The factory radio (an easter egg beside the sound layer, sound.js): a looping, procedurally
// generated ambient track, chill sci-fi beats in D dorian at 72 BPM. Typing the Konami code
// on the main page (Up Up Down Down Left Right Left Right B A) switches it on or off; while
// it plays a small RADIO key appears next to SOUND and turns it off. The choice is kept in
// this browser. Display only: it listens for key names, makes no request, loads no audio
// file and reads nothing from the page, the state or any event but the name of a key, so
// agent-written text never reaches it. Every sound comes from Web Audio oscillators and
// generated noise, through its own AudioContext (sound.js keeps its own). It never cancels a key or
// stops its propagation, so the shortcuts in app.js see every key as before.
"use strict";

(() => {
  const badge = document.getElementById("radio-key");
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!badge || !AC) return;   // no badge or no Web Audio: the code does nothing
  const STORE = "fc1984-radio";
  const CODE = ["arrowup", "arrowup", "arrowdown", "arrowdown", "arrowleft", "arrowright", "arrowleft", "arrowright", "b", "a"];
  const MODS = new Set(["shift", "control", "alt", "meta", "altgraph", "capslock"]);

  // ---------- the music
  const BPM = 72;
  const BEAT = 60 / BPM;          // 0.833 s
  const STEP = BEAT / 4;          // the grid is sixteenth notes
  const BAR = 16 * STEP;          // 4/4: 3.33 s
  const LOOP = 8;                 // the progression is eight bars (about 27 s), then it repeats
  const AHEAD = 0.2;              // schedule notes this far ahead of the audio clock (seconds)
  const TICK = 100;               // look-ahead timer period (ms)
  const LEVEL = 0.10;             // master gain: under sound.js's cues (0.18)

  const mtof = m => 440 * Math.pow(2, (m - 69) / 12);
  const rnd = (a, b) => a + Math.random() * (b - a);

  // D dorian, one chord per bar: [bass root, four upper voices] as MIDI note numbers.
  const BARS = [
    [38, [53, 57, 60, 64]],   // Dm9
    [43, [57, 59, 62, 65]],   // G9 (no root)
    [45, [55, 60, 64, 67]],   // Am7
    [48, [55, 59, 64, 67]],   // Cmaj7
    [38, [53, 57, 60, 64]],   // Dm9
    [40, [55, 59, 62, 64]],   // Em7
    [41, [57, 60, 64, 65]],   // Fmaj7
    [45, [55, 60, 64, 67]],   // Am7
  ];
  const SCALE = [62, 65, 67, 69, 72, 74, 77, 79, 81];   // D minor pentatonic, D4 to A5: the melody's notes

  let on = false, ctx = null, master = null, room = null, noise = null;
  let live = null;         // the running session: { bus, dry, wet, echo, pad, sources }
  let timer = 0, offTimer = 0, oldBus = null, nextAt = 0, step = 0, mi = 3, restBar = false;

  // ---------- building blocks
  // One second of white noise, shared by the drums.
  function noiseBuffer() {
    const n = ctx.sampleRate, buf = ctx.createBuffer(1, n, ctx.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
    return buf;
  }
  // The reverb's impulse response: stereo noise that dies away exponentially (about -60 dB at 2.5 s).
  function impulse(seconds) {
    const n = Math.floor(ctx.sampleRate * seconds), buf = ctx.createBuffer(2, n, ctx.sampleRate);
    for (let c = 0; c < 2; c++) {
      const d = buf.getChannelData(c);
      for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * Math.exp(-6.9 * i / n);
    }
    return buf;
  }
  // Start a source and remember it, so turning the radio off can stop everything still scheduled.
  function go(src, at, until) {
    live.sources.add(src);
    src.onended = () => live && live.sources.delete(src);
    src.start(at);
    src.stop(until);
    return src;
  }
  // A gain envelope: exponential rise to `peak`, hold, exponential fall to silence at `end`.
  function env(g, at, rise, peak, fallAt, end) {
    g.gain.setValueAtTime(0.0001, at);
    g.gain.exponentialRampToValueAtTime(peak, at + rise);
    if (fallAt > at + rise) g.gain.setValueAtTime(peak, fallAt);
    g.gain.exponentialRampToValueAtTime(0.0001, end);
  }
  // Route a voice to the dry bus and, scaled, to the reverb (and the echo, for the melody).
  function route(node, wet, echo) {
    node.connect(live.dry);
    if (wet) { const s = ctx.createGain(); s.gain.value = wet; node.connect(s); s.connect(live.wet); }
    if (echo) { const s = ctx.createGain(); s.gain.value = echo; node.connect(s); s.connect(live.echo); }
  }

  // ---------- the instruments
  // A pad chord: each voice is a pair of slightly detuned sawtooths plus a triangle,
  // all into the shared low-pass filter (live.pad) that the slow LFO sweeps.
  function pad(chord, at) {
    const end = at + BAR + 1.2;
    for (const m of chord) {
      const amp = ctx.createGain();
      env(amp, at, 1.1, 0.11, at + BAR - 0.1, end);
      for (const [type, cents, level] of [["sawtooth", -8, 0.55], ["sawtooth", 8, 0.55], ["triangle", 0, 0.8]]) {
        const o = ctx.createOscillator(), v = ctx.createGain();
        o.type = type;
        o.frequency.value = mtof(m);
        o.detune.value = cents;
        v.gain.value = level;
        o.connect(v);
        v.connect(amp);
        go(o, at, end + 0.05);
      }
      amp.connect(live.pad);
    }
  }
  function bass(root, at) {
    const o = ctx.createOscillator(), amp = ctx.createGain();
    o.type = "sine";
    o.frequency.value = mtof(root);
    env(amp, at, 0.08, 0.5, at + BAR - 0.3, at + BAR + 0.15);
    o.connect(amp);
    amp.connect(live.dry);
    go(o, at, at + BAR + 0.2);
  }
  // A plucked note for the sparse melody: a triangle through a closing low-pass.
  function pluck(m, at, len, vel) {
    const o = ctx.createOscillator(), f = ctx.createBiquadFilter(), amp = ctx.createGain();
    o.type = "triangle";
    o.frequency.value = mtof(m);
    f.type = "lowpass";
    f.frequency.setValueAtTime(3200, at);
    f.frequency.exponentialRampToValueAtTime(700, at + len);
    env(amp, at, 0.012, 0.32 * vel, at, at + len);
    o.connect(f);
    f.connect(amp);
    route(amp, 0.7, 0.55);
    go(o, at, at + len + 0.05);
  }
  // Drums. A sine that drops in pitch is the kick; filtered noise makes the snare and hats.
  function kick(at, vel) {
    const o = ctx.createOscillator(), amp = ctx.createGain();
    o.type = "sine";
    o.frequency.setValueAtTime(130, at);
    o.frequency.exponentialRampToValueAtTime(42, at + 0.14);
    env(amp, at, 0.004, 0.95 * vel, at, at + 0.4);
    o.connect(amp);
    amp.connect(live.dry);
    go(o, at, at + 0.45);
  }
  function hiss(at, len, peak, type, freq, wet) {
    const s = ctx.createBufferSource(), f = ctx.createBiquadFilter(), amp = ctx.createGain();
    s.buffer = noise;
    f.type = type;
    f.frequency.value = freq;
    if (type === "bandpass") f.Q.value = 0.8;
    env(amp, at, 0.003, peak, at, at + len);
    s.connect(f);
    f.connect(amp);
    route(amp, wet, 0);
    go(s, at, at + len + 0.02);
    return s;
  }
  const snare = (at, vel) => hiss(at, 0.2, 0.28 * vel, "bandpass", 1900, 0.25);
  const hat = (at, vel, open) => hiss(at, open ? 0.14 : 0.045, 0.16 * vel, "highpass", 7500, 0.08);

  // ---------- the scheduler
  // What happens on sixteenth-note `s` of the loop, to sound at time `at`.
  function play(s, at) {
    const b = s >> 4, i = s & 15, vary = () => rnd(0.75, 1.1);
    if (i === 0) {
      pad(BARS[b][1], at);
      bass(BARS[b][0], at);
      restBar = Math.random() < 0.2;   // now and then a bar leaves the melody out
    }
    if (i === 0 || i === 8) kick(at, vary());
    else if (i === 10 && Math.random() < 0.3) kick(at, 0.45);   // a ghost kick
    if (i === 4 || i === 12) snare(at, vary());
    if (i % 4 === 2) hat(at, vary(), i === 14 && Math.random() < 0.4);
    else if (i & 1 && Math.random() < 0.25) hat(at + 0.012, 0.35 * vary(), false);   // quiet, slightly late
    // Melody and arpeggio on the eighth-note grid: a random walk on the scale, with rests.
    if (!restBar && (i & 1) === 0 && Math.random() < (i === 0 ? 0.5 : 0.32)) {
      mi = Math.max(0, Math.min(SCALE.length - 1, mi + Math.floor(rnd(-2.5, 3.5))));
      pluck(SCALE[mi], at, rnd(0.5, 1.2) * BEAT, rnd(0.55, 1));
    }
  }
  // Look ahead against the audio clock, so the timer's jitter never reaches the music.
  function tick() {
    timer = 0;
    if (!on || !live) return;
    if (ctx.state === "running") {
      if (nextAt < ctx.currentTime) nextAt = ctx.currentTime + 0.05;   // the page stalled: don't play the past in a burst
      while (nextAt < ctx.currentTime + AHEAD) {
        try { play(step, nextAt); } catch (e) { /* a missed note is fine */ }
        nextAt += STEP;
        step = (step + 1) % (LOOP * 16);
      }
    }
    timer = setTimeout(tick, TICK);
  }

  // The graph for one run: a bus into the master, a dry path, the reverb, an echo and
  // the pad filter with its LFO. Rebuilt on every start, dropped on every stop.
  function begin() {
    if (!on || live || !ctx || ctx.state !== "running") return;
    const reverb = ctx.createConvolver(), bus = ctx.createGain(), dry = ctx.createGain(), wet = ctx.createGain(), echo = ctx.createDelay(2);
    const fb = ctx.createGain(), tone = ctx.createBiquadFilter(), padF = ctx.createBiquadFilter();
    const lfo = ctx.createOscillator(), depth = ctx.createGain();
    reverb.buffer = room;
    live = { bus, dry, wet, echo, pad: padF, sources: new Set() };
    bus.connect(master);
    dry.connect(bus);
    wet.gain.value = 0.55;
    wet.connect(reverb);
    reverb.connect(bus);
    echo.delayTime.value = BEAT * 0.75;   // a dotted eighth
    fb.gain.value = 0.38;
    tone.type = "lowpass";
    tone.frequency.value = 1800;
    echo.connect(tone);
    tone.connect(fb);
    fb.connect(echo);
    tone.connect(dry);
    tone.connect(wet);
    padF.type = "lowpass";
    padF.frequency.value = 900;
    padF.Q.value = 1.4;
    padF.connect(dry);
    padF.connect(wet);
    lfo.type = "sine";
    lfo.frequency.value = 0.07;   // one sweep takes about 14 s
    depth.gain.value = 450;
    lfo.connect(depth);
    depth.connect(padF.frequency);
    const now = ctx.currentTime;
    bus.gain.setValueAtTime(0.0001, now);
    bus.gain.exponentialRampToValueAtTime(1, now + 0.4);
    go(lfo, now, now + 86400);
    nextAt = now + 0.1;
    step = 0;
    restBar = false;
    tick();
  }
  // Cancel the pending park of the last session and detach its (silent) bus from the master.
  function release() {
    clearTimeout(offTimer);
    offTimer = 0;
    if (oldBus) { try { oldBus.disconnect(); } catch (e) { /* gone */ } oldBus = null; }
  }
  // Stop every node still scheduled, then park the context.
  function end() {
    clearTimeout(timer);
    timer = 0;
    const l = live;
    live = null;
    if (!l || !ctx) return;
    const now = ctx.currentTime;
    try { l.bus.gain.cancelScheduledValues(now); l.bus.gain.setTargetAtTime(0, now, 0.02); } catch (e) { /* closing anyway */ }
    for (const s of l.sources) { try { s.stop(now + 0.1); } catch (e) { /* already ended */ } }
    l.sources.clear();
    release();
    oldBus = l.bus;
    offTimer = setTimeout(() => {
      release();
      if (!on && ctx && ctx.state === "running") { const p = ctx.suspend(); if (p && p.catch) p.catch(() => {}); }
    }, 200);
  }

  // Only a user gesture may create or resume the context (autoplay rules); the music
  // starts once it is running, and a remembered "on" waits for the first gesture.
  function wake() {
    try {
      if (!ctx) {
        ctx = new AC();
        master = ctx.createGain();
        master.gain.value = LEVEL;
        master.connect(ctx.destination);
        room = impulse(2.5);
        noise = noiseBuffer();
      }
      release();
      if (ctx.state === "running") begin();
      else { const p = ctx.resume(); if (p && p.then) p.then(begin, () => {}); }
      return true;
    } catch (e) { return false; }
  }

  function setOn(v, save) {
    on = v;
    badge.hidden = !v;
    badge.setAttribute("aria-pressed", String(v));
    if (save) { try { localStorage.setItem(STORE, v ? "1" : "0"); } catch (e) { /* no storage: this tab only */ } }
    if (!v) end();
  }

  // ---------- the Konami code
  let typed = 0;   // how much of the code has been typed
  document.addEventListener("keydown", e => {
    if (e.repeat || typeof e.key !== "string") return;
    const t = e.target;
    if (t && t.nodeType === 1 && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) { typed = 0; return; }
    const k = e.key.toLowerCase();
    if (MODS.has(k)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) { typed = 0; return; }
    if (k !== CODE[typed]) { typed = k === CODE[0] ? (typed === 2 ? 2 : 1) : 0; return; }   // a wrong key starts over; an extra Up after Up Up keeps those two
    if (++typed < CODE.length) return;
    typed = 0;
    if (on) { setOn(false, true); return; }
    setOn(true, true);   // before waking: the music starts only while the radio is on
    if (!wake()) setOn(false, false);
  });
  badge.addEventListener("click", () => setOn(false, true));
  try { if (localStorage.getItem(STORE) === "1") setOn(true, false); } catch (e) { /* no storage: off */ }
  // A remembered "on" shows the badge but stays silent until the first click or key press lets the browser start audio.
  for (const t of ["pointerdown", "keydown"]) document.addEventListener(t, () => { if (on && (!live || ctx.state !== "running")) wake(); }, { passive: true });
})();
