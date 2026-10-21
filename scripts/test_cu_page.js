// Drives the control unit's script (scripts/dashboard-ui/cu.js) under node,
// with a small fake DOM, a fake server and fake timers: APPROVE on a truncated
// push asks a second question, CONFIRM stays dark for a second, CANCEL, a new
// fingerprint or the card going cancels it, and the viewer counts the files
// it doesn't show. A decided or expired card shows its state with both keys
// dark, and every refusal (each 409 reason, a 400) reaches the user as text.
// Run by test_dashboard_controls.py (CuScriptTests):
//   node scripts/test_cu_page.js scripts/dashboard-ui/cu.js
"use strict";
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const CU = process.argv[2];

class El {
  constructor(tag, id) {
    this.tagName = tag; this.id = id || ""; this.children = []; this._text = ""; this.className = "";
    this.hidden = false; this.disabled = false; this.value = ""; this.title = ""; this.attrs = {};
    this.listeners = {}; this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0; this.offsetTop = 0;
    const self = this;
    this.classList = {
      add: c => { const s = new Set(self.className.split(" ").filter(Boolean)); s.add(c); self.className = [...s].join(" "); },
      remove: c => { self.className = self.className.split(" ").filter(x => x && x !== c).join(" "); },
      toggle: (c, on) => { (on === undefined ? !self.className.split(" ").includes(c) : on) ? self.classList.add(c) : self.classList.remove(c); },
      contains: c => self.className.split(" ").includes(c),
    };
  }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  set textContent(t) { this._text = String(t); this.children = []; }
  get data() { return this._text; } set data(t) { this._text = t; }
  get length() { return this._text.length; }
  appendData(t) { this._text += t; } deleteData(a, n) { this._text = this._text.slice(0, a) + this._text.slice(a + n); }
  replaceChildren(...k) { this._text = ""; this.children = k; }
  append(...k) { for (const x of k) this.children.push(typeof x === "string" ? Object.assign(new El("#text"), { _text: x }) : x); }
  remove() {} focus() { doc.activeElement = this; } scrollIntoView() {} scrollBy() {}
  contains() { return false; }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  fire(t, ev) { for (const f of this.listeners[t] || []) f(Object.assign({ preventDefault() {}, target: this }, ev || {})); }
  click() { if (!this.disabled) this.fire("click"); }
}
const ids = new Map();
const doc = {
  hidden: false, activeElement: null, listeners: {},
  documentElement: new El("html"),
  getElementById: id => { if (!ids.has(id)) ids.set(id, new El("div", id)); return ids.get(id); },
  querySelector: sel => {
    const m = /name="([^"]+)"/.exec(sel);
    const v = { "cu-key": "k", "cu-main-port": "1234" }[m && m[1]];
    if (v === undefined) return null;
    const e = new El("meta"); e.attrs.content = v; return e;
  },
  createElement: t => new El(t), createTextNode: t => Object.assign(new El("#text"), { _text: String(t) }),
  addEventListener: (t, f) => { (doc.listeners[t] = doc.listeners[t] || []).push(f); },
};
const $ = id => doc.getElementById(id);

// Time and timers, under the test's control.
let now = 1_000_000, timers = [], tid = 0;
const realNow = Date.now;
Date.now = () => now;
const setT = (f, ms, every) => { const id = ++tid; timers.push({ id, at: now + (ms || 0), f, every }); return id; };
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    const t = timers[0];
    if (!t || t.at > end) break;
    now = t.at;
    if (t.every) t.at += t.every; else timers.shift();
    t.f();
    await flush();
  }
  now = end;
  await flush();
}
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };

// The server.
const calls = [];
let cards = [], patch = null, refuse = null;  // refuse: [status, body] for the next decide
function reply(name, body) {
  if (name === "state") return { cards, action: { name: null, phase: "idle" }, bound: true, project: "p", now: 0 };
  if (name === "decide") return { ok: true, id: body.id, verdict: body.verdict, text: "done" };
  if (name === "patch") return patch;
  return {};
}
const fetchFake = async (url, opts) => {
  const name = url.replace("/cu/api/", ""), body = JSON.parse(opts.body);
  calls.push([name, body]);
  if (name === "decide" && refuse) {
    const [status, out] = refuse;
    refuse = null;
    return { status, ok: false, json: async () => out };
  }
  return { status: 200, ok: true, json: async () => reply(name, body) };
};
const decisions = () => calls.filter(c => c[0] === "decide").map(c => c[1]);

