// The Thoughtfield's plain words for an order (all the jobs on one branch): each step's number and state, the order's one-line state,
// the history the hover card lists, and where an order's label goes in the sky. Pure functions of data the page already has
// (/api/field jobs and /api/state held writes): no DOM, no request, nothing spoken. Every string it returns is a fixed word or an
// agent-written one, and the caller draws all of them with textContent; the tone is a key of the fixed table below, the only thing
// that ever becomes a class name.
"use strict";
(function (root) {
  const hhmm = m0 => { const m = ((m0 % 1440) + 1440) % 1440; return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(Math.floor(m % 60)).padStart(2, "0"); };
  const clockMin = t => { const m = /^(\d{1,2}):(\d{2})/.exec(String(t || "")); return m ? +m[1] * 60 + +m[2] : null; };
  const BAD = /^(failed|cancelled|interrupted)$/;
  // tone -> the plain state: running, done, approved, changes requested, failed, waiting for you
  const TONES = Object.assign(Object.create(null), { run: "Running", done: "Done", ok: "Approved", rev: "Changes requested", bad: "Failed", wait: "Waiting for you" });
  const BAD_WORD = Object.assign(Object.create(null), { failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted" });
  const cut = (s, n) => { const t = String(s == null ? "" : s); return t.length > n ? t.slice(0, n - 1).trimEnd() + "…" : t; };

  function jobTone(j) { if (j.status === "running") return "run"; if (BAD.test(j.status)) return "bad"; if (j.verdict === "approved") return "ok"; if (j.verdict === "revise") return "rev"; return "done"; }
  function cardTone(c) { return !c.decided ? "wait" : c.decided === "approve" ? "ok" : "rev"; }
  const stepTone = s => s.card ? cardTone(s.card) : jobTone(s.job);

  // an order's steps, numbered by start time (a held write is the step after the last job)
  function number(jobs, cards) {
    const steps = jobs.slice().sort((a, b) => a.start - b.start || (a.i || 0) - (b.i || 0)).map(job => ({ job }));
    (cards || []).forEach(card => steps.push({ card }));
    steps.forEach((s, k) => { s.n = k + 1; s.tone = stepTone(s); });
    return steps;
  }

  // the order's state in words, from its newest step (a write waiting for you comes first)
  function orderWord(steps) {
    if (!steps.length) return { tone: "done", word: "" };
    const held = steps.find(s => s.card && !s.card.decided); if (held) return { tone: "wait", word: "Waiting for you" };
    const last = steps[steps.length - 1];
    if (last.card) return last.card.decided === "approve" ? { tone: "ok", word: "Going out" } : { tone: "rev", word: "Declined" };
    const j = last.job, tone = last.tone, review = j.role === "reviewer";
    if (tone === "run") { const before = steps.slice(0, -1).reverse().find(s => s.job && s.job.role === "reviewer" && s.job.verdict); return { tone, word: review ? "Reviewing" : before && before.job.verdict === "revise" ? "Fixing" : "Working" }; }
    if (tone === "bad") return { tone, word: BAD_WORD[j.status] || "Failed" };
    if (tone === "ok") return { tone, word: "Approved" };
    if (tone === "rev") return { tone, word: "Changes requested" };
    return { tone, word: review ? "Reviewed" : "Done" };
  }
  // one step's state in words: the tone's plain word, or for a job that stopped, how it stopped
  const stepWord = s => s.job && s.tone === "bad" ? BAD_WORD[s.job.status] || "Failed" : s.card && s.card.decided ? (s.card.decided === "approve" ? "Approved, going out" : "Declined") : TONES[s.tone];
  // an order's name: its branch, or for a job that stands alone (the order is named by its own id) the job's words
  function orderName(key, label, steps) {
    const own = steps.some(s => s.job && s.job.id === key);
    return cut((own || !key || key.charAt(0) === "(" ? label : key) || label || "", 26);
  }
  // the order's place in the queue for a label: waiting, then running, then failed, then the rest newest first
  const TIER = Object.assign(Object.create(null), { wait: 0, run: 1, bad: 2, rev: 3, ok: 3, done: 3 });
  function rank(orders) { // [{id, tone, newest}] -> the same, most important first; each gets .prio (0 is first)
    const out = orders.slice().sort((a, b) => TIER[a.tone] - TIER[b.tone] || b.newest - a.newest || (a.id < b.id ? -1 : 1)); out.forEach((o, k) => { o.prio = k; }); return out;
  }

  // the history the hover card shows: one row for each start and each end, oldest first, the hovered step marked
  const MAX_STEPS = 8;
  function history(steps, at, stagedAt) {
    let lo = 0, hi = steps.length;
    if (steps.length > MAX_STEPS) { lo = Math.max(0, Math.min(steps.length - MAX_STEPS, at - MAX_STEPS + 2)); hi = lo + MAX_STEPS; }
    const rows = [];
    steps.slice(lo, hi).forEach(s => { const cur = s === steps[at], n = s.n;
      if (s.card) { const c = s.card, t = stagedAt ? stagedAt(c.id) : null; rows.push({ n, t, lane: "", what: !c.decided ? "write held, waiting for you" : c.decided === "approve" ? "write approved, going out" : "write declined", tone: s.tone, cur, end: false, k: rows.length }); return; }
      const j = s.job, review = j.role === "reviewer";
      rows.push({ n, t: j.start, lane: j.lane, what: review ? "review started" : "work started", tone: s.tone, cur, end: false, k: rows.length });
      if (j.status !== "running" && j.end != null) rows.push({ n: 0, t: j.end, lane: "", what: BAD.test(j.status) ? BAD_WORD[j.status].toLowerCase() : review ? (j.verdict === "approved" ? "approved" : j.verdict === "revise" ? "asked for changes" : "review finished") : "finished", tone: s.tone, cur, end: true, k: rows.length });
    });
    rows.sort((a, b) => (a.t == null ? 1e9 : a.t) - (b.t == null ? 1e9 : b.t) || a.k - b.k);
    return { rows, before: lo, after: steps.length - hi };
  }

  // ---- where the labels go. items (most important first): {id, w, h, anchors: [{x, y, r}] newest crystal first, force}
  // env: {W, top, rimAt(x), cx, cy, boxes: [[x0, y0, x1, y1]] the crystals, blocked([x0, y0, x1, y1]) the witness}
  // A label sits beside its newest crystal on the side away from the witness, else above or below it, else beside its order's other
  // crystals, else in the nearest clear sky (far: true, the page draws a thread to its crystal). When nothing is clear a running
  // or waiting order (force) takes the place that covers least, and a lower one that can't be placed hides every one after it,
  // so the hidden labels are always the least important ones. (The last resort may cover the witness, but only when no clear sky is left, as on a phone.)
  const GAP = 8, PAD = 4, OUT = 5;
  const hits = (a, b, p) => a[0] < b[2] + p && b[0] < a[2] + p && a[1] < b[3] + p && b[1] < a[3] + p;
  function placeLabels(items, env) {
    const at = new Map(), hidden = [], placed = [], boxes = env.boxes.map(b => [b[0] - OUT, b[1] - OUT, b[2] + OUT, b[3] + OUT]);
    const bounds = r => r[0] >= PAD && r[2] <= env.W - PAD && r[1] >= env.top && r[3] <= Math.min(env.rimAt(r[0]), env.rimAt((r[0] + r[2]) / 2), env.rimAt(r[2])) - GAP, inside = r => bounds(r) && !env.blocked(r);
    const clear = r => inside(r) && !boxes.some(b => hits(r, b, 0)) && !placed.some(b => hits(r, b, PAD));
    const rect = (x, y, w, h) => [x, y, x + w, y + h];
    const area = (a, b) => Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0])) * Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));
    function near(it, A) {
      const { w, h } = it, dx = A.x - env.cx, dy = A.y - env.cy, out = dx < 0 ? -1 : 1, up = dy < 0 ? -1 : 1;
      const side = s => [s < 0 ? A.x - A.r - GAP - w : A.x + A.r + GAP, A.y - h / 2], vert = s => [A.x - w / 2, s < 0 ? A.y - A.r - GAP - h : A.y + A.r + GAP];
      const order = Math.abs(dx) * .8 >= Math.abs(dy) ? [side(out), vert(up), vert(-up), side(-out)] : [vert(up), side(out), side(-out), vert(-up)], list = [];
      order.forEach((p, k) => { list.push(p); if (k % 2 === 0 === (Math.abs(dx) * .8 >= Math.abs(dy))) { list.push([p[0], p[1] - h * .95], [p[0], p[1] + h * .95]); } else { list.push([p[0] - w * .4 * out, p[1]], [p[0] + w * .4 * out, p[1]]); } });
      return list.map(p => rect(p[0], p[1], w, h));
    }
    function far(it, A, ok) { for (let d = A.r + 28; d <= 460; d += 24) { const n = Math.max(12, Math.round(d / 9)); for (let k = 0; k < n; k++) { const a = (k / n) * 6.2832 + .4, r = rect(A.x + Math.cos(a) * d - it.w / 2, A.y + Math.sin(a) * d - it.h / 2, it.w, it.h); if (ok(r)) return r; } } return null; }
    let cut = false;
    items.forEach(it => {
      if (cut && !it.force) { hidden.push(it.id); return; }
      let found = null, isFar = false, anchor = it.anchors[0];
      for (const A of it.anchors) { for (const r of near(it, A)) if (clear(r)) { found = r; anchor = A; break; } if (found) break; }
      if (!found) { found = far(it, it.anchors[0], clear); isFar = !!found; }
      if (!found && it.force) { let best = null, bestA = 1e18; const tryR = r => { if (!bounds(r)) return; const a = boxes.reduce((s, b) => s + area(r, b), 0) + placed.reduce((s, b) => s + area(r, b), 0) * 2 + (env.blocked(r) ? 300 : 0); if (a < bestA) { bestA = a; best = r; } };
        it.anchors.forEach(A => near(it, A).forEach(tryR)); far(it, it.anchors[0], r => { tryR(r); return false; }); found = best; isFar = !!best; }
      if (!found) { hidden.push(it.id); cut = true; return; }
      placed.push(found); at.set(it.id, { x: found[0], y: found[1], far: isFar, anchor });
    });
    return { at, hidden };
  }

  // Terminal 0's pane has three sizes. paneSize(st, act) is the one place that decides the next one; "before" is the size Full screen returns to.
  // acts: key (the Terminal key or T), bigger (the Bigger/Smaller button), full (the Full screen/Exit button), esc, into (the frame took focus), away (the world or the control unit did).
  const PANE_SIZES = ["docked", "grown", "full"];
  function paneSize(st, act) {
    const size = PANE_SIZES.includes(st && st.size) ? st.size : "docked", before = size === "full" && st.before === "grown" ? "grown" : "docked";
    if (size === "full") return act === "key" || act === "full" || act === "esc" ? { size: before, before } : { size, before };
    if (act === "full") return { size: "full", before: size };
    const next = act === "key" || act === "bigger" ? (size === "docked" ? "grown" : "docked") : act === "into" ? "grown" : act === "esc" || act === "away" ? "docked" : size;
    return { size: next, before: next };
  }

  const api = { hhmm, paneSize, clockMin, TONES, jobTone, cardTone, number, orderWord, stepWord, orderName, rank, history, placeLabels, cut, MAX_STEPS };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.FieldOrder = api;
})(typeof window !== "undefined" ? window : globalThis);
