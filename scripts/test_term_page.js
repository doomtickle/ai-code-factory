// Drives Terminal 0's page script (scripts/dashboard-ui/term.js) under node,
// with a fake DOM, a fake xterm.js Terminal and a fake WebSocket, to check the
// OSC 52 copy prompt (doomtickle/passdesk-ops#142): a copy only raises the
// prompt, and the clipboard is written only by a click on COPY; a query, bad
// base64 and an oversized copy raise nothing; a newer copy replaces the
// pending one; DISMISS discards it; the muted replay raises no prompt; a
// refused writeText falls back to a selected textarea; the page never reads
// the clipboard; a selection is copied trimmed, by the copy event and the
// copy keys, and plain Ctrl+C still goes to the Coordinator.
// Run by test_dashboard_term.py (TermScriptTests):
//   node scripts/test_term_page.js scripts/dashboard-ui/term.js [mac]
"use strict";
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const TERM = process.argv[2], MAC = process.argv[3] === "mac";

class El {
  constructor(id) {
    this.id = id; this.hidden = false; this.disabled = false; this.value = ""; this.textContent = "";
    this.className = ""; this.listeners = {}; this.attrs = {}; this.selected = false;
    const self = this;
    this.classList = {
      toggle: (c, on) => { const s = new Set(self.className.split(" ").filter(Boolean)); (on ? s.add(c) : s.delete(c)); self.className = [...s].join(" "); },
      add: c => self.classList.toggle(c, true),
      contains: c => self.className.split(" ").includes(c),
    };
  }
  set innerHTML(v) { throw new Error("innerHTML used"); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  click() { for (const f of this.listeners.click || []) f({ type: "click", preventDefault() {} }); }
  focus() { doc.activeElement = this; }
  select() { this.selected = true; }
  remove() {}
  contains(el) { return el === this || el === termTextarea; }
  getAttribute(k) { return this.attrs[k]; }
}
const ids = new Map();
const $ = id => { if (!ids.has(id)) ids.set(id, new El(id)); return ids.get(id); };
const termTextarea = new El("xterm-textarea");
let execResult = false, execCalls = 0;
const doc = {
  readyState: "complete", hidden: false, activeElement: null, listeners: {},
  getElementById: $,
  querySelector: () => { const m = new El("meta"); m.attrs.content = "k"; return m; },
  addEventListener: (t, f, capture) => { (doc.listeners[t] = doc.listeners[t] || []).push(f); },
  execCommand: cmd => {
    execCalls++;
    assert.strictEqual(cmd, "copy");
    if (!execResult) return false;
    fireCopy(doc.activeElement === termTextarea || doc.activeElement === term0() ? termTextarea : doc.activeElement);
    return true;
  },
};
let copied = [];  // what copy events put on their clipboard data
function fireCopy(target) {
  const ev = { target, clipboardData: { setData: (type, v) => copied.push([type, v]) }, prevented: false,
    preventDefault() { this.prevented = true; }, stopImmediatePropagation() {} };
  for (const f of doc.listeners.copy || []) f(ev);
  return ev;
}

// The fake xterm.js Terminal.
let T = null;
const term0 = () => T;
class Terminal {
  constructor(opts) { T = this; this.opts = opts; this.options = { ...opts }; this.osc = {}; this.cols = 80; this.rows = 24;
    this.sel = ""; this.keyHandler = null; this.writes = []; this.focused = 0;
    this.parser = { registerOscHandler: (n, f) => { assert(!this.osc[n], "one handler"); this.osc[n] = f; } }; }
  loadAddon() {} open() {} focus() { this.focused++; doc.activeElement = termTextarea; } reset() {}
  onData() {} onBinary() {} onResize() {}
  write(bytes, cb) { this.writes.push(cb); }
  hasSelection() { return this.sel !== ""; } getSelection() { return this.sel; } clearSelection() { this.sel = ""; }
  attachCustomKeyEventHandler(f) { this.keyHandler = f; }
}
let sock = null;
class WebSocket { constructor() { sock = this; this.readyState = 1; this.sent = []; } send(d) { this.sent.push(d); } }
WebSocket.OPEN = 1;

// The clipboard: writes are recorded, and any read fails the test.
let writes = [], writeFails = false, activation = false;
const clipboard = {
  writeText: t => { assert(activation, "clipboard written outside a click"); writes.push(t);
    return writeFails ? Promise.reject(new Error("NotAllowedError")) : Promise.resolve(); },
  readText: () => { throw new Error("clipboard read"); },
  read: () => { throw new Error("clipboard read"); },
};
const win = {
  innerWidth: 1200, top: {}, addEventListener() {},
  navigator: { platform: MAC ? "MacIntel" : "Win32", userAgent: "", clipboard },
};
const ctx = {
  window: win, document: doc, navigator: win.navigator, Terminal, FitAddon: { FitAddon: class { fit() {} } },
  WebSocket, ResizeObserver: class { observe() {} }, TextEncoder, TextDecoder, Uint8Array, atob, Promise,
  setTimeout: (f, ms) => 0, clearTimeout() {}, location: { host: "127.0.0.1:9" }, JSON, console,
};
ctx.window.window = win;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(TERM, "utf8"), ctx, { filename: TERM });

