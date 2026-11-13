// Drives term.js under node (fake DOM, Terminal and WebSocket) to check the three fixed words the Thoughtfield's Terminal 0
// frame may tell the main page (t0:enter, t0:out, t0:still): only with the field skin's `tell` flag and a main-port meta,
// only to the exact main origin, nothing from the muted replay, a hidden tab or the classic skin, out at most every 500 ms,
// still 2 s after the last frame, and never a word that carries text, a count or a size.
// Run by test_dashboard_term.py (TermTellTests):  node scripts/test_term_tell.js scripts/dashboard-ui/term.js
"use strict";
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const TERM = process.argv[2];

async function load({ skin, port }) {
  const posts = [], timers = [], sent = [], hidden = { v: false };
  let clock = 1e6, T = null, sock = null;
  const el = () => ({ hidden: false, disabled: false, textContent: "", className: "", attrs: {}, classList: { toggle() {}, add() {}, contains: () => false },
    addEventListener() {}, focus() {}, remove() {}, getAttribute: () => null, contains: () => false });
  const ids = new Map(), $ = id => { if (!ids.has(id)) ids.set(id, el()); return ids.get(id); };
  const metas = { 'meta[name="term-key"]': "k" }; if (port != null) metas['meta[name="main-port"]'] = String(port);
  const doc = { readyState: "complete", get hidden() { return hidden.v; }, activeElement: null, getElementById: $, addEventListener() {},
    querySelector: sel => (sel in metas ? Object.assign(el(), { getAttribute: () => metas[sel] }) : null) };
  class Terminal { constructor(o) { T = this; this.options = { ...o }; this.cols = 80; this.rows = 24; this.parser = { registerOscHandler() {} }; }
    loadAddon() {} open() {} focus() {} reset() {} onData(f) { this.dataCb = f; } onBinary() {} onResize() {} write() {} hasSelection() { return false; }
    attachCustomKeyEventHandler() {} }
  class WebSocket { constructor() { sock = this; this.readyState = 1; } send(d) { sent.push(d); } }
  WebSocket.OPEN = 1;
  const win = { innerWidth: 1200, top: {}, parent: { postMessage: (m, o) => posts.push([m, o]) }, addEventListener() {}, navigator: { platform: "Win32", userAgent: "" } };
  win.window = win; if (skin) win.TermSkin = skin;
  const ctx = { window: win, document: doc, navigator: win.navigator, Terminal, FitAddon: { FitAddon: class { fit() {} } }, WebSocket, ResizeObserver: class { observe() {} },
    TextEncoder, TextDecoder, Uint8Array, atob, Promise, JSON, console, location: { host: "127.0.0.1:9", hostname: "127.0.0.1", protocol: "http:" },
    setTimeout: (f, ms) => { timers.push({ f, ms, at: clock + ms, dead: false }); return timers.length; }, clearTimeout: n => { if (n && timers[n - 1]) timers[n - 1].dead = true; },
    Date: { now: () => clock } };
  vm.createContext(ctx); vm.runInContext(fs.readFileSync(TERM, "utf8"), ctx, { filename: TERM });
  await new Promise(r => setImmediate(r));
  const state = running => sock.onmessage({ data: JSON.stringify({ type: "state", running }) });
  const frame = () => sock.onmessage({ data: new ArrayBuffer(8) });
  const advance = ms => { clock += ms; timers.filter(t => !t.dead && t.at <= clock).forEach(t => { t.dead = true; t.f(); }); };
  const words = () => posts.map(p => p[0]);
  return { T: () => T, sock: () => sock, posts, words, state, frame, advance, hidden, type: d => T.dataCb(d), tick: ms => { clock += ms; }, timers };
}
const FIELD = { tell: true, focusOnState: false };
const MAIN = "http://127.0.0.1:8765";

