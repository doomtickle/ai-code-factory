// Talking to LOIS in Terminal 0's frame (owner decisions 12 Nov 2026). It lives in /term/field, on the terminal origin, so
// the main page never hears what is said, as it never sees what is typed, and spoken words go only where typed words go:
// into the Coordinator's terminal through term.js's typed(), the text and then Enter. Voice approves nothing (approving is
// the held light in the control unit) and adds no other path.
//
// Two ways, both in the strip under the terminal:
// - Push to talk: hold the mic key. The microphone opens only for the hold; on release the words are typed at once.
// - Always on (off by default, remembered in this browser): the microphone stays open while the tab is visible,
//   term-voice.js cuts what it hears into utterances, and each goes to the voice service's /listen. Only a line that starts
//   with the wake word LOIS counts: the rest is shown for 2 s with a countdown and typed when it runs out, unless Escape (here
//   or on the main page) or a click on it cancels it. A line without the wake word is dropped without a word: it went
//   nowhere but /listen. While LOIS speaks (the main page says only "tm:hush" and "tm:open") and for 300 ms after, what the
//   microphone hears is thrown away, so she never answers herself.
// Audio goes only to 127.0.0.1's voice service (POST /listen, raw 16 kHz mono 16-bit, no cookie). With the service down or its
// /health not saying listen: true, both keys are dark with a tooltip and typing is unchanged.
"use strict";
window.TermMic = (() => {
  const V = window.TermVoice, RATE = V.RATE, PTT_MAX_S = 60, PTT_MAX = RATE * PTT_MAX_S, PTT_MIN = RATE * 0.3;
  const HEALTH_MS = 30000, LISTEN_TIMEOUT_MS = 90000, AFTER_SPEECH_MS = 300, HUSH_STALE_MS = 5000, QUEUE_MAX = 2, LINE_MAX = 1000;
  const TYPE = "audio/L16; rate=16000; channels=1", LE = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1, STORE = "lois-always-on";
  const OFF = "Talking needs the voice service (factory voice start). Typing works.";
  const BLOCKED = "Microphone blocked. Allow it in site settings, then reload.";
  const LISTENING = "Always on: listening for “LOIS…”";
  const WORDS = new Set(["tm:hush", "tm:open", "tm:cancel"]);

  function attach({ base, mainOrigin, type, strip, mic, auto, status, pending, pendingText, pendingLeft }) {
    let usable = false, blocked = false, held = false, timer = 0, ptt = null, pttPhase = "idle", always = null, speaking = false, hushAt = 0, hushUntil = 0;
    let alwaysOn = false;
    try { alwaysOn = localStorage.getItem(STORE) === "1"; } catch (e) {}

    // ---------- one line of state
    function idleLine() { return blocked ? BLOCKED : !usable ? (alwaysOn ? "Always on waits for the voice service." : "") : alwaysOn ? (hushed() ? "Always on: paused while LOIS speaks" : always && always.live ? LISTENING : "Always on: starting the microphone…") : ""; }
    // A message stays until its time is up (or, with no time, until the next one); otherwise the line says what is listening.
    function show(text, ms) { clearTimeout(timer); held = !!text; status.textContent = text || idleLine(); if (text && ms) timer = setTimeout(() => { held = false; refresh(); }, ms); }
    function refresh() { if (!held) status.textContent = idleLine(); }
    function setKeys() {
      const on = usable && !blocked;
      mic.setAttribute("aria-disabled", String(!on)); mic.title = blocked ? BLOCKED : on ? "Hold to talk" : OFF;
      auto.setAttribute("aria-pressed", String(alwaysOn)); auto.setAttribute("aria-disabled", String(blocked));
      auto.title = blocked ? BLOCKED : !usable ? OFF : alwaysOn ? "Always on: say “LOIS, …”. Click to stop listening." : "Listen all the time for “LOIS, …”";
      strip.dataset.always = alwaysOn && always && always.live ? (hushed() ? "hushed" : "live") : alwaysOn ? "waiting" : "off";
      strip.dataset.ptt = pttPhase;
    }
    async function check() {
      try {
        const c = new AbortController(), t = setTimeout(() => c.abort(), 2000);
        const r = await fetch(base + "/health", { credentials: "omit", cache: "no-store", signal: c.signal }); clearTimeout(t);
        const j = r.ok ? await r.json() : null; usable = !!j && j.listen === true;
      } catch (e) { usable = false; }
      setKeys(); syncAlways(); refresh();
    }

    // ---------- the microphone: 16 kHz mono 16-bit chunks of 20 ms from term-mic-worklet.js
    async function openMic(onChunk) {
      const m = { stream: null, ctx: null, node: null, ended: null, end: null };
      try {
        m.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      } catch (e) {
        if (e && (e.name === "NotAllowedError" || e.name === "SecurityError")) { blocked = true; setKeys(); throw new Error("blocked"); }
        throw new Error("none");
      }
      try {
        m.ctx = new AudioContext();
        await m.ctx.audioWorklet.addModule("/term-mic-worklet.js");
        m.node = new AudioWorkletNode(m.ctx, "term-mic", { numberOfInputs: 1, numberOfOutputs: 1 });
        const mute = m.ctx.createGain(); mute.gain.value = 0;   // keeps the node pulled; nothing is heard
        m.ctx.createMediaStreamSource(m.stream).connect(m.node); m.node.connect(mute).connect(m.ctx.destination);
        m.ended = new Promise(res => { m.end = res; });
        m.node.port.onmessage = ev => { const d = ev.data || {}; if (d.pcm instanceof Int16Array) onChunk(d.pcm, d.level || 0); if (d.end) m.end(); };
        await m.ctx.resume();
      } catch (e) { closeMic(m); throw new Error("start"); }
      return m;
    }
    async function drainMic(m) { if (m.node) { m.node.port.postMessage("stop"); await Promise.race([m.ended, new Promise(res => setTimeout(res, 500))]); } closeMic(m); }
    function closeMic(m) { if (m.stream) m.stream.getTracks().forEach(t => t.stop()); if (m.ctx) m.ctx.close().catch(() => {}); m.stream = m.ctx = m.node = null; }   // the browser's mic light goes out
    const failLine = e => e.message === "blocked" ? BLOCKED : e.message === "none" ? "No microphone is available here. Typing still works." : "The microphone couldn't start. Typing still works.";

    // ---------- /listen, one at a time from this frame (the service takes one at a time anyway)
    let chain = Promise.resolve(), queued = 0;
    function listen(pcm) {
      const body = new Int16Array(pcm);
      if (!LE) for (let i = 0; i < body.length; i++) body[i] = ((body[i] & 0xff) << 8) | ((body[i] >> 8) & 0xff);
      const job = chain.then(async () => {
        try {
          const c = new AbortController(), t = setTimeout(() => c.abort(), LISTEN_TIMEOUT_MS);
          const res = await fetch(base + "/listen", { method: "POST", credentials: "omit", cache: "no-store", headers: { "Content-Type": TYPE }, body: body.buffer, signal: c.signal });
          clearTimeout(t);
          if (res.ok) { const j = await res.json(); return typeof (j && j.text) === "string" ? { text: j.text.replace(/\s+/g, " ").trim() } : { err: "The voice service sent no words back." }; }
          return { err: res.status === 503 ? "LOIS's ears are busy. Try again in a moment." : res.status === 413 ? "That was too long to send."
            : res.status === 415 ? "The voice service refused the audio. Update it: factory voice start --build." : `The voice service answered ${res.status}. Nothing was sent.` };
        } catch (e) { check(); return { err: "Couldn't reach the voice service. Nothing was sent." }; }
      });
      chain = job.catch(() => {});
      return job;
    }
    function typeLine(text) {
      const why = type(text.slice(0, LINE_MAX));
      if (why === "off") show("LOIS is off. Nothing was sent.", 5000);
      else if (why) show("Not connected to LOIS. Nothing was sent.", 5000);
      else show("");
    }

    // ---------- push to talk
    async function pttStart() {
      if (ptt) return;
      if (blocked) { show(BLOCKED); return; }
      if (!usable) { show(OFF, 5000); return; }
      const r = ptt = { chunks: [], n: 0, released: false, send: false, m: null, cap: 0 };
      pttPhase = "starting"; setKeys(); show("Listening… let go to send");
      if (always && always.seg) always.seg.reset();
      try { r.m = await openMic(take); }
      catch (e) { ptt = null; pttPhase = "idle"; setKeys(); show(failLine(e), e.message === "blocked" ? 0 : 6000); return; }
      function take(pcm, level) {
        if (r.n >= PTT_MAX) return;
        const part = pcm.length > PTT_MAX - r.n ? pcm.subarray(0, PTT_MAX - r.n) : pcm;
        r.chunks.push(part); r.n += part.length; mic.style.setProperty("--lvl", String(Math.min(1, level * 3)));
        if (r.n >= PTT_MAX && ptt === r) pttStop(true, "That's the 60 second limit; sending what you said.");
      }
      if (r.released) { pttFinish(r); return; }
      pttPhase = "listening"; setKeys();
      r.cap = setTimeout(() => pttStop(true, "That's the 60 second limit; sending what you said."), PTT_MAX_S * 1000 + 250);
    }
    function pttStop(send, why) { const r = ptt; if (!r || r.released) return; r.released = true; r.send = send; r.why = why || ""; if (pttPhase === "listening") pttFinish(r); }
    async function pttFinish(r) {
      clearTimeout(r.cap); mic.style.setProperty("--lvl", "0");
      if (r.m) await drainMic(r.m);
      if (ptt !== r) return; ptt = null;
      if (!r.send) { pttPhase = "idle"; setKeys(); show("Dropped. Nothing was sent.", 3000); return; }
      if (r.n < PTT_MIN) { pttPhase = "idle"; setKeys(); show("Hold to talk: keep holding while you speak.", 4000); return; }
      const pcm = new Int16Array(r.n); let at = 0; r.chunks.forEach(c => { pcm.set(c, at); at += c.length; });
      pttPhase = "transcribing"; setKeys(); show(r.why || "Transcribing…");
      const out = await listen(pcm);
      pttPhase = "idle"; setKeys();
      if (out.err) { show(out.err, 6000); return; }
      if (!out.text) { show("I didn't catch any words. Nothing was sent.", 4000); return; }
      typeLine(out.text);   // push to talk is its own yes: no wake word, no window
    }
    mic.addEventListener("mousedown", e => e.preventDefault());   // a press here must not move focus (or grow the pane)
    mic.addEventListener("pointerdown", e => { if (e.button !== 0) return; e.preventDefault(); try { mic.setPointerCapture(e.pointerId); } catch (x) {} pttStart(); });
    mic.addEventListener("pointerup", () => pttStop(true));
    mic.addEventListener("pointercancel", () => pttStop(false));
    mic.addEventListener("click", e => e.preventDefault());
    mic.addEventListener("keydown", e => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); pttStart(); } else if (e.key === "Escape") pttStop(false); });
    mic.addEventListener("keyup", e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); pttStop(true); } });

    // ---------- always on
    const hushed = () => { const now = Date.now(); if (speaking && now - hushAt > HUSH_STALE_MS) { speaking = false; hushUntil = now + AFTER_SPEECH_MS; } return speaking || now < hushUntil; };
    const win = V.cancelWindow({
      now: () => Date.now(), setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: t => clearTimeout(t),
      tick: (left, text) => { pendingText.textContent = text; pendingLeft.textContent = (left / 1000).toFixed(1) + " s"; pending.style.setProperty("--left", String(left / V.WINDOW_MS)); pending.hidden = false; status.hidden = true; },
      done: how => { pending.hidden = true; status.hidden = false; if (how === "cancelled") show("Cancelled. Nothing was sent.", 2500); },
      send: text => typeLine(text),
    });
    function heard(pcm) {
      if (queued >= QUEUE_MAX) return;   // she is still working on the last ones: this one is dropped
      queued++;
      listen(pcm).then(out => {
        queued--;
        if (!alwaysOn) return;
        if (out.err) { show(out.err, 4000); return; }
        const rest = V.wake(out.text);
        if (rest === null) return;   // not for LOIS: dropped, unseen
        if (!rest) { show("I heard “LOIS”. Say it in one go: “LOIS, …”", 4000); return; }
        win.open(rest.slice(0, LINE_MAX));
      });
    }
    async function startAlways() {
      if (always) return;
      const a = always = { live: false, m: null, seg: V.segmenter(pcm => { if (always === a) heard(pcm); }) };
      setKeys(); show("");
      let m;
      try { m = await openMic((pcm, level) => {
        if (always !== a) return;
        if (ptt || hushed()) { a.seg.reset(); auto.style.setProperty("--lvl", "0"); return; }   // her own voice, or a hold in progress: thrown away
        a.seg.push(pcm); auto.style.setProperty("--lvl", String(Math.min(1, level * 3)));
      }); }
      catch (e) { if (always === a) always = null; if (e.message === "blocked") { alwaysOn = false; setKeys(); show(BLOCKED); } else { setKeys(); show(failLine(e), 6000); } return; }
      if (always !== a) { closeMic(m); return; }
      a.m = m; a.live = true; setKeys(); refresh();
    }
    function stopAlways() { const a = always; if (!a) return; always = null; if (a.m) closeMic(a.m); auto.style.setProperty("--lvl", "0"); setKeys(); refresh(); }
    // Listening only while it is switched on, the service can listen, the microphone isn't refused and the tab is visible.
    function syncAlways() { if (alwaysOn && usable && !blocked && !document.hidden) startAlways(); else stopAlways(); }
    auto.addEventListener("mousedown", e => e.preventDefault());
    auto.addEventListener("click", e => {
      e.preventDefault(); if (blocked) { show(BLOCKED); return; }
      alwaysOn = !alwaysOn; try { localStorage.setItem(STORE, alwaysOn ? "1" : "0"); } catch (x) {}
      if (!alwaysOn) win.cancel();
      setKeys(); syncAlways(); show(alwaysOn && !usable ? OFF : "", alwaysOn && !usable ? 5000 : 0);
    });
    document.addEventListener("visibilitychange", () => { if (document.hidden) { pttStop(false); win.cancel(); } syncAlways(); });
    addEventListener("blur", () => pttStop(false));

    // ---------- cancelling: Escape here (before the terminal sees it) or a click on the waiting line; the main page's Escape arrives as "tm:cancel"
    addEventListener("keydown", e => {
      if (e.key !== "Escape") return;
      if (win.pending !== null || ptt) { e.preventDefault(); e.stopImmediatePropagation(); if (ptt) pttStop(false); else win.cancel(); }   // otherwise Escape is the Coordinator's
    }, true);
    pending.addEventListener("mousedown", e => e.preventDefault());
    pending.addEventListener("click", e => { e.preventDefault(); win.cancel(); });
    addEventListener("message", e => {
      if (e.source !== window.parent || e.origin !== mainOrigin || typeof e.data !== "string" || !WORDS.has(e.data)) return;
      const now = Date.now();
      if (e.data === "tm:hush") { speaking = true; hushAt = now; if (always) always.seg.reset(); }
      else if (e.data === "tm:open") { if (speaking) hushUntil = now + AFTER_SPEECH_MS; speaking = false; }
      else win.cancel();
      setKeys(); refresh();
    });
    setInterval(() => { setKeys(); refresh(); }, 500);   // the hush ends by the clock

    strip.hidden = false; setKeys(); show(""); check(); setInterval(check, HEALTH_MS);
    return { get pending() { return win.pending; } };
  }
  return { attach };
})();