const g = globalThis;
Object.assign(g, {
  document: doc, fetch: fetchFake, location: { hostname: "127.0.0.1" },
  setTimeout: (f, ms) => setT(f, ms), clearTimeout: id => { timers = timers.filter(t => t.id !== id); },
  setInterval: (f, ms) => setT(f, ms, ms),
});
g.window = { top: {}, parent: { postMessage() {} }, addEventListener() {} };

const card = (over) => Object.assign({ id: "c1", tool: "git_push", role: "developer", lanes: [], repo: "o/r", branch: "factory/x",
  commits: 1, file_count: 3, files: ["a.py"], seconds_left: 600, fp: "fp1", patch: true, patch_truncated: false }, over);

(async () => {
  vm.runInThisContext(fs.readFileSync(CU, "utf8"), { filename: CU });
  const results = [];
  const test = async (name, fn) => { await fn(); results.push(name); };

  // 1. Not truncated: APPROVE decides at once (after the hold on a new card).
  cards = [card()];
  await advance(2000);
  await advance(3000);
  assert.strictEqual($("btn-approve").disabled, false, "approve live");
  $("btn-approve").click(); await flush();
  assert.deepStrictEqual(decisions().map(d => [d.id, d.fp, d.verdict]), [["c1", "fp1", "approve"]]);
  results.push("plain approve");

  // 2. Truncated on the deck: ask, CONFIRM dark for a second, then decide.
  calls.length = 0;
  cards = [card({ id: "c2", fp: "fp2", patch_truncated: true })];
  await advance(2000); await advance(3000);
  $("btn-approve").click(); await flush();
  assert.strictEqual(decisions().length, 0, "the first press only asks");
  assert.strictEqual($("ask").hidden, false);
  assert.match($("ask").textContent, /^Approve without seeing the rest of the push\?/);
  assert.strictEqual($("lbl-approve").textContent, "Confirm approve");
  assert.strictEqual($("lbl-decline").textContent, "Cancel");
  assert.strictEqual($("btn-approve").disabled, true, "not armed yet");
  $("btn-approve").fire("click"); await flush();            // a double press, even past the disabled key
  assert.strictEqual(decisions().length, 0, "a double press can't confirm");
  await advance(1100);
  assert.strictEqual($("btn-approve").disabled, false, "armed");
  $("btn-approve").click(); await flush();
  assert.deepStrictEqual(decisions().map(d => [d.id, d.fp, d.verdict]), [["c2", "fp2", "approve"]]);
  assert.strictEqual($("ask").hidden, true);
  results.push("deck asks, arms, confirms");

  // 3. CANCEL backs out; a new fingerprint cancels too.
  calls.length = 0;
  cards = [card({ id: "c3", fp: "fp3", patch_truncated: true })];
  await advance(2000); await advance(3000);
  $("btn-approve").click(); await flush();
  $("btn-decline").click(); await flush();
  assert.strictEqual($("ask").hidden, true, "cancelled");
  assert.strictEqual($("lbl-approve").textContent, "Approve");
  $("btn-approve").click(); await flush();
  assert.strictEqual($("ask").hidden, false);
  cards = [card({ id: "c3", fp: "fp3b", patch_truncated: true })];
  await advance(2000);
  assert.strictEqual($("ask").hidden, true, "the card changed: the question is gone");
  await advance(3000);
  $("btn-approve").click(); await flush();
  assert.strictEqual(decisions().length, 0, "a press after the change only asks again");
  assert.strictEqual($("ask").hidden, false);
  cards = [];
  await advance(2000);
  assert.strictEqual($("ask").hidden, true, "the card went: the question is gone");
  assert.strictEqual(decisions().length, 0);
  results.push("cancel, fp change and gone cancel the question");

  // 4. The viewer: a truncated patch asks with the count of files not shown.
  calls.length = 0;
  cards = [card({ id: "c4", fp: "fp4" })];
  patch = { truncated: true, patch_bytes: 10, files_total: 3, files_shown: 1, files_missing: ["b.py"], files_missing_more: 1,
    commits: [{ sha: "a".repeat(40), author: "A", date: "d", message: "m", merge: false, flags: [], files: [
      { path: "a.py", old_path: null, status: "M", adds: 1, dels: 0, flags: [], hunks: [
        { header: "@@ -1 +1 @@", lines: [["+", "x"], ["✂", "CUT HERE · the rest of this push is not shown"]] }] }] }] };
  await advance(2000); await advance(3000);
  $("btn-inspect").click(); await flush();
  assert.strictEqual($("scr-inspect").hidden, false);
  assert.strictEqual($("pv-trunc").textContent, "TRUNCATED · showing 1 of 3 files");
  assert.strictEqual($("pv-missing").textContent, "NOT SHOWN: b.py … and 1 more file");
  assert.match($("pv-main").textContent, /✂ CUT HERE · the rest of this push is not shown/);
  $("pv-approve").click(); await flush();
  assert.strictEqual(decisions().length, 0);
  assert.match($("pv-ask").textContent, /^Approve without seeing 2 files\?/);
  $("pv-approve").fire("click"); await flush();
  assert.strictEqual(decisions().length, 0, "not armed");
  await advance(1100);
  $("pv-approve").click(); await flush();
  assert.deepStrictEqual(decisions().map(d => [d.id, d.fp, d.verdict]), [["c4", "fp4", "approve"]]);
  assert.strictEqual($("scr-inspect").hidden, true, "the viewer closed after deciding");
  assert.strictEqual(doc.activeElement, $("deck-lcd"), "focus back on the glass, never APPROVE");
  results.push("viewer asks with the count");

  // 5. In the viewer, the card changing cancels the question.
  calls.length = 0;
  cards = [card({ id: "c5", fp: "fp5" })];
  await advance(2000); await advance(3000);
  $("btn-inspect").click(); await flush();
  $("pv-approve").click(); await flush();
  assert.strictEqual($("pv-ask").hidden, false);
  cards = [card({ id: "c5", fp: "fp5b" })];
  await advance(2000);
  assert.strictEqual($("pv-ask").hidden, true);
  assert.strictEqual(decisions().length, 0);
  results.push("viewer: fp change cancels");

  // 6. A decided card: its state on the glass, both keys dark, the reason shut.
  calls.length = 0;
  const at = new Date(2026, 9, 21, 14, 2).getTime() / 1000;
  cards = [card({ id: "c6", fp: "fp6", decided: "approve", decided_by: "sam", decided_at: at })];
  await advance(2000); await advance(3000);
  assert.match($("deck-body").textContent, /^APPROVED, waiting for the agent's retryby sam at 14:02/);
  assert.strictEqual($("btn-approve").disabled, true, "approve dark on a decided card");
  assert.strictEqual($("btn-decline").disabled, true, "decline dark on a decided card");
  assert.strictEqual($("reason").disabled, true);
  assert.strictEqual($("keys-hold").hidden, false);
  assert.strictEqual($("keys-hold").textContent, "Approved · waiting for the agent's retry");
  assert.match($("btn-approve").title, /^Approved by sam at 14:02; waiting for the agent to retry$/);
  $("btn-approve").fire("click"); await flush();          // even past the disabled key
  $("reason").value = "no"; $("reason").fire("keydown", { key: "Enter" }); await flush();
  assert.strictEqual(decisions().length, 0, "nothing is sent for a decided card");
  assert.match($("flash").textContent, /^Already decided: Approved by sam/);
  cards = [card({ id: "c6", fp: "fp6", decided: "decline", decided_by: "sam", decided_at: at })];
  await advance(2000);
  assert.match($("deck-body").textContent, /^DECLINEDby sam at 14:02/);
  assert.strictEqual($("keys-hold").textContent, "Declined");
  assert.strictEqual($("btn-decline").disabled, true);
  results.push("decided card shows its state, keys dark");

  // 7. An expired card, and one that expires on the glass.
  calls.length = 0;
  cards = [card({ id: "c7", fp: "fp7", seconds_left: 0 })];
  await advance(2000); await advance(3000);
  assert.match($("deck-body").textContent, /^EXPIRED/);
  assert.strictEqual($("btn-approve").disabled, true);
  assert.strictEqual($("btn-decline").disabled, true);
  assert.strictEqual($("keys-hold").textContent, "Expired");
  cards = [card({ id: "c8", fp: "fp8", seconds_left: 9 })];
  await advance(2000); await advance(3000);
  assert.doesNotMatch($("deck-body").textContent, /EXPIRED/);
  assert.strictEqual($("btn-approve").disabled, false);
  doc.hidden = true;     // no polls (they would bring the same 9 s back): the clock runs out
  await advance(10000);
  doc.hidden = false;
  assert.match($("deck-body").textContent, /^EXPIRED/);
  assert.strictEqual($("btn-approve").disabled, true);
  assert.strictEqual($("btn-decline").disabled, true);
  assert.strictEqual(decisions().length, 0);
  results.push("expired card shows EXPIRED, keys dark");

  // 8. Every refusal is shown as text.
  const live = async (id) => { cards = [card({ id, fp: id })]; await advance(2000); await advance(3000); };
  const cases = [
    [409, { error: "decided", decided: "decline", decided_by: "kim", decided_at: at }, /^Already decided: Declined by kim at 14:02; waiting for the agent to retry\.$/],
    [409, { error: "gone" }, /^Gone: /],
    [409, { error: "changed" }, /^Changed: /],
    [409, { error: "expired" }, /^Expired: /],
    [409, { error: "ambiguous" }, /^Ambiguous: /],
    [400, { error: "give the agent a reason for declining" }, /^Refused \(400\): give the agent a reason for declining$/],
    [500, {}, /^Refused \(500\): refused \(HTTP 500\)$/],
  ];
  for (const [i, [status, body, want]] of cases.entries()) {
    await live(`r${i}`);
    refuse = [status, body];
    $("btn-approve").click(); await flush();
    assert.strictEqual(decisions().length, i + 1, `sent ${i}`);
    assert.strictEqual($("flash").hidden, false);
    assert.match($("flash").textContent, want);
    assert.strictEqual($("flash").className, "flash err");
  }
  results.push("refusals shown as text");

  // 9. DECLINE without a reason says so, and sends nothing.
  calls.length = 0;
  await live("d1");
  assert.strictEqual($("btn-decline").disabled, false, "decline takes the press");
  assert.strictEqual($("led-decline").className, "led", "but its lamp is dark");
  $("btn-decline").click(); await flush();
  assert.strictEqual(decisions().length, 0);
  assert.match($("flash").textContent, /^DECLINE needs a reason/);
  assert.ok($("reason").classList.contains("need"));
  $("reason").value = "too big"; $("reason").fire("input");
  assert.strictEqual($("led-decline").className, "led r");
  $("btn-decline").click(); await flush();
  assert.deepStrictEqual(decisions().map(d => [d.id, d.verdict, d.reason]), [["d1", "decline", "too big"]]);
  results.push("decline without a reason says so");

  // 10. Agent-written strings are text: markup stays literal.
  await live("x1");
  cards = [card({ id: "x1", fp: "x1", title: "<img src=x onerror=alert(1)>", decided: "approve",
    decided_by: "<b>evil</b>", decided_at: "soon" })];
  await advance(2000);
  assert.match($("deck-body").textContent, /“<img src=x onerror=alert\(1\)>”/);
  assert.match($("deck-body").textContent, /by <b>evil<\/b> at \?/);
  results.push("agent strings stay text");

  Date.now = realNow;
  console.log(JSON.stringify(results));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
