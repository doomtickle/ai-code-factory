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

  let state = null, polledAt = 0, monLane = null, monTab = "transcript", monData = null, lastFocus = null, lastEvent = "";

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
  function renderLanes() {
    const lanes = state.lanes;
    $("lanes").replaceChildren(...lanes.map((lane, i) => {
      const card = el("button", `lane plate${lane.state === "idle" ? " idle" : ""}`);
      card.type = "button";
      card.setAttribute("aria-label", `Open lane ${lane.name}`);
      const top = el("div", "lane-top");
      top.append(el("span", `led ${LED[lane.state] || ""}`), el("span", "lane-name", lane.name),
        el("span", `state-tag st-${lane.state}`, LABEL[lane.state] || lane.state));
      if (i < 9) top.append(el("span", "keycap key", i + 1));
      const lcd = el("div", "lcd lcd-text");
      if (lane.state === "idle" && !lane.tail.length) {
        lcd.append(el("div", null, "NO JOBS YET"));
      } else {
        lane.tail.forEach(e => lcd.append(el("div", `ln k-${e.kind}`, lineText(e))));
        if (lane.state === "held") lcd.append(el("div", "ln k-held", "⏸ HELD FOR APPROVAL"));
        if (lane.state === "idle" && lane.ended) lcd.append(el("div", "ln k-idle", `-- IDLE SINCE ${lane.ended.slice(0, 5)} --`));
      }
      const stats = el("div", "lane-stats");
      const stat = (label, val) => { const s = el("div", "stat"); s.append(el("span", "silk", label), el("span", "val", val)); return s; };
      const timer = el("div", "stat timer");
      const tbox = el("div", "lcd seg-box");
      tbox.dataset.lane = String(i);
      timer.append(el("span", "silk", lane.state === "running" || lane.state === "held" ? "Elapsed" : "Took"), tbox);
      stats.append(stat("Model", lane.model || "--"), stat("Cost", usd(lane.cost)), stat("Ctx", ktok(lane.context_tokens)), timer);
      card.append(top, lcd, stats);
      card.addEventListener("click", () => openMon(lane.name));
      return card;
    }));
    tickTimers();
  }

  function tickTimers() {
    if (!state) return;
    document.querySelectorAll("#lanes .seg-box").forEach(box => {
      const lane = state.lanes[Number(box.dataset.lane)];
      const s = lane ? elapsedOf(lane) : null;
      setSeg(box, s == null ? "--:--" : mmss(Math.max(0, s)), "sm");
    });
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

  function renderGate() {
    const cards = state.gate;
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
    const left = Math.max(0, c.seconds_left - sinceSec());
    lcd.replaceChildren(
      el("div", "big", `${c.tool} · ${c.principal}`),
      el("div", null, `→ ${c.repo}`),
      el("div", null, c.base ? `${c.branch} → ${c.base}` : c.branch),
      el("div", null, `${what} · expires ${mmss(left)}`),
      ...(cards.length > 1 ? [el("div", null, `+ ${cards.length - 1} more held`)] : []));
    $("gate-hint").textContent = `Decide in a terminal: factory approvals approve ${c.id}  (or decline ${c.id} --reason "...")`;
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
      col.append(cells, el("span", "silk", h.hour % 6 === 0 ? pad(h.hour) : "\u00a0"));
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

  function renderPaper() {
    const events = state.events;
    const key = events.length ? JSON.stringify(events[events.length - 1]) + events.length : "";
    if (key === lastEvent) return;
    const fresh = lastEvent !== "";
    lastEvent = key;
    const paper = $("paper");
    paper.replaceChildren(...events.map((e, i) => {
      const n = el("div", `pl${e.red ? " red" : ""}${fresh && i === events.length - 1 ? " new" : ""}`);
      n.append(el("span", null, e.t.slice(0, 5)), el("span", null, e.kind), el("span", null, e.text));
      n.title = `${e.t} ${e.kind} ${e.text}`;
      return n;
    }));
    if (!events.length) {
      const n = el("div", "pl");
      n.append(el("span"), el("span"), el("span", null, "-- nothing yet today --"));
      paper.append(n);
    }
    paper.scrollTop = paper.scrollHeight;
    $("print-sub").textContent = `${events.filter(e => e.kind === "DENY").length} denied`;
  }

  // ---------- monitor (one lane's transcript and review packet)
  function openMon(name) {
    lastFocus = document.activeElement;
    monLane = name; monTab = "transcript"; monData = null;
    $("scrim").hidden = false;
    renderMon();
    loadMon();
    $("mon-close").focus();
  }
  function closeMon() { monLane = null; $("scrim").hidden = true; if (lastFocus) lastFocus.focus(); }
  async function loadMon() {
    if (!monLane) return;
    const name = monLane;
    try {
      const r = await fetch(`/api/lane?name=${encodeURIComponent(name)}`, { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      const data = await r.json();
      if (monLane === name) { monData = data; renderMon(); }
    } catch (e) { /* the header's Link lamp shows the outage */ }
  }
  function renderMon() {
    const d = monData || (state && state.lanes.find(l => l.name === monLane)) || { name: monLane, state: "idle", tail: [] };
    $("mon-title").textContent = `MONITOR · ${d.name}`;
    $("mon-led").className = `led ${LED[d.state] || ""}`;
    const st = $("mon-state"); st.className = `state-tag st-${d.state}`; st.textContent = LABEL[d.state] || d.state;
    document.querySelectorAll(".tabkey[data-tab]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.tab === monTab)));
    const body = $("mon-body");
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    if (monTab === "transcript") {
      const lines = d.tail || [];
      if (!lines.length) { body.replaceChildren(el("p", null, "NO TRANSCRIPT YET.")); return; }
      body.replaceChildren(...lines.map(e => { const n = el("div", `ln k-${e.kind}`); n.append(el("span", "ts", e.ts), el("span", "tx", lineText(e))); return n; }));
      if (atBottom || !body.dataset.shown) body.scrollTop = body.scrollHeight;
      body.dataset.shown = "1";
    } else {
      body.replaceChildren(el("pre", "packet", monData ? monData.packet : "LOADING…"));
    }
  }
  document.querySelectorAll(".tabkey[data-tab]").forEach(b => b.addEventListener("click", () => {
    monTab = b.dataset.tab; $("mon-body").dataset.shown = ""; renderMon(); $("mon-body").scrollTop = monTab === "packet" ? 0 : $("mon-body").scrollHeight;
  }));
  $("mon-close").addEventListener("click", closeMon);
  $("scrim").addEventListener("click", e => { if (e.target === $("scrim")) closeMon(); });
  document.addEventListener("keydown", e => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape" && monLane) { closeMon(); return; }
    if (e.key === "p" && monLane) { monTab = "packet"; renderMon(); return; }
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
  setInterval(() => { tickTimers(); if (state && state.gate.length) renderGate(); }, 1000);
})();
