// LOIS's live voice (doomtickle/passdesk-ops#207): streams Kokoro audio from the optional local service (factory voice),
// plays it through a long synthetic hall, and drives the lips from Kokoro's phoneme timing. No cookie is ever sent, and the
// page sends it only fixed-template sentences. If the service isn't running, available() is false and LOIS stays silent.
window.LiveVoice = (() => {
  const PORT = 8873, BASE = `http://127.0.0.1:${PORT}`; let up = false;
  async function check() { try { const c = new AbortController(), t = setTimeout(() => c.abort(), 1500), r = await fetch(BASE + "/health", { credentials: "omit", signal: c.signal }); clearTimeout(t); up = r.ok; } catch (e) { up = false; } }
  function watch() { check(); setInterval(check, 30000); }
  const DEF = { name: "Matriarch blend", mix: [["af_nicole", .34], ["af_kore", .33], ["bm_daniel", .33]], lang: "a", speed: 1.3, rate: 1, dry: .98, wet: .1, duck: .02, decay: 3, gap: .16 };
  const KEY = "lois-voice-settings"; let S = Object.assign({}, DEF);
  try { S = Object.assign(S, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (e) {}
  let ctx, input, an, conv, dryG, wetG, master, duckG, cur = null, td, fd, stats = {};
  // ---------- lip sync: phoneme timing from the server becomes one weight curve per viseme, sampled against the audio clock
  const FRAME = .025, STEP = .01, HALF = 4;                  // Kokoro frame; curve grid; smoothing half-width in steps, so neighbouring shapes blend
  const AUDIO_AHEAD = .05, LIPS_LEAD = .03;                  // the waveform runs 50 ms ahead of Kokoro's durations (measured); lips start shaping a little before the sound
  const VIS = ["PP", "FF", "TH", "DD", "kk", "CH", "SS", "nn", "RR", "aa", "E", "ih", "oh", "ou"], VI = Object.fromEntries(VIS.map((v, i) => [v, i]));
  const SHAPE = {}; [["PP", "pbm"], ["FF", "fv"], ["TH", "θð"], ["DD", "tdTɾ"], ["kk", "kɡgŋçxχ"], ["CH", "ʧʤʃʒ"], ["SS", "szS"], ["nn", "nl"], ["RR", "ɹɚɜꭧ"], ["aa", "ɑaɐʌæ"], ["E", "ɛe"], ["ih", "ɪiᵻj"], ["oh", "ɔɒ"], ["ou", "uʊw"]].forEach(([v, cs]) => { for (const c of cs) SHAPE[c] = v; });
  const GLIDE = { A: ["E", "ih"], I: ["aa", "ih"], O: ["oh", "ou"], Q: ["ih", "ou"], W: ["aa", "ou"], Y: ["oh", "ih"] };   // diphthongs move between two shapes
  const VOWELS = new Set(["aa", "E", "ih", "oh", "ou"]), SHUT = { PP: 4, FF: 3, TH: 2, CH: 2 };   // lips that must really close or touch win over their neighbours
  const SKIP = new Set(["ˈ", "ˌ", "h", "ʔ"]), KERNEL = Array.from({ length: 2 * HALF + 1 }, (_, i) => .5 + .5 * Math.cos((i - HALF) / (HALF + 1) * Math.PI)), KSUM = KERNEL.reduce((a, b) => a + b);
  function lipCurves(lip) { // returns { n, c: one Float32Array per viseme }; weights at any moment sum to at most 1
    const ph = Array.from(lip.p), d = lip.d, segs = []; let at = d[0] * FRAME, carry = 0, stressed = false;
    const add = (dur, v, amt) => { segs.push({ t0: at - carry, t1: at + dur, v, amt }); carry = 0; at += dur; };
    ph.forEach((c, i) => { const dur = d[i + 1] * FRAME;
      if (c === "ˈ") stressed = true;
      if (SKIP.has(c)) { carry += dur; at += dur; return; }                                     // "h" and stress marks take the next sound's shape
      if (c === "ː" && segs.length) { segs[segs.length - 1].t1 += dur; at += dur; return; }
      if (c === " " && dur <= .075) { if (segs.length) segs[segs.length - 1].t1 += dur / 2; at += dur / 2; carry = dur / 2; at += dur / 2; return; }   // a word gap, not a pause
      const full = stressed ? 1 : .82;
      if (GLIDE[c]) { stressed = false; add(dur * .55, GLIDE[c][0], full); add(dur * .45, GLIDE[c][1], full); }
      else if (c === "ə" || c === "ᵊ") { stressed = false; add(dur, "ih", .4); }                // a schwa barely shapes the mouth
      else if (SHAPE[c]) { const v = SHAPE[c]; if (VOWELS.has(v)) stressed = false; add(dur, v, VOWELS.has(v) ? full : 1); }
      else { stressed = false; carry = 0; at += dur; }                                          // punctuation and long gaps: mouth at rest
    });
    const n = Math.ceil(d.reduce((a, b) => a + b, 0) * FRAME / STEP) + 1, raw = VIS.map(() => new Float32Array(n));
    segs.forEach(s => { const r = raw[VI[s.v]]; for (let i = Math.max(0, Math.round(s.t0 / STEP)), e = Math.min(n, Math.round(s.t1 / STEP)); i < e; i++) r[i] = Math.max(r[i], s.amt); });
    const smooth = raw.map(r => { const o = new Float32Array(n); for (let i = 0; i < n; i++) { let a = 0; for (let k = -HALF; k <= HALF; k++) { const j = i + k; if (j >= 0 && j < n) a += r[j] * KERNEL[k + HALF]; } o[i] = a / KSUM; } return o; });
    const c = VIS.map(() => new Float32Array(n));
    for (let i = 0; i < n; i++) { let total = 0, used = 0; VIS.forEach((v, j) => { used += smooth[j][i]; total += smooth[j][i] * (SHUT[v] || 1); }); total += Math.max(0, 1 - used); if (total > 0) VIS.forEach((v, j) => { c[j][i] = smooth[j][i] * (SHUT[v] || 1) / total; }); }
    return { n, c };
  }
  const NAMES = VIS.map(v => "viseme_" + v), outW = Object.fromEntries(NAMES.map(k => [k, 0]));
  function visemes() { // current weights keyed by morph name, or null when timing isn't available and the caller should use mouth()
    if (!cur || !cur.lip.length) return null;
    const now = ctx.currentTime - (ctx.outputLatency || 0) + LIPS_LEAD; let on = null; for (const l of cur.lip) if (l.start <= now) on = l;
    if (on && !on.curves) return null;
    const x = on ? ((now - on.start) * on.rate + AUDIO_AHEAD) / STEP : -1, i = Math.floor(x), f = x - i, live = on && i >= 0 && i + 1 < on.curves.n;
    NAMES.forEach((k, j) => { outW[k] = live ? on.curves.c[j][i] * (1 - f) + on.curves.c[j][i + 1] * f : 0; }); return outW;
  }
  function spoken() { // which sentence is sounding and how far through it, for lighting the caption
    if (!cur || !cur.lip.length) return null; const now = ctx.currentTime - (ctx.outputLatency || 0); let i = 0; cur.lip.forEach((l, k) => { if (l.start <= now) i = k; }); const l = cur.lip[i]; return { i, n: cur.lip.length, f: Math.max(0, Math.min(1, (now - l.start) / l.dur)) };
  }
  function impulse(c, decay) {
    const sr = c.sampleRate, n = Math.floor(sr * decay * 1.15), ir = c.createBuffer(2, n, sr), pre = Math.floor(sr * .045), k = Math.log(1000) / decay;
    for (let ch = 0; ch < 2; ch++) { const d = ir.getChannelData(ch); let lp = 0, peak = 0;
      for (let i = pre; i < n; i++) { const t = (i - pre) / sr, a = Math.max(.05, .55 * Math.exp(-t * 1.1)); lp += a * ((Math.random() * 2 - 1) - lp); d[i] = lp * Math.exp(-k * t); peak = Math.max(peak, Math.abs(d[i])); }
      [[.051, .7], [.073, .55], [.109, .45], [.151, .35], [.197, .3], [.263, .24]].forEach(([s, g], j) => { d[Math.floor(sr * (s + ch * .004 * (j + 1)))] += g * peak * (j % 2 ? -1 : 1); });
      let e = 0; for (let i = 0; i < n; i++) e += d[i] * d[i]; const norm = 1 / Math.sqrt(e); for (let i = 0; i < n; i++) d[i] *= norm; }
    return ir;
  }
  function wire(c) { // input -> dry + hall -> limiter -> out
    const i = c.createGain(), dg = c.createGain(), wg = c.createGain(), cv = c.createConvolver(), m = c.createDynamicsCompressor(), pre = c.createDelay(.5), hole = c.createGain();
    cv.normalize = false; cv.buffer = impulse(c, S.decay); dg.gain.value = S.dry; wg.gain.value = S.wet * 1.6; pre.delayTime.value = .09;
    m.threshold.value = -10; m.ratio.value = 8; m.attack.value = .004; m.release.value = .25;
    i.connect(dg); i.connect(pre); pre.connect(cv); cv.connect(hole); hole.connect(wg); dg.connect(m); wg.connect(m); m.connect(c.destination);
    // Ducking: the voice's own envelope pulls the hall down while a word is sounding, so the tail swells in the gaps instead of smearing the words.
    const curve = f => { const a = new Float32Array(1025); for (let k = 0; k < 1025; k++) a[k] = f(k / 512 - 1); return a; };
    const rect = c.createWaveShaper(); rect.curve = curve(Math.abs); const lp = c.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = 4; lp.Q.value = .3;
    const drive = c.createGain(); drive.gain.value = 14; const sat = c.createWaveShaper(); sat.curve = curve(x => 1 - Math.exp(-3 * Math.max(0, x))); const dk = c.createGain(); dk.gain.value = -S.duck;
    hole.gain.value = 1; i.connect(rect); rect.connect(lp); lp.connect(drive); drive.connect(sat); sat.connect(dk); dk.connect(hole.gain);
    return { i, dg, wg, cv, m, dk };
  }
  function unlock() { if (!ctx) { ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 }); const g = wire(ctx); input = g.i; dryG = g.dg; wetG = g.wg; conv = g.cv; master = g.m; duckG = g.dk; an = ctx.createAnalyser(); an.fftSize = 1024; an.smoothingTimeConstant = .35; input.connect(an); td = new Float32Array(an.fftSize); fd = new Uint8Array(an.frequencyBinCount); } if (ctx.state === "suspended") ctx.resume(); return ctx; }
  function set(next) { S = Object.assign(S, next); try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} if (ctx) { dryG.gain.value = S.dry; wetG.gain.value = S.wet * 1.6; duckG.gain.value = -S.duck; if ("decay" in next) conv.buffer = impulse(ctx, S.decay); } }
  async function* frames(text, signal) {
    const t0 = performance.now();
    const res = await fetch(BASE + "/speak", { method: "POST", credentials: "omit", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, mix: S.mix, speed: S.speed, lang: S.lang }), signal });
    if (!res.ok) throw new Error("speech server said " + res.status);
    const rd = res.body.getReader(); let buf = new Uint8Array(0), first = true;
    for (;;) { const { value, done } = await rd.read(); if (done) break; const b = new Uint8Array(buf.length + value.length); b.set(buf); b.set(value, buf.length); buf = b;
      while (buf.length >= 8) { const dv = new DataView(buf.buffer, buf.byteOffset), n = dv.getUint32(0, true), m = dv.getUint32(4, true), need = 8 + m + n * 4; if (buf.length < need) break;
        let lip = null; if (m) try { lip = JSON.parse(new TextDecoder().decode(buf.subarray(8, 8 + m))); } catch (e) {}
        const pcm = new Float32Array(n); new Uint8Array(pcm.buffer).set(buf.subarray(8 + m, need)); buf = buf.slice(need); if (first) { stats.firstAudioMs = Math.round(performance.now() - t0); first = false; } yield { pcm, lip }; } }
    stats.totalMs = Math.round(performance.now() - t0);
  }
  function stop() { if (!cur) return; const c = cur; cur = null; c.abort.abort(); c.src.forEach(s => { try { s.stop(); } catch (e) {} }); clearTimeout(c.timer); }
  async function say(text, cb = {}) {
    unlock(); stop(); const me = cur = { abort: new AbortController(), src: [], lip: [], timer: 0 }; stats = {}; let t = 0, audio = 0;
    try { for await (const { pcm, lip } of frames(text, me.abort.signal)) { if (cur !== me) return;
        const b = ctx.createBuffer(1, pcm.length, 24000); b.copyToChannel(pcm, 0); const s = ctx.createBufferSource(); s.buffer = b; s.playbackRate.value = S.rate; s.connect(input);
        if (!t) { t = ctx.currentTime + .03; cb.onstart && cb.onstart(stats); } t = Math.max(t, ctx.currentTime + .01); s.start(t); me.src.push(s); me.lip.push({ start: t, rate: S.rate, dur: b.duration / S.rate, curves: lip ? lipCurves(lip) : null }); t += b.duration / S.rate + S.gap; audio += b.duration / S.rate; }
      if (cur !== me) return; stats.audioSec = +audio.toFixed(2); stats.speedVsRealtime = +(audio / (stats.totalMs / 1000)).toFixed(1);
      me.timer = setTimeout(() => { if (cur === me) { cur = null; cb.onend && cb.onend(stats); } }, Math.max(0, (t - S.gap - ctx.currentTime) * 1000));
    } catch (e) { if (e.name !== "AbortError" && cur === me) { cur = null; cb.onerror && cb.onerror(e); } }
  }
  function mouth() { // how open and how rounded, from the voice itself before the reverb
    if (!an) return { open: 0, round: 0 }; an.getFloatTimeDomainData(td); let e = 0; for (const v of td) e += v * v; const rms = Math.sqrt(e / td.length); an.getByteFrequencyData(fd);
    let lo = 0, hi = 0; for (let i = 2; i < 24; i++) lo += fd[i]; for (let i = 24; i < 120; i++) hi += fd[i];
    return { open: Math.min(1, Math.max(0, (rms - .006) * 7.5)), round: lo / (lo + hi + 1) };
  }
  // Same chain without a speaker, so a test can capture what the listener would hear.
  async function renderOffline(text, tail = 3) {
    const parts = []; for await (const f of frames(text)) parts.push(f.pcm); let len = 0; parts.forEach(p => len += p.length / 24000 / S.rate + S.gap);
    const c = new OfflineAudioContext(2, Math.ceil((len + tail) * 48000), 48000), g = wire(c); let t = .35;
    parts.forEach(p => { const b = c.createBuffer(1, p.length, 24000); b.copyToChannel(p, 0); const s = c.createBufferSource(); s.buffer = b; s.playbackRate.value = S.rate; s.connect(g.i); s.start(t); t += b.duration / S.rate + S.gap; });
    const r = await c.startRendering(); return { sr: 48000, l: (() => { const d = r.getChannelData(0), e = r.getChannelData(1), o = new Array(Math.floor(d.length / 3)); for (let i = 0; i < o.length; i++) { let a = 0; for (let j = 0; j < 3; j++) a += d[3 * i + j] + e[3 * i + j]; o[i] = a / 6; } return o; })(), speechEnd: t - S.gap, stats: Object.assign({}, stats) };
  }
  return { say, stop, unlock, set, mouth, visemes, spoken, lipCurves, renderOffline, watch, check, available: () => up, get settings() { return S; }, get stats() { return stats; }, get speaking() { return !!cur; }, DEF };
})();