const b64 = s => Buffer.from(s, "utf8").toString("base64");
const osc = data => T.osc[52](data);
const click = id => { activation = true; try { $(id).click(); } finally { activation = false; } };
const tick = () => new Promise(r => setImmediate(r));
const done = [];

(async () => {
  await tick();  // start(): fonts ready, connect()
  assert(sock, "connected");
  assert.strictEqual(T.opts.macOptionClickForcesSelection, true);
  assert.strictEqual(T.opts.macOptionIsMeta, true);
  assert(T.osc[52], "OSC 52 handler registered");
  sock.onmessage({ data: JSON.stringify({ type: "state", running: true }) });
  const box = $("copy");
  // The copy element starts hidden in term.html; the fake starts visible, so hide it.
  box.hidden = true;

  // A copy raises the prompt and writes nothing.
  assert.strictEqual(osc("c;" + b64("git push origin factory/x")), true);
  assert.strictEqual(box.hidden, false);
  assert.strictEqual($("copy-title").textContent, "COORDINATOR COPIED 25 CHARACTERS · PRESS TO COPY");
  assert.strictEqual($("copy-preview").textContent, "git push origin factory/x");
  assert.deepStrictEqual(writes, []);
  // A newer one replaces it; only the click writes, and the newest.
  osc(";" + b64("ls -la\nexit"));
  assert.strictEqual($("copy-preview").textContent, "ls -la⏎ exit");
  assert.deepStrictEqual(writes, []);
  click("copy-go");
  await tick();
  assert.deepStrictEqual(writes, ["ls -la\nexit"]);
  assert.strictEqual($("copy-title").textContent, "COPIED TO THE CLIPBOARD");
  click("copy-go");  // nothing pending any more
  await tick();
  assert.strictEqual(writes.length, 1);
  done.push("copy only on click, newest wins");

  // Refused: a query, an empty payload, no target separator, bad base64, bad UTF-8, controls, too long.
  box.hidden = true;
  for (const bad of ["c;?", "?", "c;", "c", b64("x"), "c;@@@@", "c;" + Buffer.from([0xff, 0xfe]).toString("base64"),
    "c;" + b64("a\u001b[31mb"), "c;" + b64("x".repeat(64 * 1024 + 1))]) {
    assert.strictEqual(osc(bad), true, "always handled");
    assert.strictEqual(box.hidden, true, `prompt raised by ${bad.slice(0, 20)}`);
  }
  assert.strictEqual(sock.sent.filter(d => typeof d !== "string").length, 0, "a query is never answered");
  done.push("queries and bad payloads refused");

  // DISMISS discards.
  osc("c;" + b64("rm -rf build"));
  click("copy-x");
  assert.strictEqual(box.hidden, true);
  click("copy-go");
  await tick();
  assert.strictEqual(writes.length, 1);
  done.push("dismiss discards");

  // The muted replay raises nothing; live output afterwards does.
  sock.onmessage({ data: JSON.stringify({ type: "replay" }) });
  sock.onmessage({ data: new ArrayBuffer(4) });
  osc("c;" + b64("old command"));
  assert.strictEqual(box.hidden, true);
  T.writes.pop()();  // the replay has been parsed
  osc("c;" + b64("new command"));
  assert.strictEqual(box.hidden, false);
  done.push("replay raises no prompt");

  // Preview: hidden characters made visible and flagged, long text cut.
  osc("c;" + b64("echo‮ hi​"));
  assert.strictEqual($("copy-preview").textContent, "echo⟨U+202E⟩ hi⟨U+200B⟩");
  assert.strictEqual($("copy-note").hidden, false);
  osc("c;" + b64("y".repeat(1000)));
  assert.strictEqual(Array.from($("copy-preview").textContent).length, 240);
  assert.strictEqual($("copy-note").hidden, true);
  assert.strictEqual($("copy-title").textContent, "COORDINATOR COPIED 1000 CHARACTERS · PRESS TO COPY");
  done.push("preview is visible text");

  // writeText refused: the textarea fallback copies, else stays selected for the user.
  writeFails = true; execResult = false;
  osc("c;" + b64("make test"));
  click("copy-go");
  await tick();
  assert.strictEqual($("copy-manual").hidden, false);
  assert.strictEqual($("copy-manual").value, "make test");
  assert($("copy-manual").selected);
  assert.match($("copy-title").textContent, /BLOCKED/);
  osc("c;" + b64("make lint"));
  assert.strictEqual($("copy-manual").hidden, true, "a new copy clears the fallback");
  assert.strictEqual(doc.activeElement, termTextarea, "and hands the keys back to the terminal");
  writeFails = false;
  done.push("fallback when writeText is refused");

  // A selection: the copy event gets it trimmed, before xterm.js's own handler.
  T.sel = "  run this   \nand this  ";
  let ev = fireCopy(termTextarea);
  assert(ev.prevented);
  assert.deepStrictEqual(copied.pop(), ["text/plain", "  run this\nand this"]);
  ev = fireCopy($("copy-manual"));  // a copy outside the terminal is left alone
  assert(!ev.prevented);
  T.sel = "";
  assert(!fireCopy(termTextarea).prevented);
  done.push("selection copied trimmed");

  // Keys. Not a Mac: Ctrl+Shift+C and Ctrl+Insert copy a selection; Ctrl+C and no selection pass through.
  // A Mac: Cmd+C is left to the browser's copy event (above); the others pass through.
  const key = (k, mods) => Object.assign({ type: "keydown", key: k, code: k.length === 1 ? "Key" + k.toUpperCase() : k,
    ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, preventDefault() {} }, mods);
  execResult = true;
  const execBefore = execCalls;
  T.sel = "abc  ";
  assert.strictEqual(T.keyHandler(key("c", { ctrlKey: true })), true, "Ctrl+C goes to the Coordinator");
  if (MAC) {
    assert.strictEqual(T.keyHandler(key("c", { metaKey: true })), false, "Cmd+C: xterm.js stays out");
    assert.strictEqual(execCalls, execBefore, "the browser copies, not the page");
    assert.strictEqual(T.keyHandler(key("C", { ctrlKey: true, shiftKey: true })), true);
    assert.strictEqual(T.keyHandler(key("Insert", { ctrlKey: true })), true);
    T.sel = "";
    assert.strictEqual(T.keyHandler(key("c", { metaKey: true })), true, "no selection: passed through");
  } else {
    assert.strictEqual(T.keyHandler(key("C", { ctrlKey: true, shiftKey: true })), false);
    assert.deepStrictEqual(copied.pop(), ["text/plain", "abc"]);
    assert.strictEqual(T.sel, "", "selection cleared");
    T.sel = "xyz";
    assert.strictEqual(T.keyHandler(key("Insert", { ctrlKey: true })), false);
    assert.deepStrictEqual(copied.pop(), ["text/plain", "xyz"]);
    assert.strictEqual(T.keyHandler(key("C", { ctrlKey: true, shiftKey: true })), true, "no selection: passed through");
    T.sel = "q";
    assert.strictEqual(T.keyHandler(key("c", { metaKey: true })), true, "Cmd+C means nothing off a Mac");
    T.sel = "";
  }
  assert.strictEqual(T.keyHandler(key("C", { ctrlKey: true, shiftKey: true, type: "keyup" })), true, "only keydown");
  done.push("copy keys");

  assert.strictEqual(writes.length, 2, "one copy and one refused attempt, both from clicks");
  process.stdout.write(JSON.stringify(done) + "\n");
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
