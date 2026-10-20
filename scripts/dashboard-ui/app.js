// factory dashboard page: polls /api/state every 2 s and draws it.
// Everything the agents wrote reaches the page as JSON strings and goes in with
// textContent, never parsed as HTML; the page's CSP allows only its own files.
"use strict";

(() => {
  const POLL_MS = 2000;
  const $ = id => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  };
  const NS = "http://www.w3.org/2000/svg";
  const svgEl = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };
  const usd = v => (v == null ? "--" : "$" + Number(v).toFixed(2));
  const pad = n => String(n).padStart(2, "0");
  const mmss = s => (s >= 3600 ? `${Math.floor(s / 3600)}:${pad(Math.floor(s / 60) % 60)}` : `${pad(Math.floor(s / 60))}:${pad(s % 60)}`);
  const ktok = n => (n == null ? "--" : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));

  const LED = { running: "g blink", held: "a blink", revise: "a", done: "g", approved: "g", idle: "", cancelled: "r", failed: "r" };
  const LABEL = { running: "Run", held: "Held", revise: "Revise", done: "Done", approved: "Approved", idle: "Idle", cancelled: "Cancelled", failed: "Failed" };

  let state = null, polledAt = 0, lastEvent = "", gateKey = null;
  // Monitor state: the open lane, its tab, the latest /api/lane data, a request
  // sequence number (stale responses are dropped) and what the body shows now.
  let monLane = null, monTab = "transcript", monData = null, monSeq = 0, monBusy = false;
  let shown = { lane: null, tab: null, keys: [], packet: null };
  // ---------- 7-segment LCD digits, drawn as SVG
  const SEG = { a: "4,2 6,0 16,0 18,2 16,4 6,4", b: "18,3 20,5 20,15 18,17 16,15 16,5", c: "18,19 20,21 20,31 18,33 16,31 16,21",
    d: "4,34 6,32 16,32 18,34 16,36 6,36", e: "2,19 4,21 4,31 2,33 0,31 0,21", f: "2,3 4,5 4,15 2,17 0,15 0,5", g: "4,18 6,16 16,16 18,18 16,20 6,20" };
  const DIG = { "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abfgcd", "-": "g", " ": "" };
  function seg(text, size) {
    let x = 0; const parts = [];
    for (const ch of text) {
      if (ch === ":") { parts.push(["colon", x]); x += 8; continue; }
      if (ch === ".") { parts.push(["dot", x]); x += 7; continue; }
      parts.push(["d", x, ch]); x += 26;
    }
    const svg = svgEl("svg", { viewBox: `-2 -1 ${x + 2} 38`, class: `seg${size ? " " + size : ""}`, role: "img", "aria-label": text });
    const g = svgEl("g", { transform: "skewX(-6)" });
    for (const p of parts) {
      if (p[0] === "colon") { g.append(svgEl("rect", { x: p[1] + 1, y: 9, width: 4, height: 4, class: "on" }), svgEl("rect", { x: p[1] + 1, y: 23, width: 4, height: 4, class: "on" })); continue; }
      if (p[0] === "dot") { g.append(svgEl("rect", { x: p[1] + 1, y: 32, width: 4, height: 4, class: "on" })); continue; }
      const lit = DIG[p[2]] || "";
      for (const s in SEG) {
        const pts = SEG[s].split(" ").map(pt => { const [a, b] = pt.split(",").map(Number); return `${a + p[1]},${b}`; }).join(" ");
        g.append(svgEl("polygon", { points: pts, class: lit.includes(s) ? "on" : "off" }));
      }
    }
    svg.append(g);
    return svg;
  }
  const setSeg = (node, text, size) => node.replaceChildren(seg(text, size));

  // ---------- transcript entries (watch.py's kinds) as display lines
  function lineText(e) {
    switch (e.kind) {
      case "start": return `▶ job ${e.text}`;
      case "task": return `> ${e.text}`;
      case "end": return `■ job ${e.text.replace(" ", " after ")}s`;
      case "branch": return `⎇ ${e.text}`;
      case "packet": return `packet ${e.text}`;
      case "error": return `✗ ${e.text}`;
      default: return e.text;
    }
  }
  const sinceSec = () => Math.floor((Date.now() - polledAt) / 1000);
  const elapsedOf = lane => (lane.elapsed == null ? null : lane.elapsed + (lane.state === "running" || lane.state === "held" ? sinceSec() : 0));

  // ---------- lanes
  // Cards are keyed by lane name and updated in place, so a focused card keeps
  // focus across polls and a click that spans a poll still lands.
  const cards = new Map();
  const setText = (n, t) => { t = String(t); if (n.textContent !== t) n.textContent = t; };
  const setClass = (n, c) => { if (n.className !== c) n.className = c; };
  const entryKey = e => `${e.ts}\u0000${e.kind}\u0000${e.text}`;

  function makeCard(name) {
    const r = { card: el("button", "lane plate") };
    r.card.type = "button";
    r.card.dataset.name = name;
    r.card.setAttribute("aria-label", `Open lane ${name}`);
    const top = el("div", "lane-top");
    r.led = el("span", "led"); r.tag = el("span", "state-tag"); r.key = el("span", "keycap key");
    r.lcd = el("div", "lcd lcd-text"); r.lcdKey = null;
    const stats = el("div", "lane-stats");
    const stat = label => { const s = el("div", "stat"); s.title = label; const v = el("span", "val"); s.append(el("span", "silk", label), v); stats.append(s); return v; };
    r.model = stat("Model"); r.cost = stat("Cost"); r.ctx = stat("Ctx");
    r.tbox = el("div", "lcd seg-box"); r.tbox.dataset.name = name; r.segText = null;
    top.append(r.led, el("span", "lane-name", name), r.tag, stats, r.tbox, r.key);
    r.card.append(top, r.lcd);
    r.card.addEventListener("click", () => openMon(name));
    return r;
  }

  function updateCard(r, lane, i) {
    setClass(r.card, `lane plate${lane.state === "idle" ? " idle" : ""}`);
    setClass(r.led, `led ${LED[lane.state] || ""}`);
    setClass(r.tag, `state-tag st-${lane.state}`);
    setText(r.tag, LABEL[lane.state] || lane.state);
    r.key.hidden = i >= 9;
    setText(r.key, i < 9 ? i + 1 : "");
    const lcdKey = JSON.stringify([lane.state, lane.ended, lane.held_note || "", lane.tail.map(entryKey)]);
    if (lcdKey !== r.lcdKey) {
      r.lcdKey = lcdKey;
      if (lane.state === "idle" && !lane.tail.length) {
        r.lcd.replaceChildren(el("div", null, "NO JOBS YET"));
      } else {
        r.lcd.replaceChildren(...lane.tail.map(e => el("div", `ln k-${e.kind}`, lineText(e))));
        if (lane.state === "held") r.lcd.append(el("div", "ln k-held", lane.held_note ? `⏸ ${lane.held_note}` : "⏸ HELD FOR APPROVAL"));
        if (lane.state === "idle" && lane.ended) r.lcd.append(el("div", "ln k-idle", `-- IDLE SINCE ${lane.ended.slice(0, 5)} --`));
      }
    }
    setText(r.model, lane.model || "--");
    setText(r.cost, usd(lane.cost));
    setText(r.ctx, ktok(lane.context_tokens));
    r.tbox.title = lane.state === "running" || lane.state === "held" ? "Elapsed" : "Took";
  }

  function renderLanes() {
    const box = $("lanes");
    const n = state.lanes.length;
    box.style.setProperty("--cols", String(n <= 2 ? Math.max(n, 1) : n <= 4 ? 2 : n <= 6 ? 3 : 4));
    const names = new Set(state.lanes.map(l => l.name));
    for (const [name, r] of cards) if (!names.has(name)) { r.card.remove(); cards.delete(name); }
    state.lanes.forEach((lane, i) => {
      let r = cards.get(lane.name);
      if (!r) { r = makeCard(lane.name); cards.set(lane.name, r); }
      updateCard(r, lane, i);
      if (box.children[i] !== r.card) box.insertBefore(r.card, box.children[i] || null);
    });
    tickTimers();
  }

  function tickTimers() {
    if (!state) return;
    const byName = new Map(state.lanes.map(l => [l.name, l]));
    for (const [name, r] of cards) {
      const lane = byName.get(name);
      const s = lane ? elapsedOf(lane) : null;
      const text = s == null ? "--:--" : mmss(Math.max(0, s));
      if (text !== r.segText) { r.segText = text; setSeg(r.tbox, text, "sm"); }
    }
    const now = new Date((state.now + sinceSec()) * 1000);
    setSeg($("ro-clock"), `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`);
  }

  // ---------- header, gate, spend, printer
  function renderHeader() {
    $("project").textContent = state.project;
    $("lamp-link").className = "led g";
    $("lamp-bridge").className = `led ${state.bridge ? "g" : "r"}`;
    $("lamp-gate").className = `led ${state.gate.length ? "a blink" : ""}`;
    setSeg($("ro-spend"), Number(state.totals.cost).toFixed(2));
    setSeg($("ro-jobs"), String(state.totals.jobs));
    setSeg($("ro-run"), String(state.totals.running));
  }

  // The gate LCD is an aria-live region, so it is rebuilt only when the held
  // cards change. The countdown ticks in an aria-hidden node (screen readers get
  // the expiry as a fixed clock time instead), so it is never re-announced.
  function renderGate() {
    const cards = state.gate;
    const key = JSON.stringify(cards.map(c => Object.assign({}, c, { seconds_left: c.seconds_left == null })));
    if (key === gateKey) { tickGate(); return; }
    gateKey = key;
    const lcd = $("gate-lcd");
    $("gate-led").className = `led ${cards.length ? "a blink" : ""}`;
    $("gate-state").textContent = cards.length ? `Held · ${cards.length}` : "Clear";
    if (!cards.length) {
      lcd.replaceChildren(el("div", "big", "No writes held"));
      $("gate-hint").textContent = "";
      return;
    }
    const c = cards[0];
    const what = c.tool === "git_push" ? `${c.commits ?? "?"} commits · ${c.file_count ?? "?"} files` : (c.title || c.tool);
    let line;
    if (c.seconds_left == null) {
      line = el("div", null, `${what} · expiry unknown`);
    } else {
      const at = new Date((state.now + c.seconds_left) * 1000);
      const cd = el("span", "gate-cd");
      cd.setAttribute("aria-hidden", "true");
      line = el("div", null, `${what} · expires `);
      line.append(cd, el("span", "sr-only", `at ${pad(at.getHours())}:${pad(at.getMinutes())}`));
    }
    // The broker names the role, not the lane: list the role's running lanes.
    const role = c.role || c.principal;
    const lanes = Array.isArray(c.lanes) ? c.lanes : [];
    lcd.replaceChildren(
      el("div", "big", `${c.tool} · ${role}`),
      ...(lanes.length ? [el("div", null, `from ${role} (lane${lanes.length > 1 ? "s" : ""}: ${lanes.join(", ")})`)] : []),
      el("div", null, `→ ${c.repo}`),
      el("div", null, c.base ? `${c.branch} → ${c.base}` : c.branch),
      line,
      ...(cards.length > 1 ? [el("div", null, `+ ${cards.length - 1} more held`)] : []));
    tickGate();
    $("gate-hint").textContent = `Decide in a terminal: factory approvals approve ${c.id}  (or decline ${c.id} --reason "...")`;
  }
  function tickGate() {
    const cd = document.querySelector("#gate-lcd .gate-cd");
    if (cd && state && state.gate.length && state.gate[0].seconds_left != null) setText(cd, mmss(Math.max(0, state.gate[0].seconds_left - sinceSec())));
  }

  function renderSpend() {
    const hours = state.spend.hours;  // midnight to now, one column an hour
    const CELLS = 12;
    const top = Math.max(1, ...hours.map(h => h.cost));
    $("bargraph").replaceChildren(...hours.map(h => {
      const col = el("div", "bcol");
      const cells = el("div", "cells");
      const lit = h.cost > 0 ? Math.max(1, Math.round(h.cost / top * CELLS)) : 0;
      for (let c = CELLS; c >= 1; c--) {
        const on = c <= lit;
        cells.append(el("i", `cell${on ? " on" : ""}${on && c > CELLS * 0.66 ? " hot" : ""}${on && c > CELLS * 0.9 ? " max" : ""}`));
      }
      col.title = `${pad(h.hour)}:00 ${usd(h.cost)}`;
      col.append(cells, el("span", "silk", h.hour % 6 === 0 ? pad(h.hour) : " "));
      return col;
    }));
    const roles = state.spend.roles;
    const max = Math.max(0.0001, ...roles.map(r => r.cost));
    $("roles").replaceChildren(...roles.map(r => {
      const row = el("div", "role-row");
      const segs = el("div", "segs");
      const lit = r.cost > 0 ? Math.max(1, Math.round(r.cost / max * 20)) : 0;
      for (let k = 0; k < 20; k++) segs.append(el("i", k < lit ? "on" : null));
      row.append(el("span", "silk", r.role), segs, el("span", "v", usd(r.cost)));
      return row;
    }));
    $("spend-sub").textContent = `${state.totals.jobs} jobs today`;
  }

  const nearBottom = (n, slack) => n.scrollHeight - n.scrollTop - n.clientHeight < slack;
  function renderPaper() {
    const events = state.events;
    const key = events.length ? JSON.stringify(events[events.length - 1]) + events.length : "";
    if (key === lastEvent) return;
    const fresh = lastEvent !== "";
    lastEvent = key;
    const paper = $("paper");
    const follow = !fresh || nearBottom(paper, 18);  // reading older lines: stay put
    paper.replaceChildren(...events.map((e, i) => {
      const n = el("div", `pl${e.red ? " red" : ""}${fresh && follow && i === events.length - 1 ? " new" : ""}`);
      n.append(el("span", null, e.t.slice(0, 5)), el("span", null, e.kind), el("span", null, e.text));
      n.title = `${e.t} ${e.kind} ${e.text}`;
      return n;
    }));
    if (!events.length) {
      const n = el("div", "pl");
      n.append(el("span"), el("span"), el("span", null, "-- nothing yet today --"));
      paper.append(n);
    }
    if (follow) paper.scrollTop = paper.scrollHeight;
    $("print-sub").textContent = `${events.filter(e => e.kind === "DENY").length} denied`;
  }

  // ---------- monitor (one lane's transcript and review packet)
  function openMon(name) {
    monLane = name; monTab = "transcript"; monData = null; monBusy = false; monSeq++;
    shown = { lane: null, tab: null, keys: [], packet: null };
    const body = $("mon-body");
    body.replaceChildren(); body.scrollTop = 0;
    $("scrim").hidden = false;
    renderMon();
    loadMon();
    $("mon-close").focus();
  }
  function closeMon() {
    if (!monLane) return;
    const r = cards.get(monLane);  // back to this lane's card, looked up by name
    monLane = null; monData = null; monSeq++;
    $("scrim").hidden = true;
    if (r && r.card.isConnected) r.card.focus();
  }
  async function loadMon() {
    if (!monLane || monBusy) return;
    const name = monLane, seq = ++monSeq;
    monBusy = true;
    try {
      const r = await fetch(`/api/lane?name=${encodeURIComponent(name)}`, { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      const data = await r.json();
      if (seq === monSeq && monLane === name) { monData = data; renderMon(); }
    } catch (e) { /* the header's Link lamp shows the outage */ }
    finally { if (seq === monSeq) monBusy = false; }
  }
  function setTab(tab) {
    if (!monLane) return;
    monTab = tab;
    renderMon();
    const body = $("mon-body");
    body.scrollTop = tab === "packet" ? 0 : body.scrollHeight;
  }
  const transcriptLine = e => { const n = el("div", `ln k-${e.kind}`); n.append(el("span", "ts", e.ts), el("span", "tx", lineText(e))); return n; };
  function renderMon() {
    const d = monData || (state && state.lanes.find(l => l.name === monLane)) || { name: monLane, state: "idle", tail: [] };
    setText($("mon-title"), `MONITOR · ${d.name}`);
    setClass($("mon-led"), `led ${LED[d.state] || ""}`);
    const st = $("mon-state"); setClass(st, `state-tag st-${d.state}`); setText(st, LABEL[d.state] || d.state);
    setText($("mon-note"), d.state === "held" && d.held_note ? d.held_note : "");
    document.querySelectorAll(".tabkey[data-tab]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.tab === monTab)));
    const body = $("mon-body");
    const same = shown.lane === monLane && shown.tab === monTab;
    if (monTab === "packet") {
      const text = monData ? monData.packet : "LOADING…";
      if (same && shown.packet === text) return;
      body.replaceChildren(el("pre", "packet", text));
      if (!same) body.scrollTop = 0;
      shown = { lane: monLane, tab: monTab, keys: [], packet: text };
      return;
    }
    const lines = d.tail || [];
    const keys = lines.map(entryKey);
    const old = same ? shown.keys : null;
    if (old && old.length === keys.length && keys.length && old[0] === keys[0] && old[old.length - 1] === keys[keys.length - 1]) return;
    if (old && !old.length && !keys.length && body.firstChild) return;
    const follow = !same || nearBottom(body, 40);
    if (!keys.length) {
      body.replaceChildren(el("p", null, "NO TRANSCRIPT YET."));
    } else {
      // The transcript is a sliding window: old lines fall off the front and new
      // ones arrive at the end. Find the overlap and patch only the ends, so a
      // text selection or the reading position survives a poll.
      let drop = -1;
      if (old && old.length) {
        for (let k = 0; k < old.length; k++) {
          const n = old.length - k;
          if (n <= keys.length && old[k] === keys[0] && old[old.length - 1] === keys[n - 1]) { drop = k; break; }
        }
      }
      if (drop < 0 || body.children.length !== old.length) {
        body.replaceChildren(...lines.map(transcriptLine));
      } else {
        const before = body.scrollHeight;
        for (let k = 0; k < drop; k++) body.firstChild.remove();
        const removed = before - body.scrollHeight;
        body.append(...lines.slice(old.length - drop).map(transcriptLine));
        if (!follow && removed > 0) body.scrollTop = Math.max(0, body.scrollTop - removed);
      }
    }
    if (follow) body.scrollTop = body.scrollHeight;
    shown = { lane: monLane, tab: monTab, keys, packet: null };
  }
  document.querySelectorAll(".tabkey[data-tab]").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $("mon-close").addEventListener("click", closeMon);
  $("scrim").addEventListener("click", e => { if (e.target === $("scrim")) closeMon(); });

  // Keep Tab inside the dialog while it is open (aria-modal).
  function trapTab(e) {
    const items = [...document.querySelectorAll(".monitor button, .monitor [tabindex='0']")].filter(n => n.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    const inside = $("scrim").contains(document.activeElement);
    if (e.shiftKey && (document.activeElement === first || !inside)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (document.activeElement === last || !inside)) { e.preventDefault(); first.focus(); }
  }
  document.addEventListener("keydown", e => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (monLane) {
      // The monitor is modal: digits do not open another lane while it is up.
      if (e.key === "Escape") { e.preventDefault(); closeMon(); }
      else if (e.key === "Tab") trapTab(e);
      else if (e.key === "p") setTab("packet");
      else if (e.key === "t") setTab("transcript");
      return;
    }
    const n = parseInt(e.key, 10);
    if (state && n >= 1 && n <= Math.min(9, state.lanes.length)) openMon(state.lanes[n - 1].name);
  });

  // ---------- front-panel knobs on the Coordinator's monitor
  // BRIGHT and CONTRAST drive CSS filters on the tube; V-HOLD, off-centre, makes the
  // picture roll like a real set (faster the further you turn it, up or down); BLOOM
  // turns the phosphor halo around lit text from none (fully counter-clockwise) to
  // full; CRT turns the tube's texture on the glyphs themselves (scanlines through
  // them, a red/blue convergence fringe, beam softness and smear, an aperture grille)
  // from flat (fully counter-clockwise) to a worn-out tube. Drag, scroll or use the arrow keys; double-click or Home resets. Settings
  // stay in this browser only.
  const KNOB_KEY = "fc1984-knobs";
  const DETENT = 12;           // V-HOLD locks within this many steps of centre
  // Each knob's range and its reset value. The centred ones sweep -100..100; BLOOM
  // sweeps 0..100 and starts a quarter of the way round, so the text is still crisp;
  // CRT sweeps 0..100 and starts at 30, which is the look the tube had before it.
  const KNOBS = { bright: [-100, 100, 0], contrast: [-100, 100, 0], vhold: [-100, 100, 0], bloom: [0, 100, 25], crt: [0, 100, 30] };
  const knobVals = {};
  for (const k in KNOBS) knobVals[k] = KNOBS[k][2];
  const clampKnob = (name, v) => Math.max(KNOBS[name][0], Math.min(KNOBS[name][1], v));
  try {
    const saved = JSON.parse(localStorage.getItem(KNOB_KEY) || "{}");
    for (const k in knobVals) if (saved && Number.isFinite(saved[k])) knobVals[k] = clampKnob(k, saved[k]);
  } catch (e) { /* no storage: defaults */ }
  const tube = $("coord-crt"), roll = $("crt-roll"), ghost = $("crt-ghost"), crtBody = $("crt-body");
  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function syncGhost() {  // the copy one frame below, for a seamless roll
    ghost.replaceChildren(...[...crtBody.childNodes].map(n => n.cloneNode(true)));
    ghost.scrollTop = crtBody.scrollTop;
  }
  new MutationObserver(() => { if (roll.classList.contains("rolling")) syncGhost(); })
    .observe(crtBody, { childList: true, subtree: true, characterData: true });
  crtBody.addEventListener("scroll", () => { ghost.scrollTop = crtBody.scrollTop; });

  function holdText(v) {
    if (Math.abs(v) < DETENT) return "locked";
    const speed = Math.abs(v) > 70 ? "fast" : Math.abs(v) > 35 ? "steady" : "slow";
    return `rolling ${v > 0 ? "up" : "down"}, ${speed}`;
  }
  // BLOOM: the live terminal is a cross-origin frame this page can't style inside,
  // so the halo is drawn on the frame's pixels from out here: stacked drop-shadows
  // on a transparent element follow the lit glyphs. 0 is no filter at all.
  function bloomFilter(v) {
    if (v <= 0) return "none";
    const b = v / 100;
    const glow = (r, a) => `drop-shadow(0 0 ${r.toFixed(1)}px rgba(0, 255, 65, ${a.toFixed(2)}))`;
    return `${glow(0.6 + 3.4 * b, 0.25 + 0.6 * b)} ${glow(3 + 15 * b, 0.12 + 0.58 * b)}`;
  }
  // CRT: up to the default it fades in the tube's usual scanlines and convergence
  // fringe; past it the scanlines thicken and darken, the fringe widens, an aperture
  // grille appears and the beam softens and smears sideways. The scanlines and grille
  // are the glass layer (.crt::after); the live frame's glyphs get the SVG filter
  // #crt-glyph (none at 0); the placeholder gets the same fringe as a text-shadow.
  const crtNode = id => document.getElementById(id);
  const glyph = { rc: crtNode("crt-glyph-rc"), bc: crtNode("crt-glyph-bc"), r: crtNode("crt-glyph-r"), b: crtNode("crt-glyph-b"),
    blur: crtNode("crt-glyph-blur"), mix: crtNode("crt-glyph-mix") };
  function crtTexture(v) {
    const home = KNOBS.crt[2];
    const lo = Math.min(v, home) / home, hi = Math.max(0, v - home) / (100 - home);  // 0..1 each side of the default
    const dx = 0.85 * lo + 1.75 * hi, fa = 0.3 * lo + 0.35 * hi, soft = 0.5 * hi;
    const f = n => n.toFixed(3);
    tube.style.setProperty("--scan", f(0.22 * lo + 0.33 * hi));
    tube.style.setProperty("--scan-w", `${f(1 + 0.5 * hi)}px`);
    tube.style.setProperty("--mask", f(0.2 * hi));
    tube.style.setProperty("--crt-fx", v <= 0 ? "0 0 0 transparent" :
      `-${f(dx)}px 0 rgba(255, 40, 90, ${f(fa * 1.2)}), ${f(dx)}px 0 rgba(40, 150, 255, ${f(fa * 1.2)}), ` +
      `${f(1.4 * hi)}px 0 ${f(1.6 * hi)}px rgba(0, 255, 65, ${f(0.4 * hi)})`);
    if (v <= 0 || !glyph.mix) return "";
    glyph.rc.setAttribute("values", `0 0 0 0 1  0 0 0 0 .157  0 0 0 0 .353  0 0 0 ${f(fa)} 0`);
    glyph.bc.setAttribute("values", `0 0 0 0 .157  0 0 0 0 .588  0 0 0 0 1  0 0 0 ${f(fa)} 0`);
    glyph.r.setAttribute("dx", f(-dx));
    glyph.b.setAttribute("dx", f(dx));
    glyph.blur.setAttribute("stdDeviation", `${f(1.3 * hi)} ${f(0.35 * hi)}`);
    glyph.mix.setAttribute("k2", f(soft));
    glyph.mix.setAttribute("k3", f(1 - 0.3 * hi));
    return "url(#crt-glyph)";
  }
  function applyKnobs() {
    tube.style.setProperty("--bright", (1 + knobVals.bright / 100 * 0.6).toFixed(3));
    tube.style.setProperty("--contrast", (1 + knobVals.contrast / 100 * 0.5).toFixed(3));
    const bloom = bloomFilter(knobVals.bloom), glyphFx = crtTexture(knobVals.crt);
    tube.style.setProperty("--bloom-fx", bloom);
    tube.style.setProperty("--term-fx", [glyphFx, bloom === "none" ? "" : bloom].filter(Boolean).join(" ") || "none");
    // The placeholder's faint wide text glow is part of the bloom too: it fades out
    // below the default, so fully counter-clockwise is crisp.
    tube.style.setProperty("--bloom", `color-mix(in srgb, var(--c) ${Math.round(24 * Math.min(1, knobVals.bloom / 25))}%, transparent)`);
    const v = knobVals.vhold, off = Math.abs(v) >= DETENT;
    const wasRolling = roll.classList.contains("rolling");
    if (off && !wasRolling && !roll.classList.contains("live")) syncGhost();
    roll.classList.toggle("rolling", off);
    if (off) {
      const t = (Math.abs(v) - DETENT) / (100 - DETENT);       // 0..1 past the detent
      roll.style.setProperty("--roll-dur", `${(9 - t * 8.2).toFixed(2)}s`);  // 9 s .. 0.8 s a frame
      roll.style.setProperty("--roll-dir", v > 0 ? "normal" : "reverse");
      roll.style.setProperty("--roll-still", `${(-t * 45 * Math.sign(v)).toFixed(1)}%`);  // reduced motion: a still slip
    }
    document.querySelectorAll(".knob[data-knob]").forEach(k => {
      const name = k.dataset.knob, val = knobVals[name], [lo, hi] = KNOBS[name];
      k.querySelector("i").style.setProperty("--rot", `${(-135 + (val - lo) / (hi - lo) * 270).toFixed(1)}deg`);
      k.setAttribute("aria-valuenow", String(Math.round(val)));
      k.setAttribute("aria-valuetext", name === "vhold" ? holdText(val) : name === "bloom" ? (val ? `${Math.round(val)}%` : "off") : name === "crt" ? (val ? `${Math.round(val)}%` : "flat") : `${Math.round(val)}`);
    });
    try { localStorage.setItem(KNOB_KEY, JSON.stringify(knobVals)); } catch (e) { /* fine */ }
  }
  function setKnob(name, value) {
    knobVals[name] = clampKnob(name, Math.round(value));
    applyKnobs();
  }
  document.querySelectorAll(".knob[data-knob]").forEach(k => {
    const name = k.dataset.knob, [lo, hi, home] = KNOBS[name];
    const travel = (hi - lo) / 200;  // the same drag turns every knob by the same angle
    let drag = null;
    k.addEventListener("pointerdown", e => {
      if (e.button !== 0) return;
      e.preventDefault();
      k.focus();
      k.setPointerCapture(e.pointerId);
      drag = { x: e.clientX, y: e.clientY, v: knobVals[name] };
    });
    k.addEventListener("pointermove", e => {
      if (!drag) return;
      // Up or right turns it clockwise, like reaching for a real knob.
      setKnob(name, drag.v + ((drag.y - e.clientY) * 1.1 + (e.clientX - drag.x) * 0.6) * travel);
    });
    const end = () => { drag = null; };
    k.addEventListener("pointerup", end);
    k.addEventListener("pointercancel", end);
    k.addEventListener("wheel", e => { e.preventDefault(); setKnob(name, knobVals[name] - Math.sign(e.deltaY) * 5); }, { passive: false });
    k.addEventListener("dblclick", () => setKnob(name, home));
    k.addEventListener("keydown", e => {
      const step = { ArrowUp: 5, ArrowRight: 5, ArrowDown: -5, ArrowLeft: -5, PageUp: 20, PageDown: -20 }[e.key];
      if (step != null) { e.preventDefault(); e.stopPropagation(); setKnob(name, knobVals[name] + step); }
      else if (e.key === "Home") { e.preventDefault(); e.stopPropagation(); setKnob(name, home); }
      else if (e.key === "End") { e.preventDefault(); e.stopPropagation(); setKnob(name, hi); }
    });
  });
  if (reduceMotion) roll.dataset.still = "1";
  applyKnobs();

  // ---------- Terminal 0: the Coordinator's live terminal
  // It lives on the dashboard's second origin (another port), framed here; this
  // page never sees its key and can't script it. The frame sits in the tube's
  // roll layer, so the glass, scanlines, knobs and V-HOLD still apply (the ghost
  // copy is for the static placeholder only: a frame can't be cloned).
  let termFrame = null;
  function renderTerm() {
    const t = state.term;
    $("lamp-term").className = !t ? "led" : t.running ? "led g" : t.reason === "elsewhere" ? "led a" : "led";
    if (!t) {
      $("crt-banner").textContent = "TERMINAL 0 · NO COORDINATOR LINK";
      $("crt-note").textContent = "This dashboard was started without --coordinator. Talk to the Coordinator with: factory chat";
      return;
    }
    if (termFrame) return;
    termFrame = document.createElement("iframe");
    termFrame.className = "crt-term";
    termFrame.title = "Coordinator terminal";
    // Same host as this page (the cookie is per host), the terminal's port.
    termFrame.src = `http://${location.hostname}:${Number(t.port)}/term`;
    termFrame.setAttribute("sandbox", "allow-scripts allow-same-origin");
    termFrame.setAttribute("referrerpolicy", "no-referrer");
    crtBody.hidden = true;
    roll.classList.add("live");
    roll.insertBefore(termFrame, roll.firstChild);
  }
  // A click on the tube's glass or bezel (outside the frame) hands the keyboard to the terminal.
  tube.addEventListener("click", () => { if (termFrame) termFrame.focus(); });

  // ---------- polling
  async function poll() {
    try {
      const r = await fetch("/api/state", { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      state = await r.json();
      polledAt = Date.now();
      renderHeader(); renderLanes(); renderGate(); renderSpend(); renderPaper(); renderTerm();
      if (monLane) loadMon();
    } catch (e) {
      $("lamp-link").className = "led r blink";
    }
  }
  poll();
  setInterval(poll, POLL_MS);
  setInterval(() => { tickTimers(); tickGate(); }, 1000);
})();
