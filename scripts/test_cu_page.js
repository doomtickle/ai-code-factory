// Drives the control unit's script (scripts/dashboard-ui/cu.js) under node,
// with a small fake DOM, a fake server and fake timers: APPROVE on a truncated
// push asks a second question, CONFIRM stays dark for a second, CANCEL, a new
// fingerprint or the card going cancels it, and the viewer counts the files
// it doesn't show. Run by test_dashboard_controls.py (CuScriptTests):
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
let cards = [], patch = null;
function reply(name, body) {
  if (name === "state") return { cards, action: { name: null, phase: "idle" }, bound: true, project: "p", now: 0 };
  if (name === "decide") return { ok: true, id: body.id, verdict: body.verdict, text: "done" };
  if (name === "patch") return patch;
  return {};
}
const fetchFake = async (url, opts) => {
  const name = url.replace("/cu/api/", ""), body = JSON.parse(opts.body);
  calls.push([name, body]);
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

  Date.now = realNow;
  console.log(JSON.stringify(results));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
