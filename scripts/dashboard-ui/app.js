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
    top.append(r.led, el("span", "lane-name", name), r.tag, r.key);
    r.lcd = el("div", "lcd lcd-text"); r.lcdKey = null;
    const stats = el("div", "lane-stats");
    const stat = label => { const s = el("div", "stat"); const v = el("span", "val"); s.append(el("span", "silk", label), v); stats.append(s); return v; };
    r.model = stat("Model"); r.cost = stat("Cost"); r.ctx = stat("Ctx");
    const timer = el("div", "stat timer");
    r.timerLabel = el("span", "silk");
    r.tbox = el("div", "lcd seg-box"); r.tbox.dataset.name = name; r.segText = null;
    timer.append(r.timerLabel, r.tbox);
    stats.append(timer);
    r.card.append(top, r.lcd, stats);
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
    setText(r.timerLabel, lane.state === "running" || lane.state === "held" ? "Elapsed" : "Took");
  }

  function renderLanes() {
    const box = $("lanes");
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

  // ---------- polling
  async function poll() {
    try {
      const r = await fetch("/api/state", { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      state = await r.json();
      polledAt = Date.now();
      renderHeader(); renderLanes(); renderGate(); renderSpend(); renderPaper();
      if (monLane) loadMon();
    } catch (e) {
      $("lamp-link").className = "led r blink";
    }
  }
  poll();
  setInterval(poll, POLL_MS);
  setInterval(() => { tickTimers(); tickGate(); }, 1000);
})();
