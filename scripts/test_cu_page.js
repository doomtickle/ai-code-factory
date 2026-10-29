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
let cards = [], patch = null, refuse = null, service = null, config = null, cfgReply = null;  // refuse: [status, body] for the next decide
function reply(name, body) {
  if (name === "state") return { cards, action: { name: null, phase: "idle" }, bound: true, project: "p", now: 0, service, config };
  if (name === "decide") return { ok: true, id: body.id, verdict: body.verdict, text: "done" };
  if (name === "patch") return patch;
  return {};
}
const fetchFake = async (url, opts) => {
  const name = url.replace("/cu/api/", ""), body = JSON.parse(opts.body);
  calls.push([name, body]);
  if (name.startsWith("config-") && cfgReply) {
    const [status, out] = await cfgReply(name, body);
    return { status, ok: status < 300, json: async () => out };
  }
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
const parent = { postMessage() {} }, listeners = [];
g.window = { top: {}, parent, addEventListener: (t, f) => { if (t === "message") listeners.push(f); } };
const ORIGIN = "http://127.0.0.1:1234";   // the main page, from the cu-main-port meta
const deliver = (data, over) => listeners.forEach(f => f(Object.assign({ source: parent, origin: ORIGIN, data }, over)));

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
  cards = [card({ id: "c6", fp: "fp6", queued: true, decided: "approve", decided_by: "sam", decided_at: at })];
  await advance(2000); await advance(3000);
  assert.match($("deck-body").textContent, /^APPROVED, the gateway writes it within secondsby sam at 14:02/);
  assert.strictEqual($("btn-approve").disabled, true, "approve dark on a decided card");
  assert.strictEqual($("btn-decline").disabled, true, "decline dark on a decided card");
  assert.strictEqual($("reason").disabled, true);
  assert.strictEqual($("keys-hold").hidden, false);
  assert.strictEqual($("keys-hold").textContent, "Approved · the gateway writes it");
  assert.match($("btn-approve").title, /^Approved by sam at 14:02; the gateway makes the write within seconds$/);
  $("btn-approve").fire("click"); await flush();          // even past the disabled key
  $("reason").value = "no"; $("reason").fire("keydown", { key: "Enter" }); await flush();
  assert.strictEqual(decisions().length, 0, "nothing is sent for a decided card");
  assert.match($("flash").textContent, /^Already decided: Approved by sam/);
  cards = [card({ id: "c6", fp: "fp6", queued: true, decided: "decline", decided_by: "sam", decided_at: at })];
  await advance(2000);
  assert.match($("deck-body").textContent, /^DECLINEDby sam at 14:02/);
  assert.match($("btn-approve").title, /^Declined by sam at 14:02; the gateway files it; nothing is written$/);
  cards = [card({ id: "c6", fp: "fp6", queued: false, decided: "approve", decided_by: "sam", decided_at: at })];
  await advance(2000);
  assert.match($("deck-body").textContent, /^APPROVED, but not staged: nothing to write/);
  cards = [card({ id: "c6", fp: "fp6", queued: true, decided: "decline", decided_by: "sam", decided_at: at })];
  await advance(2000);
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
    [409, { error: "decided", decided: "decline", decided_by: "kim", decided_at: at }, /^Already decided: Declined by kim at 14:02; the gateway files it; nothing is written\.$/],
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

  // 11. The header's SERVICE DUE lamp lights the key that applies it: UPDATE or RESTART, never both.
  const leds = () => [$("led-restart").className, $("led-update").className];
  assert.deepStrictEqual(leds(), ["led", "led"], "nothing due: both dark");
  service = "update"; await advance(2000);
  assert.deepStrictEqual(leds(), ["led", "led a"]);
  service = "restart"; await advance(2000);
  assert.deepStrictEqual(leds(), ["led a", "led"]);
  service = "bogus"; await advance(2000);
  assert.deepStrictEqual(leds(), ["led", "led"], "anything else lights nothing");
  service = null;
  results.push("service keys follow the lamp");

  // 12. The main page can ask which card to show ({type:"cu:select", id}): a display hint, nothing more.
  calls.length = 0;
  const two = (p = "s") => [card({ id: p + "1", fp: "f1", branch: "factory/one" }), card({ id: p + "2", fp: "f2", branch: "factory/two" })];
  const shown = () => $("sel-n").textContent;
  const sel = (id, over) => deliver({ type: "cu:select", id }, over);
  cards = two();
  await advance(2000); await advance(3000);
  assert.strictEqual(shown(), "1/2");
  assert.match($("deck-body").textContent, /factory\/one/);
  $("reason").value = "typed for the first card";
  doc.activeElement = $("deck-lcd");
  sel("s2"); await flush();
  assert.strictEqual(shown(), "2/2", "the asked-for card is on the glass");
  assert.match($("deck-body").textContent, /factory\/two/);
  assert.strictEqual($("reason").value, "", "a typed reason never carries over to another card");
  assert.strictEqual(doc.activeElement, $("deck-lcd"), "focus did not move, least of all onto a key");
  assert.strictEqual($("btn-approve").disabled, true, "the keys go dark, as for any card that arrives unasked");
  assert.strictEqual($("btn-decline").disabled, true);
  $("btn-approve").fire("click"); await flush();
  assert.strictEqual(decisions().length, 0, "a press at once decides nothing");
  await advance(3000);
  assert.strictEqual($("btn-approve").disabled, false);
  $("btn-approve").click(); await flush();
  assert.deepStrictEqual(decisions().map(d => [d.id, d.fp, d.verdict]), [["s2", "f2", "approve"]], "a press decides the card shown, with its own fingerprint");

  // Anything but exactly that, from the parent at the main origin, is ignored.
  calls.length = 0;
  cards = two("t");
  await advance(2000); await advance(3000);
  const stays = async (what, fn) => { await advance(500); const before = shown(); fn(); await flush(); assert.strictEqual(shown(), before, what); };
  await stays("wrong sender", () => sel("t2", { source: {} }));
  await stays("wrong origin", () => sel("t2", { origin: "http://127.0.0.1:9999" }));
  await stays("lookalike origin", () => sel("t2", { origin: ORIGIN + ".evil.test" }));
  await stays("wrong type", () => deliver({ type: "cu:other", id: "t2" }));
  await stays("an extra field", () => deliver({ type: "cu:select", id: "t2", fp: "f2" }));
  await stays("a missing field", () => deliver({ type: "cu:select" }));
  await stays("an array", () => deliver(["cu:select", "t2"]));
  await stays("a bare string", () => deliver("cu:select"));
  await stays("a null", () => deliver(null));
  await stays("an id with a path in it", () => sel("../t2"));
  await stays("an id with markup in it", () => sel("<b>t2</b>"));
  await stays("an id too long", () => sel("t".repeat(65)));
  await stays("an id that is not text", () => sel(2));
  await stays("an empty id", () => sel(""));
  assert.strictEqual(shown(), "1/2");
  assert.strictEqual($("flash").hidden, true, "a refused message says nothing");
  assert.strictEqual(calls.filter(c => c[0] !== "state").length, 0, "no request came of any of it");

  // A card this unit does not hold is not shown; it says so. At most one hint per 400 ms.
  await advance(500);
  sel("gone-card"); await flush();
  assert.strictEqual(shown(), "1/2");
  assert.strictEqual($("flash").textContent, "That write is no longer held.");
  $("flash").hidden = true;
  sel("t2"); await flush();
  assert.strictEqual(shown(), "1/2", "a second hint inside the gap is dropped");
  await advance(500);
  sel("t2"); await flush();
  assert.strictEqual(shown(), "2/2");
  await advance(500);
  sel("t2"); await flush();   // already on it: no new hold
  await advance(3000);
  assert.strictEqual($("btn-approve").disabled, false);
  await advance(500);
  sel("t2"); await flush();
  assert.strictEqual($("btn-approve").disabled, false, "the card already shown is no change, so no hold");

  // Not over a question being asked, a viewer or a decision in flight.
  cards = [card({ id: "u1", fp: "f1", patch_truncated: true }), card({ id: "u2", fp: "f2" })];
  await advance(2000); await advance(3000);
  await advance(500);
  const first = shown();
  $("btn-approve").click(); await flush();
  assert.strictEqual($("ask").hidden, false);
  sel("u2"); await flush();
  assert.strictEqual(shown(), first, "the question stays on its card");
  assert.strictEqual($("ask").hidden, false);
  $("btn-decline").click(); await flush();
  cards = two("v");
  await advance(2000); await advance(3000);
  await advance(500);
  $("btn-inspect").click(); await flush();
  assert.strictEqual($("scr-inspect").hidden, false);
  sel("v2"); await flush();
  assert.strictEqual(shown(), "1/2", "the viewer keeps its card");
  results.push("main page may only choose the card shown");


  // ---- SETUP tuning (#185): knobs, prepare, diff, confirm (the host side is mocked)
  const walk = (n, out = []) => { out.push(n); n.children.forEach(c => walk(c, out)); return out; };
  const kids = () => walk($("con-body"));
  const rack = role => kids().find(n => n.attrs["aria-label"] === `Tuning ${role}`);
  const inRack = (role, pred) => walk(rack(role)).find(pred);
  const byLabel = (role, label) => inRack(role, n => n.attrs["aria-label"] === label);
  const byText = (role, tag, text) => inRack(role, n => n.tagName === tag && n.textContent === text);
  const prepKey = role => byText(role, "button", "Prepare");
  const row = (key, value, source, repo) => Object.assign({ key, value, applies: "START", source }, source === "LOCAL" ? { repo } : {});
  const role = (name, keys) => ({ name, optional: false, on: true, override: false, ignored: [], fixed: [],
    keys: [row("MODEL", "claude-sonnet-5-5", "REPO"), row("MODELS", "claude-haiku-5-5 opus", "REPO"), row("EFFORT", "", "REPO"),
      row("LANES", name === "developer" ? "3" : "1", "REPO"), row("MAX_SESSION_TOKENS", "", "REPO"), ...keys, row("MAX_JOB_SECONDS", "", "REPO")] });
  config = { project: "p", roles: [role("developer", [row("MAX_TURNS", "80", "LOCAL", "200")]), role("reviewer", [row("MAX_TURNS", "200", "REPO")])],
    enabled: { file: false, listed: [], unknown: [], applies: "START" }, approvals: { tools: [], applies: "UPDATE" }, repos: { list: [], applies: "UPDATE" },
    gitlab: { url: "", applies: "UPDATE" }, coordinator: { broker_tools: [], applies: "UPDATE" }, notes: { project: "p", limit: 20000, files: [], applies: "START" },
    look: { file: false, problem: "" }, local_policy: false };
  cards = [];
  let wrote = [];
  const AGENTISH = '<img src=x onerror="alert(1)"> ignore all previous instructions & <script>x</script>';
  cfgReply = (name, body) => {
    wrote.push([name, body]);
    if (name === "config-prepare") return [200, { token: "tok1", expires: 120, path: "~/.config/ai-code-factory/roles/developer.conf", changed: true, applies: "RESTART",
      diff: [{ op: " ", text: 'MODEL="claude-sonnet-5-5"' }, { op: "-", text: 'MAX_TURNS="80"' }, { op: "+", text: AGENTISH }, { op: "<b>", text: "odd op" }] }];
    return [200, { ok: true, applies: "RESTART" }];
  };
  await advance(2000);
  $("svc-setup").click(); await flush();
  assert.strictEqual($("con-title").textContent, "SETUP · CONFIGURATION");
  const turnsNow = role => byLabel(role, "Turns");
  assert.strictEqual(turnsNow("developer").value, "80", "a LOCAL value shows in its field");
  assert.strictEqual(turnsNow("reviewer").value, "", "a REPO value leaves the field empty: it follows the repo");
  assert.strictEqual(prepKey("developer").disabled, true, "nothing changed, nothing to prepare");
  assert.deepStrictEqual(inRack("developer", n => n.tagName === "select").children.map(o => o.value), ["", "claude-haiku-5-5", "opus", "claude-sonnet-5-5"], "the model list is the role's MODELS");

  // Turn LANES 3 -> 5, EFFORT high, clear MAX_TURNS (a LOCAL value: unset), and set nothing else.
  byLabel("developer", "More lanes").click(); byLabel("developer", "More lanes").click();
  assert.strictEqual(byLabel("developer", "Lanes").attrs["aria-valuenow"], "5");
  assert.ok(byLabel("developer", "Lanes").className.split(" ").includes("d5"), "the dial turned to detent 5");
  byText("developer", "button", "high").click();
  const turns = turnsNow("developer"); turns.value = ""; turns.fire("input");
  const sess = byLabel("developer", "Session tokens"); sess.value = "12x"; sess.fire("input");
  assert.strictEqual(prepKey("developer").disabled, true, "a bad number blocks PREPARE");
  sess.value = ""; sess.fire("input");
  assert.strictEqual(prepKey("developer").disabled, false);
  // Stepping a knob back to the repo's value is no change.
  byLabel("reviewer", "More lanes").click(); byLabel("reviewer", "Fewer lanes").click();
  assert.strictEqual(prepKey("reviewer").disabled, true, "a knob put back where the repo has it changes nothing");
  calls.length = 0; wrote = [];
  prepKey("developer").click(); await flush();
  assert.deepStrictEqual(wrote, [["config-prepare", { kind: "role", role: "developer", set: { LANES: "5", EFFORT: "high" }, unset: ["MAX_TURNS"] }]],
    "only the changed keys are sent: the untouched MODEL and an emptied REPO field are not");
  results.push("tuning sends changed keys only");

  // The diff is drawn as text, in classes from a fixed map.
  assert.strictEqual($("con-title").textContent, "SETUP · TUNE DEVELOPER");
  const lines = kids().filter(n => n.tagName === "p" && n.className.startsWith("diff "));
  assert.deepStrictEqual(lines.map(n => n.className), ["diff k-dim", "diff k-err", "diff k-ok", "diff k-dim"]);
  assert.strictEqual(lines[2].textContent, `+ ${AGENTISH}`, "agent-looking markup stays text");
  assert.strictEqual(lines[3].textContent, "  odd op", "an unknown op is drawn as a plain line, never as a class");
  assert.ok(!kids().some(n => n.tagName === "img" || n.tagName === "script"), "no element came of it");
  assert.match($("con-body").textContent, /writes: ~\/\.config\/ai-code-factory\/roles\/developer\.conf/);
  assert.match($("con-body").textContent, /after RESTART/);
  results.push("tuning diff is text");

  // CONFIRM is dark for HOLD_MS; a click past the disabled key does nothing; one press sends the token once.
  assert.strictEqual($("con-go").textContent, "Confirm apply");
  assert.strictEqual($("con-go").disabled, true);
  $("con-go").fire("click"); await flush();
  assert.strictEqual(wrote.length, 1, "no config-run inside the hold");
  await advance(2400);
  $("con-go").fire("click"); await flush();
  assert.strictEqual(wrote.length, 1, "still inside the 2.5 s");
  await advance(300);
  assert.strictEqual($("con-go").disabled, false, "armed");
  calls.length = 0;
  // The host answers config-run only when the test lets it, and by then has written the override.
  let release; const gate = new Promise(r => { release = r; });
  const prepareReply = cfgReply;
  cfgReply = async (name, body) => {
    if (name !== "config-run") return prepareReply(name, body);
    wrote.push([name, body]);
    await gate;
    const dev = config.roles[0];
    dev.keys = dev.keys.map(k => k.key === "LANES" ? row("LANES", "5", "LOCAL", "3") : k.key === "EFFORT" ? row("EFFORT", "high", "LOCAL", "")
      : k.key === "MAX_TURNS" ? row("MAX_TURNS", "200", "REPO") : k);
    return [200, { ok: true, applies: "RESTART" }];
  };
  $("con-go").click(); await flush();
  $("con-go").fire("click"); await flush();
  // In flight: CANCEL and Esc are dead, so a write that goes through always shows its note.
  assert.strictEqual($("con-cancel").disabled, true, "CANCEL is dark while the write is in flight");
  $("con-cancel").fire("click"); await flush();
  const esc = { key: "Escape", preventDefault() {} };
  (doc.listeners.keydown || []).forEach(f => f(esc)); await flush();
  assert.strictEqual($("con-title").textContent, "SETUP · TUNE DEVELOPER", "Esc and CANCEL did not leave the diff");
  release(); await flush();
  assert.deepStrictEqual(wrote.slice(1), [["config-run", { token: "tok1" }]], "the token goes once");
  assert.match($("con-body").textContent, /SERVICE DUE · RESTART TO APPLY/);
  assert.strictEqual($("con-cancel").disabled, false, "CANCEL is back");
  assert.ok(calls.filter(c => c[0] === "state").length >= 1, "state is refreshed");
  assert.strictEqual($("con-title").textContent, "SETUP · CONFIGURATION");
  assert.strictEqual(turnsNow("developer").value, "", "the drafts are gone and the knobs show the written file: MAX_TURNS back on the repo's");
  assert.strictEqual(byLabel("developer", "Lanes").attrs["aria-valuenow"], "5", "LANES shows the new value");
  assert.strictEqual(byText("developer", "button", "high").className.split(" ").includes("on"), true, "EFFORT shows the new value");
  assert.strictEqual(prepKey("developer").disabled, true, "nothing left to prepare");
  results.push("tuning confirm holds, then sends the token once");

  // The file changed between prepare and run: 409 changed, said plainly, nothing retried.
  cfgReply = (name, body) => { wrote.push([name, body]); return name === "config-run" ? [409, { error: "changed" }] : [200, { token: "tok2", expires: 120, path: "p", changed: true, applies: "RESTART", diff: [{ op: "+", text: 'LANES="2"' }] }]; };
  wrote = [];
  byLabel("reviewer", "More lanes").click();
  prepKey("reviewer").click(); await flush();
  assert.deepStrictEqual(wrote[0][1], { kind: "role", role: "reviewer", set: { LANES: "2" }, unset: [] });
  assert.match($("con-body").textContent, /applies: after RESTART/);
  await advance(2600);
  $("con-go").click(); await flush();
  assert.match($("con-body").textContent, /file changed on disk after it was checked, so nothing was written\. Go back and prepare again/);
  assert.strictEqual($("con-go").hidden, true);
  $("con-cancel").click(); await flush();
  assert.strictEqual($("con-title").textContent, "SETUP · CONFIGURATION", "back on the knobs, not out of SETUP");
  assert.strictEqual(byLabel("reviewer", "Lanes").attrs["aria-valuenow"], "2", "the draft is kept, to prepare again");
  results.push("tuning 409 changed");

  // CANCEL on the diff drops the token and sends nothing.
  cfgReply = (name, body) => { wrote.push([name, body]); return [200, { token: "tok3", expires: 120, path: "p", changed: true, applies: "RESTART", diff: [{ op: "+", text: 'LANES="2"' }] }]; };
  wrote = [];
  prepKey("reviewer").click(); await flush();
  $("con-cancel").click(); await flush();
  await advance(3000);
  assert.deepStrictEqual(wrote.map(w => w[0]), ["config-prepare"], "no run came of a cancelled diff");
  assert.strictEqual($("con-go").hidden, true);
  results.push("tuning cancel drops the token");

  // Demo mode: prepare is refused with 403 and the rack says so, in a fixed line.
  cfgReply = (name, body) => [403, { error: "demo mode: this dashboard shows made-up data and nothing here acts" }];
  prepKey("reviewer").click(); await flush();
  assert.strictEqual($("con-title").textContent, "SETUP · CONFIGURATION");
  assert.ok(walk(rack("reviewer")).some(n => n.textContent === "DEMO · CHANGES DISABLED"));
  assert.ok(!$("con-body").textContent.includes("made-up data"), "the fixed line, not the server's text");
  // Another refusal shows its reason, as text.
  cfgReply = () => [400, { error: "LANES must be 1 to 8 <b>" }];
  prepKey("reviewer").click(); await flush();
  assert.ok(walk(rack("reviewer")).some(n => n.textContent === "✕ LANES must be 1 to 8 <b>"));
  results.push("tuning demo 403 line");
  $("con-cancel").click(); await flush();
  cfgReply = null; config = null;

  Date.now = realNow;
  console.log(JSON.stringify(results));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
