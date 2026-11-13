// The Thoughtfield's Horizon interface (doomtickle/passdesk-ops#207). LOIS rises behind a dark world whose lit edge is the day.
// The main page only displays: it polls /api/state and /api/field, draws with textContent, and frames the two pages that can
// act (the control unit at /cu/field and Terminal 0 at /term/field), which it can't script. It may also POST fixed
// text to the optional local voice: fixed event sentences and the lines the Coordinator gives its say tool (state.said, doomtickle/passdesk-ops#216).
// Nothing else an agent wrote is spoken: task and branch labels stay in the detail line.
"use strict";
(function () {
  const $ = id => document.getElementById(id), LF = window.LF, W = LF.W, NS = "http://www.w3.org/2000/svg";
  const LANE = /^[a-z0-9-]{1,40}$/, JOB = /^[0-9A-Za-z-]{1,64}$/, CARD = /^[A-Za-z0-9_-]{1,64}$/;
  const nowMin = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60; };
  const hhmm = m => String(Math.floor(m / 60) % 24).padStart(2, "0") + ":" + String(Math.floor(m % 60)).padStart(2, "0");
  const svg = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
  let state = null, field = null, Sky = null, unlocked = false, greeted = false;

  // ---------- the world: one big circle whose lit edge is the day
  const G = { t0: 9 * 60, t1: 16 * 60 }, world = $("world"), atmos = $("atmos");
  const rimAt = x => G.cy - Math.sqrt(Math.max(0, G.R * G.R - (x - G.cx) ** 2));
  const dayX = t => G.w * .04 + t / (G.t1 - G.t0) * G.w * .92;
  const onRim = (x, depth = 0) => { const a = Math.asin(Math.max(-1, Math.min(1, (x - G.cx) / G.R))), r = G.R - depth; return { x: G.cx + r * Math.sin(a), y: G.cy - r * Math.cos(a), deg: a * 180 / Math.PI }; };
  W.skyBlocked = (x, y) => G.R && y > rimAt(x) - 4; W.geo = () => G; W.rimAt = rimAt; W.dayX = dayX; W.dayT0 = () => G.t0;
  function dayWindow() { const n = nowMin(), first = Sky && Sky.firstStart(), t0 = Math.floor(Math.min(first == null ? n : first, n - 60) / 60) * 60, t1 = Math.max(t0 + 420, Math.ceil((n + 60) / 60) * 60); return [Math.max(0, t0), Math.min(1440, t1)]; }
  function layout() {
    const w = G.w = innerWidth, h = G.h = innerHeight, narrow = w <= 1040; [G.t0, G.t1] = dayWindow(); G.rim = Math.round(h * (narrow ? .52 : .565)); if (termFrame) G.rim = Math.round(Math.min(G.rim, Math.max(h * .3, h - paneS(h, w) - below()))); G.sag = Math.min(h * .13, w * .105); G.R = (w * w / 4 + G.sag * G.sag) / (2 * G.sag); G.cx = w / 2; G.cy = G.rim + G.R;
    document.documentElement.style.setProperty("--rim", G.rim + "px");
    const k = termFrame ? Math.max(.22, Math.min(.41, G.rim / h - .17)) : .41; LF.faceK = k; if (LF.frameFace && k !== G.faceK) { G.faceK = k; LF.frameFace(k); }   // her chin about .12 of the height above the horizon, room for her shoulders
    placeKey = ""; ords.forEach(el => { el._wd = el.offsetWidth; el._ht = el.offsetHeight; });
    const y0 = rimAt(0), arc = `M0 ${y0} A${G.R} ${G.R} 0 0 1 ${w} ${y0}`;
    [atmos, world, $("threads")].forEach(s => s.setAttribute("viewBox", `0 0 ${w} ${h}`));
    atmos.textContent = ""; const defs = svg("defs", {}); [["b1", 34], ["b2", 7]].forEach(([id, sd]) => { const f = svg("filter", { id, filterUnits: "userSpaceOnUse", x: 0, y: 0, width: w, height: h }); f.append(svg("feGaussianBlur", { stdDeviation: sd })); defs.append(f); }); atmos.append(defs, svg("path", { d: arc, fill: "none", stroke: "#2470e6", "stroke-width": 46, filter: "url(#b1)", opacity: ".62" }), svg("path", { d: arc, fill: "none", stroke: "#6fd4ff", "stroke-width": 9, filter: "url(#b2)", opacity: ".75" }));
    const base = $("world-base"), top = $("world-top"); base.textContent = ""; top.textContent = "";
    const wd = svg("defs", {}), lg = svg("linearGradient", { id: "surf", x1: 0, y1: G.rim, x2: 0, y2: h, gradientUnits: "userSpaceOnUse" }); [[0, "#071226", .95], [.28, "#040a15", .985], [1, "#020409", 1]].forEach(([o, c, a]) => lg.append(svg("stop", { offset: o, "stop-color": c, "stop-opacity": a }))); const rg = svg("radialGradient", { id: "sunG" }); [[0, "#d9f5ff", .95], [.2, "#7fe0ff", .55], [1, "#7fe0ff", 0]].forEach(([o, c, a]) => rg.append(svg("stop", { offset: o, "stop-color": c, "stop-opacity": a }))); wd.append(lg, rg); base.append(wd);
    base.append(svg("circle", { id: "nowglow", r: 62, fill: "url(#sunG)" }), svg("path", { d: `${arc} L${w} ${h} L0 ${h} Z`, fill: "url(#surf)" }));   // the glow goes under the world, so "now" rises over the edge like a sun
    [84, 180, 300].forEach(d => { const r = G.R - d, yy = G.cy - Math.sqrt(Math.max(0, r * r - w * w / 4)); base.append(svg("path", { d: `M0 ${yy} A${r} ${r} 0 0 1 ${w} ${yy}`, fill: "none", stroke: "#9fc8ff", "stroke-opacity": ".055" })); });
    base.append(svg("path", { d: arc, fill: "none", stroke: "#a9dcff", "stroke-opacity": ".5", "stroke-width": 1 }), svg("path", { id: "past", fill: "none", stroke: "#e9f8ff", "stroke-width": 2 }));
    const sc = svg("path", { id: "scrub", d: arc, fill: "none", stroke: "transparent", "stroke-width": 30 }); const st = svg("title", {}); st.textContent = "Drag to rewind the day"; sc.append(st); base.append(sc);
    for (let m = Math.ceil(G.t0 / 60) * 60; m <= G.t1; m += 60) { const p = onRim(dayX(m - G.t0)), t = onRim(p.x, 19), a = onRim(p.x, 5); base.append(svg("line", { class: "tick", x1: p.x, y1: p.y, x2: a.x, y2: a.y })); const tx = svg("text", { class: "hr", "data-m": m, x: t.x, y: t.y, transform: `rotate(${t.deg} ${t.x} ${t.y})` }); tx.textContent = hhmm(m); base.append(tx); }
    const nl = svg("text", { id: "nowl" }); nl.textContent = "now"; const sl = svg("text", { id: "scrubl", visibility: "hidden" }); top.append(svg("circle", { id: "nowdot", r: 4.2, fill: "#fff" }), nl, svg("circle", { id: "scrubdot", r: 6.5, fill: "#fff", visibility: "hidden" }), sl);
    const tb = document.querySelector(".top"), tr = tb && tb.getBoundingClientRect(); topY = Math.max(52, tr && tr.height ? tr.bottom + 4 : 52); placeKey = "";
    G.nowX = -1; hug(); G.dirty = true; beadKey = ""; drawBeads(); tickDay(); if (Sky) Sky.rescale(); }
  function tickDay() { const n = nowMin(), [a, b] = dayWindow(); if (a !== G.t0 || b !== G.t1) { layout(); return; } const X = dayX(n - G.t0); if (Math.abs(X - G.nowX) < .5) return; G.nowX = X; const p = onRim(X), st = onRim(dayX(0)), pa = $("past"); if (!pa) return;
    pa.setAttribute("d", `M${st.x} ${st.y} A${G.R} ${G.R} 0 0 1 ${p.x} ${p.y}`); const lb = onRim(X, 19), l = $("nowl"); ["nowglow", "nowdot"].forEach(id => { $(id).setAttribute("cx", p.x); $(id).setAttribute("cy", p.y); }); l.setAttribute("x", lb.x); l.setAttribute("y", lb.y); l.setAttribute("transform", `rotate(${lb.deg} ${lb.x} ${lb.y})`);
    world.querySelectorAll(".hr").forEach(t => t.classList.toggle("dim", Math.abs(dayX(+t.dataset.m - G.t0) - X) < 26)); }
  // beads: the day's events. Every word is drawn with textContent; the class comes from this fixed table.
  const BEAD = Object.assign(Object.create(null), { JOB: "k-ask", OK: "k-ok", NO: "k-rev", WRITTEN: "k-out", STAGED: "k-hold", DENY: "k-bad", FAILED: "k-bad" }); let beadKey = "", marks = [];
  const parseT = t => { const m = /^(\d{1,2}):(\d{2})/.exec(String(t || "")); return m ? +m[1] * 60 + +m[2] : null; };
  function drawBeads() { const list = ((state && state.events) || []).filter(e => BEAD[e.kind]).slice(-60), key = list.length + "|" + (list.length ? list[list.length - 1].t + list[list.length - 1].kind : "") + "|" + G.w; if (key === beadKey || !G.R) return; beadKey = key; const g = $("beads"); g.textContent = ""; list.forEach(e => { const t = parseT(e.t); if (t == null || t < G.t0 || t > G.t1) return; const X = dayX(t - G.t0), p = onRim(X), lb = onRim(X, 35), r = e.kind === "STAGED" ? 5.5 : 3.6, c = svg("circle", { class: "ev " + BEAD[e.kind] + (e.red && e.kind === "JOB" ? " k-bad" : ""), cx: p.x, cy: p.y, r }); const hit = svg("circle", { class: "hit", cx: p.x, cy: p.y, r: 11 }); hit._ev = e; hit._lbl = svg("text", { class: "lbl", x: lb.x, y: lb.y, "text-anchor": X < 90 ? "start" : X > G.w - 90 ? "end" : "middle", transform: `rotate(${lb.deg} ${lb.x} ${lb.y})` }); hit._lbl.textContent = e.t.slice(0, 5) + "  " + e.text.slice(0, 80); g.append(c, hit, hit._lbl); }); }
  world.addEventListener("pointerover", e => { world.querySelectorAll(".lbl.near").forEach(n => n.classList.remove("near")); if (e.target._lbl) e.target._lbl.classList.add("near"); });
  world.addEventListener("pointerout", e => { if (e.target._lbl) e.target._lbl.classList.remove("near"); });
  // With Terminal 0 docked, the room the caption, the decision row and the chips need between the horizon and the pane (field.css: body.has-term .mid).
  const paneS = (h, w) => h * (w <= 760 ? .38 : .31);   // --pane-s
  function below() { const px = (el, p) => parseFloat(getComputedStyle(el)[p]) || 0, txt = $("txt"), chips = $("chips");
    return (innerWidth <= 1040 ? 50 : 34) + 2 * px(txt, "fontSize") * 1.35 + $("meta").offsetHeight + px($("meta"), "marginTop") + (parseFloat(getComputedStyle(document.body).getPropertyValue("--dh")) || 92) + chips.offsetHeight + px(chips, "marginTop") + 14; }
  function hug() { const mid = document.querySelector(".mid").getBoundingClientRect(); [["#jobs-list > *, .work h2", -1], [".today > *", 1]].forEach(([q, side]) => document.querySelectorAll(q).forEach(el => { el.style.transform = ""; const r = el.getBoundingClientRect(); if (!r.height) return; const rr = G.R - 34, half = Math.sqrt(Math.max(0, rr * rr - (G.cy - r.top) ** 2)), need = Math.min(side < 0 ? (G.cx - half) - r.left : r.right - (G.cx + half), side < 0 ? mid.left - 12 - r.right : r.left - mid.right - 12); if (need > 0) el.style.transform = `translateX(${side < 0 ? need : -need}px)`; })); }

  // ---------- what LOIS says: these fixed sentences, whose names go in the detail line as text, and the Coordinator's own lines (sayOwn).
  const txt = $("txt"), meta = $("meta"), queue = []; let cur = null, capWords = [], capSent = [], capLen = 0, capT0 = 0, capAll = false, tight = null, on = true;
  function setCaption(text) { txt.textContent = ""; txt.classList.toggle("long", text.length > 110); capWords = []; capAll = false; capT0 = performance.now(); let off = 0; capSent = text.split(/(?<=[.!?])\s+/).map(c => { const r = { start: off, len: c.length }; off += c.length + 1; return r; }); capLen = off; let pos = 0; text.split(/\s+/).forEach(w => { const el = document.createElement("span"); el.className = "w" + (/\d\d:\d\d/.test(w) ? " t" : ""); el.textContent = w; txt.append(el, " "); capWords.push({ el, at: pos, on: false }); pos += w.length + 1; }); tight = null; txt.classList.remove("in"); void txt.offsetWidth; txt.classList.add("in"); }
  function say(text, m, opt = {}) { const fixed = queue.filter(q => !q.own); fixed.slice(0, Math.max(0, fixed.length - 2)).forEach(q => queue.splice(queue.indexOf(q), 1)); queue.push(Object.assign({ text, m }, opt)); if (!cur) next(); }
  // Its own words go ahead of any fixed sentence still waiting, in the order it said them. A fixed sentence already being spoken finishes first.
  function sayOwn(text) { ownAt = performance.now(); lensArmed = false; W.s.flash = Math.max(W.s.flash, .8); stopThinking(); clearTimeout(outTimer); shimmer(false); const own = queue.filter(q => q.own).length; queue.splice(own, 0, { text, m: "", own: true, ans: true, mood: "calm", fx: () => { W.focus = null; } }); if (own >= OWN_WAITING) queue.shift(); if (!cur) next(); }
  // Between your line and its first words the face is thinking, and only that: there is no spoken "one moment", which would be a messenger.
  let lensArmed = false, hadFocus = false;
  const LISTEN_MS = 12000, OUT_MS = 5000, STILL_MS = 45000, GIVE_UP_MS = 180000, OWN_WAITING = 4, OWN_QUIETS_START_MS = 30000; let ownAt = -Infinity, thinkTimers = [];
  const thinkLine = t => { thinkNote = t; if (!cur) { setCaption(""); meta.textContent = t; } };
  let thinkNote = "", listenTimer = 0, outTimer = 0;
  // Terminal 0 tells this page three fixed words (t0:enter, t0:out, t0:still) and nothing else: never text, a count or a size. They set the mood and nothing acts on them.
  // Enter: the ring and the "Thinking…" caption, and the stars draw gently inward ("listening"; W.listenT, eased in field-sky.js). While output keeps arriving a faint shimmer
  // runs along the pane's rim and the ring's time-out is pushed back. Still: the stars ease back and the shimmer stops; the ring runs on until LOIS says something (sayOwn)
  // or its time-out. Both effects have their own fallback, in case the frame never says still. Reduced motion: no shimmer here, no drift in field-sky.js, a steady ring.
  const reducedMotion = () => !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  const shimmer = on => $("term-panel").classList.toggle("out", on && !reducedMotion());
  function holdListen() { W.listenT = 1; clearTimeout(listenTimer); listenTimer = setTimeout(() => { W.listenT = 0; }, LISTEN_MS); }
  function t0(word) {
    if (word === "enter") { startThinking(); return; }
    if (word === "still") { W.listenT = 0; clearTimeout(listenTimer); clearTimeout(outTimer); shimmer(false); return; }
    shimmer(true); clearTimeout(outTimer); outTimer = setTimeout(() => shimmer(false), OUT_MS);
    if (W.thinking) { holdListen(); clearTimeout(thinkTimers[1]); thinkTimers[1] = setTimeout(giveUp, GIVE_UP_MS); } }
  function giveUp() { stopThinking(); if (!cur) meta.textContent = ""; }
  function stopThinking() { W.thinking = false; W.listenT = 0; clearTimeout(listenTimer); thinkNote = ""; thinkTimers.forEach(clearTimeout); thinkTimers = []; }
  // Browsers keep sound shut until a click or key; until then LOIS shows captions only, and speaks from the first gesture on.
  const talks = () => on && unlocked && window.LiveVoice && LiveVoice.available();
  function unlock() { if (unlocked || (navigator.userActivation && !navigator.userActivation.hasBeenActive)) return; unlocked = true; try { LF.audio(); if (window.LiveVoice) LiveVoice.unlock(); } catch (e) {} ["pointerdown", "pointerup", "keydown"].forEach(t => removeEventListener(t, unlock)); }
  ["pointerdown", "pointerup", "keydown"].forEach(t => addEventListener(t, unlock));
  LF.unlocked = () => unlocked;
  function startThinking() { stopThinking(); queue.splice(0, queue.length, ...queue.filter(q => !q.own)); if (cur && window.LiveVoice) LiveVoice.stop(); if (cur) { cur = null; W.speaking = false; W.mood = "calm"; } W.focus = null; W.thinking = true; holdListen(); thinkLine("Thinking…"); thinkTimers = [setTimeout(() => thinkLine("Still thinking. My working is below."), STILL_MS), setTimeout(giveUp, GIVE_UP_MS)]; if (queue.length) next(); }
  function next() { const it = queue.shift(); if (!it) { cur = null; if (thinkNote) thinkLine(thinkNote); return; } cur = it;  setCaption(it.text); meta.textContent = it.m || ""; marks = it.marks || []; if (it.fx) it.fx(); W.speaking = true; W.mood = it.mood || (/failed|Declined|changes|blocked/i.test(it.text) ? "stern" : /^Hi|Approved|approved/i.test(it.text) ? "lit" : "calm");
    const done = () => { if (cur !== it) return; capWords.forEach(w => { w.on = true; w.el.classList.add("on"); }); capAll = true; W.speaking = false; W.mood = "calm"; setTimeout(() => { if (!cur || cur === it) W.focus = null; }, 1500); setTimeout(() => { if (cur === it) next(); }, 900); };
    it.finish = done; const quiet = () => setTimeout(done, 1500 + it.text.length * 60);
    if (talks()) LiveVoice.say(it.text, { onend: done, onerror: quiet }); else quiet(); }
  function answer(text, m) { queue.unshift({ text, m, ans: true, fx: () => { W.focus = null; } }); if (cur) { if (window.LiveVoice) LiveVoice.stop(); cur = null; W.speaking = false; } next(); }
  function litCaption(now) { if (!capWords.length || capAll) return; let idx; const sp = talks() ? LiveVoice.spoken() : null; if (sp) { if (sp.n === capSent.length) { const c = capSent[Math.min(sp.i, sp.n - 1)]; idx = c.start + sp.f * c.len; } else idx = (sp.i + sp.f) / sp.n * capLen; } else idx = now - capT0 > 1500 || !on ? (now - capT0) / (1500 + capLen * 60) * capLen : -1; capWords.forEach(w => { if (!w.on && w.at <= idx + 1) { w.on = true; w.el.classList.add("on"); } }); }
  const laneWords = n => LANE.test(n) ? n.replace(/-lane(\d+)$/, " lane $1").replace(/-/g, " ") : "a lane";
  const modelWord = m => { const k = /^(opus|sonnet|haiku|fable)/.exec(String(m || "")); return k ? k[1][0].toUpperCase() + k[1].slice(1) : ""; };
  const CARD_NOUN = new Map([["git_push", "A push"], ["github_create_pr", "A pull request"], ["gitlab_create_mr", "A merge request"]]), cardNoun = c => CARD_NOUN.get(c.tool) || "A write";
  const prev = { lanes: new Map(), cards: new Set(), events: new Set(), said: new Set(), denyAt: 0, primed: false };
  const own = (map, k) => (Object.prototype.hasOwnProperty.call(map, k) ? map[k] : undefined);   // these keys come from files an agent's run can touch
  const evKey = e => e.t + "|" + e.kind + "|" + e.text;
  function narrate() { const s = state; const cards = s.gate || [];
    if (!prev.primed) { prev.primed = true; (s.lanes || []).forEach(l => prev.lanes.set(l.name, [l.job, l.state])); cards.forEach(c => prev.cards.add(c.id)); (s.events || []).forEach(e => prev.events.add(evKey(e))); (s.said || []).forEach(l => prev.said.add(l.id)); return; }
    (s.said || []).forEach(l => { if (prev.said.has(l.id)) return; prev.said.add(l.id); if (typeof l.text === "string" && l.text) sayOwn(l.text); });
    (s.lanes || []).forEach(l => { const was = prev.lanes.get(l.name) || [null, "idle"], nm = laneWords(l.name), det = String(l.desk || l.branch || "").slice(0, 80);
      if (Sky && Sky.comet && typeof l.job === "string" && JOB.test(l.job)) { if (l.state === "running" && l.job !== was[0]) Sky.comet("out", l.job); else if (l.job === was[0] && was[1] === "running" && l.state !== "running") Sky.comet("back", l.job, l.state); }
      if (l.job && l.job !== was[0] && l.state === "running") { const mw = modelWord(l.model); if (performance.now() - ownAt > OWN_QUIETS_START_MS) say(l.role === "reviewer" ? `A review is starting on ${nm}.` : `A new job is starting on ${nm}.` + (mw ? ` It's running on ${mw}.` : ""), det, { fx: () => { W.focus = Sky && Sky.jobMesh(l.job); } }); }
      else if (l.job && l.job === was[0] && was[1] === "running" && l.state !== "running") { const cost = l.cost != null ? " · $" + l.cost.toFixed(2) : ""; const lines = { approved: "The reviewer approved the work.", revise: "The reviewer asked for changes.", failed: `A job failed on ${nm}.`, cancelled: `A job was cancelled on ${nm}.`, interrupted: `A job was interrupted on ${nm}.` }; say(own(lines, l.state) || `The job on ${nm} is done.`, det + cost); }
      prev.lanes.set(l.name, [l.job, l.state]); });
    cards.forEach(c => { if (prev.cards.has(c.id) || !CARD.test(c.id)) return; prev.cards.add(c.id); if (!c.decided) { LF.chime && LF.chime(); say(`${cardNoun(c)} is waiting for you. Its crystal is gold until you decide.`, [c.repo, c.branch].filter(Boolean).join(" · ").slice(0, 120), { fx: () => { W.focus = Sky && Sky.goldMesh(c.id); } }); } });
    const ev = s.events || [], nowSeen = new Set(); ev.forEach(e => { const k = evKey(e); nowSeen.add(k); if (prev.events.has(k)) return; if (e.kind === "WRITTEN") say("The write went out.", ""); else if (e.kind === "FAILED") say("A write failed. The reason is on its mark on the horizon.", ""); else if (e.kind === "DENY" && performance.now() - prev.denyAt > 120000) { prev.denyAt = performance.now(); say("The gateway blocked a request.", ""); } }); prev.events = nowSeen; }
  W.onCardGone = (c, yes) => { prev.cards.delete(c.id); if (c.decided === "approve") say("Approved. It goes out within seconds.", ""); else if (c.decided) say("Declined. The agent will read your reason.", ""); else if (c.seconds_left != null && c.seconds_left <= 5) say("A held write expired without a decision.", ""); else say("A held write was settled.", ""); };
  W.onPickJob = j => { if (LANE.test(j.lane) && JOB.test(j.id) && window.FieldMonitor) FieldMonitor.open(j.lane, j.id); };
  W.onPickCard = c => selectCard(c.id);
  function greet() { const s = state || {}, n = (s.totals || {}).running || 0, m = (s.gate || []).length; const parts = n ? `${n === 1 ? "One job is" : n + " jobs are"} running` : "Nothing is running"; const wait = m ? ` and ${m === 1 ? "one is" : m + " are"} waiting for you` : ""; say(`Hi. ${parts}${wait}.`, !n && s.prompt ? "Try: " + String(s.prompt).slice(0, 160) : (s.totals ? `Today so far: ${s.totals.jobs} jobs · spend $${s.totals.cost.toFixed(2)}` : ""), { fx: () => { W.focus = null; } }); }

  // ---------- the frames that can act. The main page can't script them; they're other origins.
  // Only Terminal 0 may use the microphone (talking: Terminal 0's strip): what is said stays in that frame, as what is typed does.
  let cuFrame = null, cuOrigin = "", termFrame = null, termOrigin = "";
  function frame(cls, title, src, allow) { const f = document.createElement("iframe"); f.className = cls; f.title = title; f.setAttribute("sandbox", "allow-scripts allow-same-origin"); f.setAttribute("referrerpolicy", "no-referrer"); if (allow) f.setAttribute("allow", allow); f.src = src; return f; }
  function ensureFrames() { const s = state; if (s.cu && !cuFrame) { cuOrigin = `http://${location.hostname}:${Number(s.cu.port)}`; cuFrame = frame("cu-frame", "Control unit", `${cuOrigin}/cu/field`); cuFrame.addEventListener("load", () => { setCu(false); $("settings-key").disabled = false; }); $("cu-wrap").append(cuFrame); }
    if (s.term && !termFrame) { termOrigin = `http://${location.hostname}:${Number(s.term.port)}`; termFrame = frame("term-frame", "LOIS's terminal", `${termOrigin}/term/field`, "clipboard-write; microphone"); termFrame.addEventListener("load", () => { hushSent = null; }); $("term-slot").append(termFrame); $("term-key").hidden = false; $("term-panel").hidden = false; document.body.classList.add("has-term"); layout(); } }
  // Talking's only signals, fixed words to the terminal's exact origin: "tm:hush" while LOIS's voice plays (again every 2 s, so a lost
  // "tm:open" can't keep the microphone deaf: the frame lets a hush go stale after 5 s), "tm:open" when it stops, "tm:cancel" on Escape.
  let hushSent = null, hushAt = 0;
  const tell = w => { if (termFrame && termFrame.contentWindow) termFrame.contentWindow.postMessage(w, termOrigin); };
  setInterval(() => { const sp = !!(window.LiveVoice && LiveVoice.speaking), now = Date.now(); if (sp !== hushSent || (sp && now - hushAt > 2000)) { hushSent = sp; hushAt = now; tell(sp ? "tm:hush" : "tm:open"); } }, 150);
  function setCu(big) { $("cu-wrap").classList.toggle("expanded", big); }
  function selectCard(id) { if (cuFrame && CARD.test(id)) cuFrame.contentWindow.postMessage({ type: "cu:select", id }, cuOrigin); }
  // The pane is always there; "grown" is the owner's own click into it, T, or the Bigger key. Only those ever focus the terminal frame.
  let grown = false;
  function setGrown(on, focus) { if (!termFrame) return; grown = on; $("term-panel").classList.toggle("grown", on); $("term-key").setAttribute("aria-expanded", String(on)); $("term-size").textContent = on ? "Smaller" : "Bigger"; if (on && focus) termFrame.focus(); else if (!on && document.activeElement === termFrame) termFrame.blur(); }
  // Clicking into the frame moves focus there without a message to this page; the window's blur is how this page hears it.
  addEventListener("blur", () => setTimeout(() => { if (!termFrame) return; if (document.activeElement === termFrame) { unlock(); setGrown(true); } else if (cuFrame && document.activeElement === cuFrame) setGrown(false); }, 0));
  document.addEventListener("pointerdown", e => { if (grown && !(e.target.closest && e.target.closest(".term-panel,#term-key,#monitor-root,.cu-wrap.expanded"))) setGrown(false); });
  addEventListener("message", e => { if (cuFrame && e.source === cuFrame.contentWindow && e.origin === cuOrigin) { if (e.data === "cu:expand") setCu(true); else if (e.data === "cu:collapse") setCu(false); else if (e.data === "cu:reload") location.reload(); return; }
    if (termFrame && e.source === termFrame.contentWindow && e.origin === termOrigin && typeof e.data === "string") { const m = /^t0:(enter|out|still)$/.exec(e.data); if (m) t0(m[1]); } });
  $("settings-key").addEventListener("click", () => { if (cuFrame) cuFrame.contentWindow.postMessage({ type: "cu:open", screen: "settings" }, cuOrigin); });
  $("term-key").addEventListener("click", () => setGrown(!grown, true)); $("term-size").addEventListener("click", () => setGrown(!grown, !grown));

  // ---------- questions: the sky answers
  const back = $("back"); let viewIdle = 0;
  function goHome() { if (Sky && Sky.view !== "home") Sky.show("home"); back.hidden = true; }
  function askView(v) { if (!Sky) { answer("This view needs WebGL, so there's no sky to answer with. Try a browser or setting where WebGL is on.", ""); return; } const r = Sky.show(v); if (r.same) return; if (r.none) { answer(r.say, r.meta); return; } back.hidden = v === "home"; viewIdle = performance.now(); W.focus = null; W.hoverFocus = null; answer(r.say, r.meta); }
  $("chips").addEventListener("click", e => { if (e.target.id === "back") { goHome(); return; } const v = e.target.dataset && e.target.dataset.v; if (v) askView(v); });
  // dragging along the horizon rewinds the galaxy to that moment; letting go, it re-forms
  const toT = x => Math.max(0, Math.min(nowMin() - G.t0, (x - G.w * .04) / (G.w * .92) * (G.t1 - G.t0)));
  function doScrub(x) { const t = toT(x), p = onRim(dayX(t)), l = onRim(dayX(t), 35), d = $("scrubdot"), tx = $("scrubl"), at = G.t0 + t; Sky.scrub(at); d.setAttribute("cx", p.x); d.setAttribute("cy", p.y); d.setAttribute("visibility", "visible"); const near = ((state && state.events) || []).filter(e => BEAD[e.kind] && Math.abs((parseT(e.t) || 0) - at) <= 8).sort((a, b) => Math.abs(parseT(a.t) - at) - Math.abs(parseT(b.t) - at))[0]; tx.textContent = hhmm(at) + (near ? "  " + near.text.slice(0, 60) : ""); tx.setAttribute("x", l.x); tx.setAttribute("y", l.y); tx.setAttribute("transform", `rotate(${l.deg} ${l.x} ${l.y})`); tx.setAttribute("visibility", "visible"); }
  world.addEventListener("dblclick", e => { if (Sky && e.target.id === "scrub") askView("time"); });
  let downAt = null;
  world.addEventListener("pointerdown", e => { if (!Sky || !(e.target.id === "scrub" || e.target._ev)) return; e.preventDefault(); downAt = { x: e.clientX, t: e.target }; goHome(); W.scrubbing = true; doScrub(e.clientX); });
  addEventListener("pointermove", e => { if (W.scrubbing) doScrub(e.clientX); viewIdle = performance.now(); W.overUI = !!(e.target.closest && e.target.closest(".top,.legend,.term-panel,#shown,#monitor-root,.work,.today")); });
  addEventListener("pointerup", e => { if (!W.scrubbing) return; W.scrubbing = false; Sky.scrub(null); $("scrubdot").setAttribute("visibility", "hidden"); $("scrubl").setAttribute("visibility", "hidden"); if (downAt && Math.abs(e.clientX - downAt.x) < 4 && downAt.t._ev) { const ev = downAt.t._ev; if (ev.job_id && ev.lane && LANE.test(ev.lane) && JOB.test(ev.job_id) && window.FieldMonitor) FieldMonitor.open(ev.lane, ev.job_id); else if (ev.card_id) selectCard(ev.card_id); } downAt = null; });

  // ---------- keys, chrome
  const setVoice = v => { on = v; LF.setOn(v); $("voice-key").setAttribute("aria-pressed", String(v)); if (!v && window.LiveVoice) { LiveVoice.stop(); if (cur && cur.finish) cur.finish(); } try { localStorage.setItem("lois-voice-on", v ? "1" : "0"); } catch (e) {} };
  try { if (localStorage.getItem("lois-voice-on") === "0") { on = false; LF.setOn(false); $("voice-key").setAttribute("aria-pressed", "false"); } } catch (e) {}
  $("voice-key").addEventListener("click", () => setVoice(!on));
  $("full").addEventListener("click", () => { if (!document.fullscreenElement) document.documentElement.requestFullscreen?.().catch(() => {}); else document.exitFullscreen?.(); });
  document.addEventListener("fullscreenchange", () => $("full").setAttribute("aria-pressed", String(!!document.fullscreenElement)));
  $("legend-k").addEventListener("click", () => { const closed = $("legend").classList.toggle("closed"); $("legend-k").setAttribute("aria-expanded", String(!closed)); });
  const LENS_KEYS = { __proto__: null, 1: "spend", 2: "stuck", 3: "kinds", 4: "time" };
  document.addEventListener("keydown", e => { if (e.target.matches && e.target.matches("input,textarea")) return; if (window.FieldMonitor && FieldMonitor.isOpen()) return; const k = e.key.toLowerCase(); if (k === "v") setVoice(!on); else if (k === "t" && termFrame) { e.preventDefault(); setGrown(!grown, true); } else if (k === "," ) $("settings-key").click(); else if (LENS_KEYS[k] && !e.ctrlKey && !e.metaKey && !e.altKey) askView(LENS_KEYS[k]); else if (k === "escape") { tell("tm:cancel"); if (grown) setGrown(false); else if (Sky && Sky.view !== "home") goHome(); else $("legend").classList.add("closed"); } });
  LF.fail = text => { setCaption(text + " Try a browser or setting where WebGL is on."); $("meta").textContent = "The job list and the controls still work."; };

  // ---------- the work list and today's numbers, from /api/state
  const list = $("jobs-list"), rows = new Map(); let lastSpend = null, spT = 0;
  const elapsedWord = s => s == null ? "" : s < 60 ? "under a minute" : Math.round(s / 60) + " min";
  function jobRow(id) { const b = document.createElement("button"); b.type = "button"; b.className = "job"; const o = svg("svg", { class: "orb", viewBox: "0 0 24 24", "aria-hidden": "true" }); b._p = svg("circle", { class: "pg", cx: 12, cy: 12, r: 10, pathLength: 1 }); o.append(svg("circle", { class: "bg", cx: 12, cy: 12, r: 10 }), b._p, svg("circle", { class: "ct", cx: 12, cy: 12, r: 3 })); b._n = document.createElement("span"); b._n.className = "n"; b._s = document.createElement("span"); b._s.className = "s"; b.append(o, b._n, b._s);
    const at = () => ({ x: b.getBoundingClientRect().left + 26, y: $("work").getBoundingClientRect().top }); const inn = () => { if (!Sky || !b._u) return; Sky.brush(b._u(), at()); W.hoverFocus = b._mesh ? b._mesh() : null; }, out = () => { if (Sky) Sky.brush(null); W.hoverFocus = null; };
    b.addEventListener("pointerenter", inn); b.addEventListener("pointerleave", out); b.addEventListener("focus", inn); b.addEventListener("blur", out); b.addEventListener("click", () => { if (b._go) b._go(); }); list.appendChild(b); rows.set(id, b); return b; }
  const STATE_C = { __proto__: null, waiting: "var(--gold)", approved: "var(--ok)", revise: "var(--amber)", failed: "var(--crit)", cancelled: "var(--crit)", interrupted: "var(--crit)" };
  function setRow(id, cls, color, prog, name, sub, u, mesh, go) { const b = rows.get(id) || jobRow(id); if (b._cls !== cls) { b._cls = cls; b.className = "job " + (["s-waiting", "s-failed"].includes(cls) ? cls : ""); } b.style.setProperty("--c", color); b._p.style.strokeDashoffset = 1 - prog; if (b._n.textContent !== name) b._n.textContent = name; if (b._s.textContent !== sub) b._s.textContent = sub; b._u = u; b._mesh = mesh; b._go = go; list.appendChild(b); }
  function renderWork() { const s = state, seen = new Set(), cards = (s.gate || []).filter(c => CARD.test(c.id)), avg = (() => { const d = ((field && field.jobs) || []).filter(j => j.end != null && j.role === "developer"); return d.length >= 3 ? d.reduce((a, j) => a + (j.end - j.start), 0) / d.length : 0; })();
    cards.slice(0, 2).forEach(c => { const id = "c" + c.id; seen.add(id); setRow(id, "s-waiting", STATE_C.waiting, 1, (c.title || (cardNoun(c).slice(2) + " " + (c.branch || c.repo))).slice(0, 60), c.decided ? "Decided · going out" : `Waiting${c.seconds_left != null ? " · " + Math.max(0, Math.round(c.seconds_left / 60)) + " min left" : ""}. Approve below`, () => ({ kind: "gold", card: c }), () => Sky && Sky.goldMesh(c.id), () => selectCard(c.id)); });
    const lanes = (s.lanes || []).filter(l => l.state !== "idle" && l.job && JOB.test(l.job)).sort((a, b) => (a.state === "running" ? 0 : 1) - (b.state === "running" ? 0 : 1)).slice(0, 4 - Math.min(2, cards.length));
    lanes.forEach(l => { const id = "l" + l.name; seen.add(id); const j = Sky && Sky.job(l.job), color = STATE_C[l.state] || (j ? `hsl(${j.order.hue}, 85%, 68%)` : "var(--blue)"), mw = modelWord(l.model), run = l.state === "running", sub = run ? [mw, elapsedWord(l.elapsed)].filter(Boolean).join(" · ") : (own({ approved: "Approved", revise: "Changes requested", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted", done: "Done" }, l.state) || String(l.state)) + (l.cost != null ? " · $" + l.cost.toFixed(2) : ""); setRow(id, /failed|cancelled|interrupted/.test(l.state) ? "s-failed" : "", color, run ? (avg ? Math.min(1, (l.elapsed || 0) / 60 / avg) : .15) : 1, l.desk || l.branch || laneWords(l.name), sub, () => Sky && Sky.jobU(l.job), () => Sky && Sky.jobMesh(l.job), () => { if (window.FieldMonitor && LANE.test(l.name)) FieldMonitor.open(l.name, l.job); }); });
    const bad = ((field && field.jobs) || []).filter(j => /failed|cancelled|interrupted/.test(j.status)); if (bad.length && !lanes.some(l => l.job === bad[bad.length - 1].id)) { const j = bad[bad.length - 1], id = "failed"; seen.add(id); setRow(id, "s-failed", "var(--crit)", 1, bad.length === 1 ? "1 failed today" : bad.length + " failed today", j.label + (j.cost != null ? " · $" + j.cost.toFixed(2) : ""), () => Sky && Sky.jobU(j.id), () => Sky && Sky.jobMesh(j.id), () => W.onPickJob(j)); }
    rows.forEach((b, id) => { if (!seen.has(id)) { b.remove(); rows.delete(id); } });
    const running = (s.totals || {}).running || 0, h = $("work-h"); h.textContent = ""; const bold = t => { const e = document.createElement("b"); e.textContent = t; return e; }; h.append(bold(String(running)), " running"); if (cards.length) { const g = document.createElement("span"); g.className = "g"; g.append(" · ", bold(String(cards.length)), " waiting for you"); h.append(g); }
    const cost = (s.totals || {}).cost || 0; $("spend").textContent = "$" + cost.toFixed(2); $("jobs").textContent = String((s.totals || {}).jobs || 0); if (lastSpend != null && cost - lastSpend > .001) { const d = $("sp-d"); d.textContent = "+$" + (cost - lastSpend).toFixed(2); d.classList.add("on"); clearTimeout(spT); spT = setTimeout(() => d.classList.remove("on"), 3500); } lastSpend = cost;
    $("roles").textContent = (((s.spend || {}).roles) || []).slice(0, 3).filter(r => r.cost > 0).map(r => r.role + " $" + r.cost.toFixed(2)).join(" · ");
    const sv = s.service || {}, note = !s.bridge && !s.demo ? "The dispatch bridge is down" : sv.due === "restart" ? "Restart due · see Service" : sv.due ? "Update due · see Service" : ""; $("service-note").hidden = !note; $("service-note").textContent = note;
    const hint = (s.hints || [])[0]; $("hint-line").hidden = !hint; $("hint-line").textContent = hint ? String(hint.text || "").slice(0, 200) : "";
    $("sum").textContent = running + " running · " + (cards.length ? cards.length + " waiting · " : "") + "$" + cost.toFixed(2); document.title = cards.length ? `(${cards.length}) ${plant()}` : plant();
    if (s.term) { const on = !!s.term.running; $("term-lamp").classList.toggle("on", on); $("pane-lamp").classList.toggle("on", on); $("term-panel").classList.toggle("run", on); } if (s.demo) { $("foot").hidden = false; $("foot").textContent = "Demo: made-up data. Nothing here acts."; }
    $("plate-project").textContent = String(s.project || ""); $("plate-name").textContent = plant(); if (Sky) $("legend-rate").textContent = "About " + Sky.rate.toLocaleString() + " to the dollar"; G.dirty = true; drawBeads(); tickDay(); }
  const plant = () => { const p = state && state.look && state.look.plant; return typeof p === "string" && p ? p : "LOIS"; };

  // ---------- labels in the sky and threads of light, every frame
  const tagsEl = $("tags"), v3 = new THREE.Vector3();
  const toScreen = o => { o.getWorldPosition(v3); const d = v3.distanceTo(LF.camera.position); v3.project(LF.camera); return { x: (v3.x * .5 + .5) * G.w, y: (-v3.y * .5 + .5) * G.h, d }; };
  const curve = (x0, y0, x1, y1) => { const dx = x1 - x0, dy = y1 - y0, out = Math.sign(x1 - G.cx) * Math.abs(dy) * .32; return `M${x0} ${y0} C${x0 + out} ${y0 + dy * .35}, ${x1 + out * .55} ${y1 - dy * .3}, ${x1} ${y1}`; };
  const thread = (id, d) => { const el = $(id); if (d) { el.setAttribute("d", d); el.classList.add("on"); } else el.classList.remove("on"); };
  // Each crystal wears its step number and one plain state mark (.pip); each order has one label, its name and its state in words, beside
  // the order's newest crystal. Placement is field-order.js's; it is redone only when a crystal or a label moves, not every frame.
  // Every word goes in with textContent; a class comes only from FieldOrder.TONES' keys.
  const pipsEl = $("pips"), pips = new Map(), ords = new Map(), leadG = $("leaders"), leads = new Map(); let topY = 52, placeKey = "", placed = { at: new Map(), hidden: [] };
  const toneOf = t => (window.FieldOrder && Object.prototype.hasOwnProperty.call(FieldOrder.TONES, t) ? t : "done");
  function drawPips(list, show) { const seen = new Set();
    if (show) list.forEach(p => { seen.add(p.key); let el = pips.get(p.key); if (!el) { el = document.createElement("div"); el.className = "pip"; el._m = document.createElement("i"); el._m.className = "pm"; el._n = document.createElement("b"); el._n.className = "pn"; el.append(el._m, el._n); pipsEl.appendChild(el); pips.set(p.key, el); }
      const tone = toneOf(p.tone); if (el._tone !== tone) { el._tone = tone; el.className = "pip p-" + tone; } const n = p.n ? String(p.n) : ""; if (el._n.textContent !== n) el._n.textContent = n;
      const x = Math.round(p.x * 4) / 4, y = Math.round(p.y * 4) / 4; if (el._x !== x || el._y !== y) { el._x = x; el._y = y; el.style.transform = `translate(${x}px, ${y}px)`; }
      const r = Math.round(p.r * 2) / 2; if (el._r !== r) { el._r = r; el.style.setProperty("--r", r + "px"); el.classList.toggle("tiny", r < 9.5); } const o = Math.round(p.dim * 20) / 20; if (el._o !== o) { el._o = o; el.style.opacity = String(o); } });
    pips.forEach((el, k) => { if (!seen.has(k)) { el.remove(); pips.delete(k); } }); }
  const rectOf = p => [p.x - p.r, p.y - p.r, p.x + p.r, p.y + p.r];
  function orderLabel(o) { let el = ords.get(o.id); const tone = toneOf(o.tone); if (!el) { el = document.createElement("div"); el.className = "tag ord"; el._d = document.createElement("i"); el._b = document.createElement("b"); el._s = document.createElement("span"); el._w = document.createElement("span"); el._s.className = "sep"; el._w.className = "wd"; el._s.textContent = " · "; el.append(el._d, el._b, el._s, el._w); tagsEl.appendChild(el); ords.set(o.id, el); }
    if (el._col !== o.color) { el._col = o.color; el._d.style.setProperty("background", o.color); } if (el._b.textContent !== o.name || el._w.textContent !== o.word || el._tone !== tone) { el._b.textContent = o.name; el._w.textContent = o.word; el._tone = tone; el.className = "tag ord t-" + tone + (el._on ? " on" : ""); el._wd = el.offsetWidth; el._ht = el.offsetHeight; } return el; }
  function skyLabels() { const home = Sky.view === "home", list = home || Sky.view === "stuck" ? Sky.packets() : []; drawPips(list, !!list.length); const hov = !!Sky.hoverInfo(); if (tagsEl._hov !== hov) { tagsEl._hov = hov; tagsEl.classList.toggle("hov", hov); }
    const orders = home && !Sky.rewound ? Sky.orderList() : [], byKey = new Map(list.map(p => [p.key, p])), c = Sky.center() || { x: G.cx, y: G.rim * .5 }, items = [], seen = new Set();
    orders.forEach(o => { const anchors = o.keys.map(k => byKey.get(k)).filter(Boolean).map(p => ({ x: p.x, y: p.y, r: p.r })); if (!anchors.length) return; const el = orderLabel(o); seen.add(o.id); items.push({ id: o.id, el, w: el._wd, h: el._ht, anchors, tone: o.tone, newest: o.newest, force: o.tone === "wait" || o.tone === "run" }); });
    const ranked = window.FieldOrder ? FieldOrder.rank(items) : items, key = G.w + "x" + G.h + "|" + topY + "|" + ranked.map(i => i.id + ":" + i.w + "x" + i.h + i.anchors.map(a => Math.round(a.x) + "," + Math.round(a.y) + "," + Math.round(a.r)).join(";")).join("|") + "|" + list.length;
    if (key !== placeKey && ranked.length) { placeKey = key; const onWitness = a => [0, .25, .5, .75, 1].some(k => Sky.overWitness(a[0] + (a[2] - a[0]) * k, a[1], 6) || Sky.overWitness(a[0] + (a[2] - a[0]) * k, a[3], 6) || Sky.overWitness(a[0] + (a[2] - a[0]) * k, (a[1] + a[3]) / 2, 6));
      placed = FieldOrder.placeLabels(ranked, { W: G.w, top: topY, rimAt, cx: c.x, cy: c.y, boxes: list.map(rectOf), blocked: onWitness }); }
    ranked.forEach(i => { const at = placed.at.get(i.id), el = i.el, show = !!at; if (show && (el._px !== at.x || el._py !== at.y)) { el._px = at.x; el._py = at.y; el.style.transform = `translate(${Math.round(at.x)}px, ${Math.round(at.y)}px)`; } if (el._on !== show) { el._on = show; el.classList.toggle("on", show); }
      let ld = leads.get(i.id); if (show && at.far) { if (!ld) { ld = svg("path", {}); leadG.appendChild(ld); leads.set(i.id, ld); } const a = at.anchor, lx = Math.max(at.x, Math.min(at.x + i.w, a.x)), ly = Math.max(at.y, Math.min(at.y + i.h, a.y)), d = Math.hypot(a.x - lx, a.y - ly) || 1, ex = a.x - (a.x - lx) / d * (a.r + 3), ey = a.y - (a.y - ly) / d * (a.r + 3), dd = `M${Math.round(lx)} ${Math.round(ly)} L${Math.round(ex)} ${Math.round(ey)}`; if (ld._d !== dd) { ld._d = dd; ld.setAttribute("d", dd); } } else if (ld) { ld.remove(); leads.delete(i.id); } });
    ords.forEach((el, id) => { if (!seen.has(id)) { el.remove(); ords.delete(id); const ld = leads.get(id); if (ld) { ld.remove(); leads.delete(id); } } }); }
  let capStamp = 0;
  function threads(now) { const cards = (state && state.gate) || [], gm = cards.length && !$("cu-wrap").classList.contains("expanded") ? Sky.goldMesh() : null; if (gm && gm.visible && cuFrame) { const P = toScreen(gm), r = cuFrame.getBoundingClientRect(), left = P.x < G.cx, sgn = left ? -1 : 1, y0 = r.top - 3; if (now - capStamp > 400 || !tight) { const rg = document.createRange(); rg.selectNodeContents(txt); tight = rg.getBoundingClientRect(); capStamp = now; } const w = tight.width || 0, xo = G.cx + sgn * Math.max(180, (w / 2) + 26), rimY = rimAt(xo); thread("t-gold", `M${G.cx} ${y0} L${xo - sgn * 40} ${y0} Q${xo} ${y0}, ${xo} ${y0 - 40} L${xo} ${rimY} C${xo} ${rimY - (rimY - P.y) * .5}, ${P.x} ${P.y + (rimY - P.y) * .5}, ${P.x} ${P.y}`); } else thread("t-gold", null);   // along the gap above the decision and round the caption, never through words
    const fo = W.focus && W.focus.visible !== false && Sky.view === "home" && W.speaking && !W.hoverFocus && W.focus !== gm ? W.focus : null; if (fo && capWords.length) { if (now - capStamp > 400 || !tight) { const rg = document.createRange(); rg.selectNodeContents(txt); tight = rg.getBoundingClientRect(); capStamp = now; } const P = toScreen(fo), left = P.x < G.cx; thread("t-voice", P.y < rimAt(P.x) ? curve(left ? tight.left - 10 : tight.right + 10, tight.top + 13, P.x, P.y) : null); } else thread("t-voice", null);
    const hv = Sky.hoverInfo(); if (hv && hv.mesh) { const P = toScreen(hv.mesh), dx = P.x - hv.x, dy = P.y - hv.y; thread("t-hover", `M${hv.x} ${hv.y} C${hv.x + dx * .1} ${hv.y + dy * .5}, ${P.x - dx * .3} ${P.y - dy * .2}, ${P.x} ${P.y}`); } else thread("t-hover", null); }

  // ---------- polling. Only GETs to its own origin.
  async function getJSON(url) { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) throw new Error(url + " " + r.status); return r.json(); }
  let lost = 0;
  async function pollState() { try { state = await getJSON("/api/state"); lost = 0; W.running = (state.totals || {}).running || 0; W.waiting = (state.gate || []).length; ensureFrames(); if (window.FieldShown) FieldShown.update(state); renderWork(); if (Sky && field) Sky.ingest(field, state); narrate(); if (!greeted) { greeted = true; layout(); greet(); } } catch (e) { lost++; if (lost >= 3) { $("hint-line").hidden = false; $("hint-line").textContent = "Lost touch with the dashboard. What's here may be out of date."; } } setTimeout(pollState, 2000); }
  async function pollField() { try { field = await getJSON("/api/field"); if (Sky && state) Sky.ingest(field, state); } catch (e) {} setTimeout(pollField, 3000); }
  LF.onReady = () => { Sky = LF.sky; if (field && state) Sky.ingest(field, state); layout(); };
  if (LF.sky) LF.onReady();
  if (LF.failMsg) LF.fail(LF.failMsg);
  if (window.LiveVoice) LiveVoice.watch(); layout(); addEventListener("resize", layout); pollState(); pollField();
  let lastSlow = 0, talkSet = -1;
  function loop(now) { const has = !!termFrame && document.activeElement === termFrame && document.hasFocus(); if (has && !hadFocus) lensArmed = true; hadFocus = has; W.attendT = has && lensArmed ? 1 : 0;   // the sky dims while you are in the terminal, and blooms back when LOIS speaks
    if (termFrame) { const tk = Math.round(Math.min(1, LF.U.uTalk.value * 2) * 25) / 25; if (tk !== talkSet) { talkSet = tk; $("term-panel").style.setProperty("--talk", String(tk)); } }
    LF.drawBeing(W, now, ...LF.pointer()); LF.drawMotes(now); atmos.style.opacity = .62 + .38 * ((W.s ? W.s.pres : .75) - .75) * 4 * .6 + (W.speaking ? .1 * Math.min(1, LF.U.uTalk.value * 2) : 0); if (Sky) { if (G.dirty && now - lastSlow > 220) { lastSlow = now; G.dirty = false; hug(); } skyLabels(); threads(now); if (Sky.view !== "home") { if (cur && cur.ans && W.speaking) viewIdle = now; else if (now - viewIdle > 14000) goHome(); } } litCaption(now); requestAnimationFrame(loop); }
  requestAnimationFrame(loop);
})();