(async () => {
  const done = [];

  // Enter, to the exact main origin, only while running, and only a fixed word.
  let p = await load({ skin: FIELD, port: 8765 });
  p.type("\r"); assert.deepStrictEqual(p.posts, [], "Enter with the Coordinator off tells nothing");
  p.state(true);
  p.type("hello"); assert.deepStrictEqual(p.posts, [], "typed text tells nothing");
  p.type("\r");
  assert.deepStrictEqual(p.posts, [["t0:enter", MAIN]], "Enter tells t0:enter to the main origin and nothing else");
  p.hidden.v = true; p.type("\r"); p.hidden.v = false;
  assert.strictEqual(p.posts.length, 1, "a hidden tab tells nothing on Enter");
  done.push("enter");

  // Output: the echo of a key is ignored; later frames say out, at most every 500 ms; 2 s after the last frame, still.
  p = await load({ skin: FIELD, port: 8765 });
  p.state(true); p.type("a"); p.tick(100); p.frame();
  assert.deepStrictEqual(p.posts, [], "output within 300 ms of a key is the echo");
  p.tick(400); p.frame();
  assert.deepStrictEqual(p.words(), ["t0:out"]);
  p.tick(200); p.frame(); p.tick(200); p.frame();
  assert.deepStrictEqual(p.words(), ["t0:out"], "at most one out in 500 ms");
  p.tick(400); p.frame();
  assert.deepStrictEqual(p.words(), ["t0:out", "t0:out"], "another after 500 ms");
  p.advance(1900); assert.strictEqual(p.words().length, 2, "no still before 2 s of quiet");
  p.advance(200); assert.deepStrictEqual(p.words(), ["t0:out", "t0:out", "t0:still"], "still after 2 s with no output");
  p.advance(5000); assert.strictEqual(p.words().length, 3, "one still per out");
  for (const [m, o] of p.posts) { assert.match(m, /^t0:(enter|out|still)$/); assert.strictEqual(o, MAIN); }
  done.push("out and still");

  // The muted replay says nothing, however much of it there is; live output after it does.
  p = await load({ skin: FIELD, port: 8765 });
  p.state(true);
  p.sock().onmessage({ data: JSON.stringify({ type: "replay" }) });
  p.frame();
  p.advance(5000);
  assert.deepStrictEqual(p.posts, [], "the replay tells nothing");
  p.tick(1000); p.frame();
  assert.deepStrictEqual(p.words(), ["t0:out"], "live output after the replay does");
  done.push("replay says nothing");

  // A hidden tab says no out; the connection closing settles a lit out.
  p = await load({ skin: FIELD, port: 8765 });
  p.state(true); p.hidden.v = true; p.frame(); p.hidden.v = false;
  assert.deepStrictEqual(p.posts, [], "a hidden tab tells no out");
  p.frame(); assert.deepStrictEqual(p.words(), ["t0:out"]);
  p.sock().onclose(); assert.deepStrictEqual(p.words(), ["t0:out", "t0:still"], "a dropped connection settles");
  p = await load({ skin: FIELD, port: 8765 });
  p.state(true); p.frame(); p.state(false);
  assert.deepStrictEqual(p.words(), ["t0:out", "t0:still"], "the Coordinator going off settles");
  done.push("hidden and closed");

  // Nothing without the skin flag (the classic page), without the port meta, or with a bad one.
  for (const [name, opt] of [["classic skin", { skin: null, port: 8765 }], ["no flag", { skin: { focusOnState: false }, port: 8765 }],
                              ["no port", { skin: FIELD, port: null }], ["bad port", { skin: FIELD, port: 0 }], ["text port", { skin: FIELD, port: "x" }]]) {
    p = await load(opt);
    p.state(true); p.type("\r"); p.tick(1000); p.frame(); p.advance(5000);
    assert.deepStrictEqual(p.posts, [], name + " sends nothing");
  }
  done.push("classic sends nothing");
  console.log(JSON.stringify(done));
})().catch(e => { console.error(e.stack || e); process.exit(1); });
