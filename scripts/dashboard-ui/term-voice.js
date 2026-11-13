// Talking to LOIS, the parts with no browser in them (owner decisions 12 Nov 2026): the wake word, the voice-activity
// segmenter always-on listening uses, and the 2 s window in which a heard line can still be cancelled. Pure functions and
// small state machines, no DOM, no request: term-mic.js uses them in Terminal 0's frame, and test_dashboard_field.py runs
// them under node. Nothing here sends anything; what to do with a line is the caller's.
"use strict";
(function (root) {
  const RATE = 16000;

  // ---------- the wake word. A line counts only if its first word is Lois, Louis or Computer (owner decision 13 Nov 2026), as
  // the listener (whisper base.en) writes it. Case, accents and punctuation don't matter (Lois, LOIS, "lois.", Loïs). "Louis"
  // is the commonest mishearing of a short "Lois" and a rare first word for anything said to a room. A spelled-out "L.O.I.S."
  // or "Lo-is" collapses to "lois" because only letters count. "Computer" is a plain word whisper spells one way; it counts only
  // as the first word (after an optional "hey", "hi", "ok" or "okay"), never inside a sentence. Everything else is not for LOIS.
  const WAKE = new Set(["lois", "louis", "computer"]);
  const LEAD = new Set(["hey", "hi", "ok", "okay"]);
  const fold = w => w.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z]/g, "");
  // The words after the wake word (trimmed, leading punctuation gone), "" if nothing followed it, or null if it isn't there.
  function wake(text) {
    if (typeof text !== "string") return null;
    const words = text.replace(/\s+/g, " ").trim().split(" ");
    let i = 0;
    if (words.length > 1 && LEAD.has(fold(words[0]))) i = 1;
    // "Lo is" and "L O I S": whisper sometimes splits the name; join at most four short pieces back.
    // Never across a comma or a question mark: "Lo, is it raining?" is not her name.
    for (let n = 1, joined = ""; n <= 4 && i + n <= words.length; n++) {
      if (n > 1 && /[,;:!?]/.test(words[i + n - 2])) break;
      joined += fold(words[i + n - 1]);
      if (WAKE.has(joined)) return words.slice(i + n).join(" ").replace(/^[\s,.;:!?\-–—…]+/, "").trim();
      if (joined.length >= 6) break;
    }
    return null;
  }

  // ---------- voice activity: a speech/silence decision every 20 ms frame from its RMS level (full scale 1.0).
  // The microphone runs with the browser's echo cancelling, noise suppression and gain control, which leave a quiet room at
  // about -60 to -50 dBFS and normal speech at arm's length around -30 to -20 dBFS. So:
  // - START_DB -40 dBFS (0.01): well above a suppressed room, well below speech. A frame also has to be 3x (about 10 dB) over
  //   the room's own level, which the segmenter learns from frames below the threshold, so a fan or a noisy room raises the bar.
  // - KEEP_DB -46 dBFS (0.005): once speaking, quieter frames still count (trailing consonants, a breath between words).
  // - START_FRAMES 4 (80 ms) of loud frames in a row begins an utterance: a click or a knock is shorter.
  // - END_MS 800 of quiet ends it (a pause between sentences is shorter); MIN_VOICED_MS 300 of speech in it or it is dropped.
  // - PRE_MS 300 of audio from before the start is kept, so the first sound of "LOIS" is not clipped.
  // - MAX_MS 30,000: an utterance is cut there and goes as it is, so a radio in the background can't hold the line open.
  const VAD = { FRAME: RATE / 50, START: 0.01, KEEP: 0.005, OVER: 3, START_FRAMES: 4, END_MS: 800, MIN_VOICED_MS: 300, PRE_MS: 300, MAX_MS: 30000 };

  function rms(pcm, from, to) {
    let s = 0;
    for (let i = from; i < to; i++) { const v = pcm[i] / 32768; s += v * v; }
    return to > from ? Math.sqrt(s / (to - from)) : 0;
  }

  // segmenter(onUtterance) -> { push(Int16Array), reset() }. push takes 16 kHz mono samples in any chunk size; each finished
  // utterance (pre-roll included, at most MAX_MS) is handed to onUtterance as one Int16Array, with why: "end" or "cap".
  function segmenter(onUtterance, opts) {
    const o = Object.assign({}, VAD, opts || {}), F = o.FRAME, frameMs = F / RATE * 1000;
    const endFrames = Math.round(o.END_MS / frameMs), minVoiced = Math.round(o.MIN_VOICED_MS / frameMs), maxFrames = Math.round(o.MAX_MS / frameMs);
    const preFrames = Math.round(o.PRE_MS / frameMs);
    let floor = o.KEEP / 2, part = new Int16Array(F), fill = 0, pre = [], run = [], speaking = false, cur = [], voiced = 0, quiet = 0;
    function reset() { fill = 0; pre = []; run = []; speaking = false; cur = []; voiced = 0; quiet = 0; }
    function finish(why) {
      const n = cur.reduce((a, f) => a + f.length, 0), out = new Int16Array(n); let at = 0;
      cur.forEach(f => { out.set(f, at); at += f.length; });
      const ok = voiced >= minVoiced; speaking = false; cur = []; voiced = 0; quiet = 0; run = [];
      if (ok) onUtterance(out, why);
    }
    function frame(f) {
      const level = rms(f, 0, f.length);
      if (!speaking) {
        if (level >= Math.max(o.START, floor * o.OVER)) { run.push(f); if (run.length >= o.START_FRAMES) { speaking = true; cur = pre.concat(run); voiced = run.length; quiet = 0; pre = []; run = []; } return level; }
        // A quiet frame: the room's level follows it slowly (about a second), and it joins the pre-roll.
        if (level < o.START) floor = floor * 0.95 + level * 0.05;
        pre = pre.concat(run, [f]).slice(-preFrames); run = [];
        return level;
      }
      cur.push(f);
      if (level >= Math.max(o.KEEP, floor * o.OVER * 0.5)) { voiced++; quiet = 0; } else quiet++;
      if (cur.length >= maxFrames) finish("cap");
      else if (quiet >= endFrames) { cur.length -= Math.max(0, quiet - Math.round(200 / frameMs)); finish("end"); }   // keep 200 ms of the tail
      return level;
    }
    function push(pcm) {
      let level = 0;
      for (let i = 0; i < pcm.length;) {
        const take = Math.min(F - fill, pcm.length - i);
        part.set(pcm.subarray(i, i + take), fill); fill += take; i += take;
        if (fill === F) { level = Math.max(level, frame(part)); part = new Int16Array(F); fill = 0; }
      }
      return level;
    }
    return { push, reset, get speaking() { return speaking; } };
  }

  // ---------- the cancel window: a heard line waits WINDOW_MS, then goes through send(text). cancel() drops it; a newer line
  // replaces the waiting one (the newer is what was meant). tick(left_ms, text) redraws the countdown; done(how) says how it
  // ended: "sent", "cancelled" or "replaced". Timers are passed in, so tests can run it on a fake clock.
  const WINDOW_MS = 2000, TICK_MS = 100;
  function cancelWindow({ send, tick, done, now, setTimeout: st, clearTimeout: ct, ms }) {
    const span = ms || WINDOW_MS; let item = null;
    function step() {
      if (!item) return;
      const left = Math.max(0, item.at + span - now());
      if (left <= 0) { const t = item.text; item = null; done("sent", t); send(t); return; }
      tick(left, item.text); item.timer = st(step, Math.min(TICK_MS, left));
    }
    function open(text) {
      if (item) { ct(item.timer); const old = item.text; item = null; done("replaced", old); }
      item = { text, at: now(), timer: 0 }; step();
    }
    function cancel() { if (!item) return false; ct(item.timer); const t = item.text; item = null; done("cancelled", t); return true; }
    return { open, cancel, get pending() { return item ? item.text : null; } };
  }

  const api = { wake, WAKE, segmenter, rms, VAD, cancelWindow, WINDOW_MS, RATE };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.TermVoice = api;
})(typeof window !== "undefined" ? window : globalThis);
