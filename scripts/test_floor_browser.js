// Drives the factory floor in headless Chrome (the sandbox's chrome-headless-shell, over
// the DevTools pipe) against a running `factory dashboard --demo`. Run by
// test_floor_browser.py:   node scripts/test_floor_browser.js <dashboard url with token>
// Checks, for every station (developers and reviewers): nothing lies over it (the topmost
// element at its centre is the station), a click opens that lane's monitor, Enter and Space
// do too, and it wears glasses. Job titles wider than the console glass slide, clipped to it;
// short ones and everything under prefers-reduced-motion stay still (ellipsis + tooltip).
// It also types the Konami code to check the factory radio badge (radio.js) shows, hides and remembers.
// The attention signals (#192, app.js) are driven by rewriting the /api/state the page polls: the title and
// favicon badge, the cues' events, the jobs-done count clearing on focus, and the NOTIFY key (a stubbed
// Notification: permission asked only from the key, a body of fixed words and validated names only).
// The SHIFT BUDGET (#195): the amber/red thresholds of the header's spend readout, the meter, and the stored figure.
"use strict";
const { spawn } = require("node:child_process");
const assert = require("node:assert");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");

async function launch(url, media, init) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "floor-test."));
  const child = spawn(process.env.FACTORY_SHOT_CHROME || "chrome-headless-shell", ["--headless", "--no-sandbox", "--disable-gpu",
    "--disable-dev-shm-usage", "--hide-scrollbars", "--no-first-run", "--disable-extensions", "--remote-debugging-pipe",
    `--user-data-dir=${profile}`, "--window-size=1280,900", "about:blank"], { stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"] });
  let next = 1, buf = "";
  const pending = new Map(), listeners = [], contexts = new Map();
  listeners.push(m => {
    if (m.method === "Runtime.executionContextCreated") contexts.set(m.params.context.id, m.params.context);
    else if (m.method === "Runtime.executionContextDestroyed") contexts.delete(m.params.executionContextId);
    else if (m.method === "Runtime.executionContextsCleared") contexts.clear();
  });
  child.stdio[4].on("data", c => {
    buf += c;
    let i;
    while ((i = buf.indexOf("\0")) >= 0) {
      const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1);
      if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); }
      else if (m.method) listeners.slice().forEach(f => f(m));
    }
  });
  const send = (method, params = {}, sessionId) => {
    const id = next++;
    child.stdio[3].write(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }) + "\0");
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  };
  const { targetInfos } = await send("Target.getTargets");
  const { sessionId } = await send("Target.attachToTarget", { targetId: targetInfos.find(t => t.type === "page").targetId, flatten: true });
  const s = (m, p) => send(m, p, sessionId);
  await s("Page.enable"); await s("Runtime.enable");
  await s("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  if (media) await s("Emulation.setEmulatedMedia", { features: media });
  if (init) await s("Page.addScriptToEvaluateOnNewDocument", { source: init });
  const loaded = new Promise(r => { const f = m => { if (m.method === "Page.loadEventFired") { listeners.splice(listeners.indexOf(f), 1); r(); } }; listeners.push(f); });
  await s("Page.navigate", { url });
  await loaded;
  return {
    sleep: ms => new Promise(r => setTimeout(r, ms)),
    async ev(expr) {
      const r = await s("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
      return r.result.value;
    },
    // Evaluate in the framed control unit (a second origin on 127.0.0.1): the page itself can't reach into it.
    async evFrame(expr) {
      const pageOrigin = new URL(url).origin;
      const c = [...contexts.values()].find(x => x.origin !== pageOrigin && x.origin.startsWith("http://127.0.0.1:") && x.auxData && x.auxData.isDefault);
      if (!c) throw new Error("no framed control unit");
      const r = await s("Runtime.evaluate", { expression: expr, contextId: c.id, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
      return r.result.value;
    },
    async click(x, y) {
      for (const type of ["mouseMoved", "mousePressed", "mouseReleased"])
        await s("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons: type === "mousePressed" ? 1 : 0, clickCount: 1 });
    },
    async key(key, code, text) {
      await s("Input.dispatchKeyEvent", { type: "keyDown", key, code, ...(text ? { text } : {}) });
      await s("Input.dispatchKeyEvent", { type: "keyUp", key, code });
    },
    async close() {
      child.kill("SIGKILL");
      await new Promise(r => child.once("exit", r));
      try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 }); } catch (e) { /* Chrome still flushing its profile: a leftover temp folder is harmless */ }
    },
  };
}

async function floorOn(b) {
  for (let i = 0; i < 40 && !(await b.ev(`document.querySelectorAll(".lane").length > 0`)); i++) await b.sleep(250);
  await b.ev(`document.getElementById("floor-key").click()`);
  for (let i = 0; i < 40 && !(await b.ev(`document.querySelectorAll(".fl .st").length >= 5`)); i++) await b.sleep(250);
  await b.sleep(500);
}

// The page's own poll, with /api/state rewritten by window.__mut(state) and counted in window.__polls.
const HOOK = `(() => { const real = window.fetch.bind(window); window.__polls = 0; window.__mut = null;
  window.fetch = async (u, o) => { const r = await real(u, o); if (!String(u).startsWith("/api/state")) return r;
    const j = await r.json(); if (window.__mut) window.__mut(j); window.__polls++; return new Response(JSON.stringify(j), { status: 200 }); };
  window.__events = []; for (const n of ["held", "done"]) document.addEventListener("factory-attention:" + n, () => window.__events.push(n));
  window.__asked = 0; window.__notes = [];
  window.Notification = class { constructor(t, o) { window.__notes.push([t, o && o.body]); } close() {} static permission = "default";
    static requestPermission(cb) { window.__asked++; Notification.permission = "granted"; if (cb) cb("granted"); return Promise.resolve("granted"); } };
  document.hasFocus = () => false; })()`;

async function attention(url) {
  const b = await launch(url, undefined, HOOK);
  try {
    const poll = async () => { const n = await b.ev(`window.__polls`); for (let i = 0; i < 40 && (await b.ev(`window.__polls`)) <= n; i++) await b.sleep(250); };
    const title = () => b.ev(`document.title`);
    for (let i = 0; i < 40 && !(await b.ev(`window.__polls > 0`)); i++) await b.sleep(250);
    await b.sleep(300);
    assert.strictEqual(await title(), "(1 held) LOIS FC-1984", "the demo's held write badges the title");
    assert.strictEqual(await b.ev(`window.__asked`), 0, "permission is never asked on load");
    assert.deepStrictEqual(await b.ev(`window.__events`), [], "the first poll's state is history, not an event");
    const icon = await b.ev(`document.querySelector('link[rel="icon"]').href`);
    assert.ok(icon.startsWith("data:image/png;base64,"), "the favicon is a canvas-drawn data URL");
    const pixel = (x, y) => b.ev(`new Promise(r => { const i = new Image(); i.onload = () => { const c = document.createElement("canvas"); c.width = c.height = 32;
      const g = c.getContext("2d"); g.drawImage(i, 0, 0); r([...g.getImageData(${x}, ${y}, 1, 1).data]); }; i.src = document.querySelector('link[rel="icon"]').href; })`);
    const [r, g] = await pixel(27, 27);
    assert.ok(r > 200 && g < 100, `the favicon carries a red held badge (${r},${g})`);

    // A job finishing while the tab is behind another window counts, and focus clears it.
    await b.ev(`(() => { window.__phase = "running"; window.__mut = j => { const l = j.lanes[0]; l.state = window.__phase; if (window.__name) l.name = window.__name;
      for (const x of (window.__extra || [])) j.gate.push(Object.assign({}, j.gate[0], x)); }; return null; })()`);
    await poll();
    await b.ev(`window.__phase = "done"`);
    await poll(); await b.sleep(100);
    assert.strictEqual(await title(), "(1 held · 1 done) LOIS FC-1984", "a finished job adds to the badge");
    assert.deepStrictEqual(await b.ev(`window.__events`), ["done"]);
    await b.ev(`document.hasFocus = () => true; window.dispatchEvent(new Event("focus"))`);
    assert.strictEqual(await title(), "(1 held) LOIS FC-1984", "focus clears the finished-jobs count");
    await b.ev(`window.__phase = "running"`); await poll();
    await b.ev(`window.__phase = "done"`); await poll();
    assert.strictEqual(await title(), "(1 held) LOIS FC-1984", "a job that ends while the tab is watched isn't counted");
    assert.deepStrictEqual(await b.ev(`window.__events`), ["done", "done"], "...but still makes its cue");

    // A new held write: the cue fires once for it, the badge counts both.
    await b.ev(`window.__extra = [{ id: "demo-extra-1", tool: "git_push" }]`);
    await poll(); await b.sleep(100);
    assert.strictEqual(await title(), "(2 held) LOIS FC-1984");
    assert.deepStrictEqual(await b.ev(`window.__events.filter(e => e === "held")`), ["held"]);
    await poll();
    assert.deepStrictEqual(await b.ev(`window.__events.filter(e => e === "held")`), ["held"], "a card already seen doesn't cue again");

    // NOTIFY: nothing until the key is pressed, then a notification only while the tab is behind.
    assert.strictEqual(await b.ev(`document.getElementById("notify-key").getAttribute("aria-pressed")`), "false");
    await b.ev(`document.getElementById("notify-key").click()`); await b.sleep(100);
    assert.deepStrictEqual(await b.ev(`[window.__asked, document.getElementById("notify-key").getAttribute("aria-pressed"), localStorage.getItem("fc1984-notify")]`), [1, "true", "1"]);
    await b.ev(`window.__extra = [{ id: "demo-extra-1", tool: "git_push" }, { id: "demo-extra-2", tool: "<img src=x onerror=alert(1)>" }]`);
    await poll(); await b.sleep(100);
    assert.deepStrictEqual(await b.ev(`window.__notes`), [], "no notification while the tab is focused");
    await b.ev(`document.hasFocus = () => false; window.__extra = [{ id: "demo-extra-1", tool: "git_push" }, { id: "demo-extra-2", tool: "<img src=x onerror=alert(1)>" }, { id: "demo-extra-3", tool: "gitlab_create_mr" }]`);
    await poll(); await b.sleep(100);
    assert.deepStrictEqual(await b.ev(`window.__notes`), [["LOIS", "Write held · gitlab_create_mr"]], "a fixed tool name, nothing else");
    await b.ev(`window.__extra = [{ id: "demo-extra-1", tool: "git_push" }, { id: "demo-extra-2" }, { id: "demo-extra-3" }, { id: "demo-extra-4", tool: "<b>evil</b>" }]`);
    await poll(); await b.sleep(100);
    assert.deepStrictEqual((await b.ev(`window.__notes`))[1], ["LOIS", "Write held · write"], "a tool outside the fixed list is just a write");
    await b.ev(`window.__name = "Evil<b>name"; window.__phase = "running"`); await poll();
    await b.ev(`window.__phase = "done"`); await poll(); await b.sleep(100);
    assert.deepStrictEqual((await b.ev(`window.__notes`))[2], ["LOIS", "Job finished · lane"], "a lane name outside the pattern is just a lane");
    await b.ev(`window.__name = null; window.__phase = "running"`); await poll();
    await b.ev(`window.__phase = "done"`); await poll(); await b.sleep(100);
    const notes = await b.ev(`window.__notes`);
    assert.ok(/^Job finished · [a-z][a-z0-9-]+$/.test(notes[3][1]), `a lane name: ${notes[3]}`);
    assert.ok(notes.every(n => n[0] === "LOIS" && /^(Write held · [a-z_]+|Job finished · [a-z][a-z0-9-]*)$/.test(n[1])), `notification bodies: ${JSON.stringify(notes)}`);
    await b.ev(`document.getElementById("notify-key").click()`);
    assert.strictEqual(await b.ev(`localStorage.getItem("fc1984-notify")`), "0", "the key turns notifications off and remembers it");
  } finally { await b.close(); }
}

// The event printer (#190): lines are buttons; JOB opens the lane's monitor on that job's packet, STAGED points the
// control unit at its card by postMessage (one validated message, Enter or click, never a decision key), DENY opens in
// place to its whole text and Esc shuts it again.
const PRINTER = `(() => { const real = window.fetch.bind(window); window.__lane = []; window.__long = ""; window.__more = false;
  window.fetch = async (u, o) => { if (String(u).startsWith("/api/lane")) window.__lane.push(String(u)); const r = await real(u, o);
    if (!String(u).startsWith("/api/state")) return r; const j = await r.json();
    if (window.__long) j.events.push({ t: "23:59:59", kind: "DENY", red: true, text: window.__long }, { t: "23:59:59", kind: "FAILED", red: true, text: "git_push o/r factory/x · " + window.__long, card_id: "demo0001push" });
    if (window.__more) j.events.push({ t: "23:59:59", kind: "ALLOW", red: false, text: "a newer line" });
    return new Response(JSON.stringify(j), { status: 200 }); }; })()`;

async function printer(url) {
  const b = await launch(url, undefined, PRINTER);
  try {
    for (let i = 0; i < 40 && !(await b.ev(`document.querySelectorAll("#paper button.pl").length > 3`)); i++) await b.sleep(250);
    for (let i = 0; i < 40; i++) {
      try { if (await b.evFrame(`!document.getElementById("scr-deck").hidden && document.getElementById("deck-body").textContent.length > 0`)) break; } catch (e) { /* not loaded yet */ }
      await b.sleep(250);
    }
    await b.evFrame(`window.__msgs = []; window.addEventListener("message", e => window.__msgs.push([e.origin, JSON.stringify(e.data), e.source === window.parent]))`);
    const msgs = () => b.evFrame(`window.__msgs`);
    const line = (kind, has = "") => `[...document.querySelectorAll("#paper button.pl")].find(n => n.children[1].textContent === ${JSON.stringify(kind)} && n.textContent.includes(${JSON.stringify(has)}))`;
    const focusLine = (kind, has) => b.ev(`(() => { const n = ${line(kind, has)}; n.scrollIntoView({ block: "nearest" }); n.focus(); return document.activeElement === n; })()`);
    const origin = new URL(url).origin;

    assert.strictEqual(await b.ev(`[...document.querySelectorAll("#paper .pl")].every(n => n.tagName === "BUTTON")`), true, "printer lines are buttons");

    // STAGED: Enter on the focused line sends exactly one validated message; the card shown is that card.
    assert.ok(await focusLine("STAGED", "greet-empty-name"));
    await b.key("Enter", "Enter", "\r"); await b.sleep(300);
    assert.deepStrictEqual(await msgs(), [[origin, '{"type":"cu:select","id":"demo0001push"}', true]]);
    assert.match(await b.evFrame(`document.getElementById("deck-body").textContent`), /factory\/greet-empty-name/);
    assert.ok(!["btn-approve", "btn-decline"].includes(await b.evFrame(`document.activeElement && document.activeElement.id`)), "focus is on no decision key");
    assert.strictEqual(await b.ev(`document.activeElement.children[1].textContent`), "STAGED", "focus stays on the printer line");

    // WRITTEN for a card no longer held: the unit says so and shows nothing new.
    await b.sleep(500);
    assert.ok(await focusLine("WRITTEN"));
    await b.key("Enter", "Enter", "\r"); await b.sleep(300);
    assert.strictEqual((await msgs()).length, 2);
    assert.strictEqual(await b.evFrame(`document.getElementById("flash").textContent`), "That write is no longer held.");

    // JOB: a mouse click opens that lane's monitor on that job's packet.
    await b.ev(`(() => { const n = ${line("JOB")}; n.scrollIntoView({ block: "nearest" }); return null; })()`);
    const at = await b.ev(`(() => { const r = ${line("JOB")}.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()`);
    await b.click(at[0], at[1]); await b.sleep(500);
    assert.strictEqual(await b.ev(`document.getElementById("scrim").hidden`), false, "the monitor opened");
    const lane = await b.ev(`window.__lane`);
    assert.ok(lane.length && /^\/api\/lane\?name=[a-z][a-z0-9-]*&job=[0-9]{8}-[0-9a-z-]+$/.test(lane[lane.length - 1]), `lane request: ${lane}`);
    assert.strictEqual((await msgs()).length, 2, "a JOB line tells the unit nothing");
    await b.key("Escape", "Escape"); await b.sleep(200);
    assert.strictEqual(await b.ev(`document.getElementById("scrim").hidden`), true);

    // DENY and FAILED open in place to the whole reason, as text; Esc shuts them.
    const long = "bundle does not exist in the workspace <img src=x onerror=alert(1)> " + "and then some more words to be sure it is long ".repeat(12);
    await b.ev(`window.__long = ${JSON.stringify(long)}`);
    for (let i = 0; i < 40 && !(await b.ev(`!!${line("FAILED", "onerror")}`)); i++) await b.sleep(250);
    // The newest DENY is the mutated one: it is the last in the DOM.
    await b.ev(`(() => { const all = [...document.querySelectorAll("#paper button.pl")].filter(n => n.children[1].textContent === "DENY"); all[all.length - 1].focus(); return null; })()`);
    assert.strictEqual(await b.ev(`document.activeElement.textContent.includes("onerror=alert(1)")`), true);
    assert.strictEqual(await b.ev(`document.activeElement.getAttribute("aria-expanded")`), "false");
    assert.strictEqual((await b.ev(`(() => { const t = document.activeElement.children[2]; return t.scrollWidth > t.clientWidth; })()`)), true, "shut: the reason is cut off");
    await b.key("Enter", "Enter", "\r"); await b.sleep(200);
    assert.deepStrictEqual(await b.ev(`(() => { const n = document.activeElement, t = n.children[2]; return [n.getAttribute("aria-expanded"), t.scrollWidth <= t.clientWidth, getComputedStyle(t).whiteSpace, t.textContent === ${JSON.stringify(long)}, n.querySelector("img")]; })()`),
      ["true", true, "normal", true, null], "open: wrapped, whole, still text");
    assert.strictEqual((await msgs()).length, 2, "a DENY line tells the unit nothing");
    await b.key("Escape", "Escape"); await b.sleep(200);
    assert.strictEqual(await b.ev(`document.activeElement.getAttribute("aria-expanded")`), "false", "Esc shuts it");
    assert.strictEqual(await b.ev(`document.getElementById("scrim").hidden`), true, "...and opens nothing");
    // It stays open across the printer redrawing for a new line.
    await b.key("Enter", "Enter", "\r"); await b.sleep(200);
    await b.ev(`window.__more = true`);
    for (let i = 0; i < 40 && !(await b.ev(`!![...document.querySelectorAll("#paper button.pl")].find(n => n.textContent.includes("a newer line"))`)); i++) await b.sleep(250);
    assert.deepStrictEqual(await b.ev(`(() => { const n = document.activeElement; return [n.getAttribute("aria-expanded"), n.textContent.includes("onerror=alert(1)"), n.classList.contains("open")]; })()`),
      ["true", true, true], "a redraw for a new line keeps the line open and focused");
    await b.key("Escape", "Escape"); await b.sleep(100);

    // FAILED: opening it also points the unit at its card; shutting it does not.
    await b.sleep(500);
    await b.ev(`(() => { const all = [...document.querySelectorAll("#paper button.pl")].filter(n => n.children[1].textContent === "FAILED"); const n = all[all.length - 1]; if (n.classList.contains("open")) n.click(); n.focus(); return null; })()`);
    const before = (await msgs()).length;
    await b.sleep(500);
    await b.key("Enter", "Enter", "\r"); await b.sleep(300);
    const after = await msgs();
    assert.strictEqual(after.length, before + 1);
    assert.strictEqual(after[after.length - 1][1], '{"type":"cu:select","id":"demo0001push"}');
  } finally { await b.close(); }
}

// SHIFT BUDGET (#195): a dollars-a-day figure on the spend module, kept in this browser with the knob values, tints the
// header's spend readout amber past it and red past twice it, and fills a meter. Display only. The page's own poll is
// rewritten so today's spend is whatever window.__cost says.
const BUDGET = `(() => { const real = window.fetch.bind(window); window.__polls = 0; window.__cost = 10;
  window.fetch = async (u, o) => { const r = await real(u, o); if (!String(u).startsWith("/api/state")) return r;
    const j = await r.json(); j.totals.cost = window.__cost; window.__polls++; return new Response(JSON.stringify(j), { status: 200 }); }; })()`;

async function budget(url) {
  const b = await launch(url, undefined, BUDGET);
  try {
    const poll = async () => { const n = await b.ev(`window.__polls`); for (let i = 0; i < 40 && (await b.ev(`window.__polls`)) <= n; i++) await b.sleep(250); await b.sleep(100); };
    const look = () => b.ev(`(() => { const box = document.getElementById("ro-spend-box"), m = document.getElementById("budget-meter");
      return { cls: [box.classList.contains("over"), box.classList.contains("over2")], ink: getComputedStyle(box.querySelector(".seg .on")).fill,
        meter: m.className.replace("budget-meter", "").trim(), width: m.firstChild.style.width, text: document.getElementById("budget-v").textContent, tip: box.title }; })()`);
    // Layout, not just classes: the meter must have room to show and the words beside it must not be clipped.
    const fits = () => b.ev(`(() => { const m = document.getElementById("budget-meter"), v = document.getElementById("budget-v"), r = m.getBoundingClientRect(), f = m.firstChild.getBoundingClientRect(),
      box = document.getElementById("budget").getBoundingClientRect(), lab = document.querySelector("#budget label"), inp = document.getElementById("budget-in").getBoundingClientRect();
      return { meter: r.width, high: r.height, fill: f.width, valueClipped: v.scrollWidth > v.clientWidth, labelClipped: lab.scrollWidth > lab.clientWidth,
        inside: r.right <= box.right + 0.5 && v.getBoundingClientRect().right <= box.right + 0.5 && inp.right <= box.right + 0.5 }; })()`);
    const roomy = async (what) => { const f = await fits();
      assert.ok(f.meter >= 60 && f.high >= 4, `${what}: the meter shows (${f.meter} x ${f.high}px)`);
      assert.ok(!f.valueClipped && !f.labelClipped, `${what}: the words aren't clipped`);
      assert.ok(f.inside, `${what}: everything stays inside the module`); return f; };
    const stored = () => b.ev(`JSON.parse(localStorage.getItem("fc1984-knobs") || "{}")`);
    const reload = async () => { await b.ev(`location.reload()`); await b.sleep(500); for (let i = 0; i < 40 && !(await b.ev(`window.__polls > 0`)); i++) await b.sleep(250); await b.sleep(200); };
    const typed = v => b.ev(`(() => { const i = document.getElementById("budget-in"); i.value = ${JSON.stringify(v)}; i.dispatchEvent(new Event("input")); return null; })()`);
    const setCost = async v => { await b.ev(`window.__cost = ${v}`); await poll(); };
    for (let i = 0; i < 40 && !(await b.ev(`window.__polls > 0`)); i++) await b.sleep(250);

    // No budget: nothing tints, whatever the spend.
    await setCost(500);
    let l = await look();
    assert.deepStrictEqual([l.cls, l.meter, l.width, l.text, l.tip], [[false, false], "none", "0%", "no budget", ""], "no budget: the readout stays plain");
    const plainInk = l.ink;
    await roomy("no budget");

    // Type 50 with the real keys; digits and letters in the box are not lane or console keys.
    await b.ev(`document.getElementById("budget-in").focus()`);
    await b.key("5", "Digit5", "5"); await b.key("0", "Digit0", "0"); await b.key("f", "KeyF", "f");
    assert.strictEqual(await b.ev(`document.getElementById("budget-in").value`), "50f", "the keys went into the box");
    assert.strictEqual(await b.ev(`document.getElementById("floor-key").getAttribute("aria-pressed")`), "false", "f typed in the box doesn't turn the floor on");
    assert.strictEqual(await b.ev(`document.getElementById("scrim").hidden`), true, "a digit typed in the box opens no monitor");
    assert.strictEqual(await b.ev(`document.getElementById("budget-in").getAttribute("aria-invalid")`), "true", "junk is flagged");
    assert.strictEqual((await stored()).budget, 50, "junk doesn't replace the last good figure");
    await typed("50");
    assert.strictEqual(await b.ev(`document.getElementById("budget-in").getAttribute("aria-invalid")`), null);
    assert.strictEqual((await stored()).budget, 50, "50 is stored");

    // The thresholds: amber only past the figure (not at it), red only past twice it (not at it).
    const cases = [[0, [false, false], "ok"], [49.99, [false, false], "ok"], [50, [false, false], "ok"], [50.01, [true, false], "over"],
      [99.99, [true, false], "over"], [100, [true, false], "over"], [100.01, [false, true], "over2"], [500, [false, true], "over2"], [10, [false, false], "ok"]];
    for (const [cost, cls, name] of cases) {
      await setCost(cost);
      l = await look();
      assert.deepStrictEqual(l.cls, cls, `spend ${cost} against 50: classes`);
      assert.strictEqual(l.meter, name === "ok" ? "" : name, `spend ${cost}: meter colour`);
      assert.strictEqual(l.ink !== plainInk, name !== "ok", `spend ${cost}: the digits change colour only when tinted`);
    }
    // The header shows cents, so the thresholds compare cents: $50.004 reads $50.00 and stays calm.
    await setCost(50.004);
    assert.deepStrictEqual([(await look()).cls], [[false, false]], "50.004 against 50 is not over");
    await setCost(100.004);
    assert.deepStrictEqual([(await look()).cls], [[true, false]], "100.004 against 50 is not past twice it");
    await setCost(50.006);
    assert.deepStrictEqual([(await look()).cls], [[true, false]], "50.006 reads $50.01 and is over");
    await setCost(75);
    l = await look();
    assert.deepStrictEqual([l.width, l.text, l.tip], ["75%", "$75.00 / $50.00", "Past the shift budget of $50.00"]);
    assert.strictEqual(await b.ev(`document.getElementById("budget-meter").getAttribute("aria-valuetext")`), "$75.00 of $50.00 a day");
    const half = await roomy("budget set");
    assert.ok(Math.abs(half.fill - half.meter * 0.75) < 2, `the fill is 75% of the meter's real width (${half.fill} of ${half.meter})`);
    const amberInk = l.ink;
    await setCost(1234.5);
    await roomy("a long figure");
    await setCost(120);
    l = await look();
    assert.deepStrictEqual([l.width, l.tip], ["100%", "Past twice the shift budget of $50.00"]);
    assert.notStrictEqual(l.ink, amberInk, "red digits differ from amber ones");

    // It is stored with the knob values, and a knob turn keeps it.
    await b.ev(`(() => { const k = document.querySelector('.knob[data-knob="crt"]'); k.focus(); return null; })()`);
    await b.key("ArrowUp", "ArrowUp"); await b.sleep(100);
    const both = await stored();
    assert.deepStrictEqual([both.budget, both.crt], [50, 4], "budget and knob values share the stored blob");
    await reload();
    assert.strictEqual(await b.ev(`document.getElementById("budget-in").value`), "50", "remembered across a reload");
    await setCost(75);
    assert.deepStrictEqual((await look()).cls, [true, false], "...and in force");

    // 0 or empty: no budget again. Arrow keys step it.
    await typed("");
    l = await look();
    assert.deepStrictEqual([l.cls, l.meter, (await stored()).budget], [[false, false], "none", 0], "empty means no budget");
    await b.ev(`document.getElementById("budget-in").focus()`);
    await b.key("ArrowUp", "ArrowUp"); await b.sleep(100);
    assert.strictEqual((await stored()).budget, 5, "Up steps by 5");
    await typed("0");
    assert.deepStrictEqual([(await look()).cls, (await stored()).budget], [[false, false], 0], "0 means no budget");
    await typed("$12.345");
    assert.strictEqual((await stored()).budget, 0, "three decimals are refused");
    await typed("9999999");
    assert.strictEqual((await stored()).budget, 0, "over the cap is refused");
    await typed("$12.5");
    assert.strictEqual((await stored()).budget, 12.5, "a leading $ and cents are fine");
    await b.ev(`document.getElementById("budget-in").blur()`);
    assert.strictEqual(await b.ev(`document.getElementById("budget-in").value`), "12.5");

    // A bad stored figure is ignored (and the knob values around it still load).
    for (const bad of ['"x"', "-5", "1e9", "null", "[1]", "true"]) {
      await b.ev(`localStorage.setItem("fc1984-knobs", '{"crt":7,"budget":${bad.replace(/'/g, "")}}')`);
      await reload();
      assert.strictEqual(await b.ev(`document.getElementById("budget-in").value`), "", `stored ${bad}: no budget`);
      await setCost(500);
      assert.deepStrictEqual((await look()).cls, [false, false], `stored ${bad}: nothing tints`);
      assert.strictEqual(await b.ev(`document.querySelector('.knob[data-knob="crt"]').getAttribute("aria-valuenow")`), "7", `stored ${bad}: the knobs still load`);
    }
    await b.ev(`localStorage.setItem("fc1984-knobs", "not json")`);
    await reload();
    assert.strictEqual(await b.ev(`document.getElementById("budget-in").value`), "", "unparseable storage: no budget, no crash");
  } finally { await b.close(); }
}

async function run(url) {
  // ---- motion allowed
  let b = await launch(url);
  try {
    await floorOn(b);
    const names = await b.ev(`[...document.querySelectorAll(".fl .st")].map(g => g.querySelector(".name").textContent)`);
    assert.ok(names.some(n => n.startsWith("developer")) && names.some(n => n.startsWith("reviewer")), `developer and reviewer stations: ${names}`);

    // The factory radio: hidden until the Konami code is typed, off again with the code or the badge.
    const radio = () => b.ev(`(() => { const k = document.getElementById("radio-key"); return [k.hidden, k.getAttribute("aria-pressed"), localStorage.getItem("fc1984-radio")]; })()`);
    const konami = async (...keys) => { for (const k of keys) await b.key(k, k.length === 1 ? "Key" + k.toUpperCase() : k, k.length === 1 ? k : undefined); await b.sleep(100); };
    const CODE = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"];
    assert.deepStrictEqual(await radio(), [true, "false", null], "the radio badge starts hidden");
    await konami(...CODE.slice(0, 9), "x", "a");
    assert.strictEqual((await radio())[0], true, "a wrong key starts the code over");
    await konami("ArrowUp", ...CODE);
    assert.deepStrictEqual(await radio(), [false, "true", "1"], "the Konami code, after an extra Up, turns the radio on and shows the badge");
    await b.ev(`document.getElementById("radio-key").click()`);
    assert.deepStrictEqual(await radio(), [true, "false", "0"], "the badge turns the radio off and hides itself");
    await konami(...CODE);
    await konami(...CODE);
    assert.deepStrictEqual(await radio(), [true, "false", "0"], "the code toggles the radio");

    const stations = await b.ev(`[...document.querySelectorAll(".fl .st")].map((g, i) => {
      const r = g.querySelector(".hit").getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2, top = document.elementFromPoint(x, y);
      return { i, label: g.getAttribute("aria-label"), x, y, covered: !top || top.closest(".st") !== g };
    })`);
    for (const st of stations) {
      const lane = st.label.replace(/^Open lane /, "").split(",")[0];
      assert.ok(!st.covered, `something lies over the ${lane} station: it can't take a click`);
      const shown = () => b.ev(`!document.getElementById("scrim").hidden && document.getElementById("mon-title").textContent`);
      const close = async () => { await b.ev(`document.getElementById("mon-close").click()`); await b.sleep(100); };
      await b.click(st.x, st.y); await b.sleep(150);
      assert.strictEqual(await shown(), `MONITOR · ${lane}`, `a click on ${lane} opens its monitor`);
      await close();
      for (const [key, code, text] of [["Enter", "Enter", "\r"], [" ", "Space", " "]]) {
        await b.ev(`document.querySelectorAll(".fl .st")[${st.i}].focus()`);
        await b.key(key, code, text); await b.sleep(150);
        assert.strictEqual(await shown(), `MONITOR · ${lane}`, `${code} on ${lane} opens its monitor`);
        await close();
      }
    }

    const looks = await b.ev(`[...document.querySelectorAll(".fl .st")].map(g => {
      const lenses = [...g.querySelectorAll(".hd .shade")].map(n => { const r = n.getBoundingClientRect(); return [r.width, r.height, getComputedStyle(n).fill]; });
      const job = g.querySelector(".card-t"), tag = g.querySelector(".card-tag");
      const slides = n => n.getAnimations().filter(a => a.effect.getKeyframes().some(k => k.transform && k.transform !== "none"));
      return { lane: g.querySelector(".name").textContent, lenses, text: job.textContent, wide: job.getComputedTextLength(),
        slides: slides(job).length, tagSlides: slides(tag).length, tip: g.querySelector("title").textContent, clip: g.querySelector(".card").getAttribute("clip-path") };
    })`);
    assert.ok(looks.length >= 5);
    for (const l of looks) {
      assert.strictEqual(l.lenses.length, 2, `${l.lane} wears two lenses`);
      for (const [w, h, fill] of l.lenses) assert.ok(w > 2 && h > 1.5 && fill !== "none" && !/rgba\(0, 0, 0, 0\)/.test(fill), `${l.lane}: a visible lens (${w}x${h} ${fill})`);
      assert.ok(/^url\(["']?#fl-card-\d+["']?\)$/.test(l.clip), `${l.lane}: the placard is clipped to the glass`);
    }
    const wide = looks.filter(l => l.text && l.wide > 34), narrow = looks.filter(l => l.text && l.wide <= 34);
    assert.ok(wide.length, "the demo has a job title wider than the glass");
    assert.ok(narrow.length, "the demo has a job title that fits");
    for (const l of wide) assert.strictEqual(l.slides, 1, `${l.lane}: "${l.text}" slides`);
    for (const l of narrow) assert.strictEqual(l.slides + l.tagSlides, 0, `${l.lane}: "${l.text}" stays still`);
    assert.ok(looks.some(l => l.lane.startsWith("reviewer") && l.text), "a reviewer placard is drawn");
    assert.ok(wide.every(l => !l.text.includes("…")), "a sliding title is whole, no ellipsis");
  } finally { await b.close(); }

  // ---- reduced motion: nothing slides, wide titles keep their ellipsis and the tooltip
  b = await launch(url, [{ name: "prefers-reduced-motion", value: "reduce" }]);
  try {
    await floorOn(b);
    const still = await b.ev(`[...document.querySelectorAll(".fl .st")].map(g => { const j = g.querySelector(".card-t"), t = g.querySelector(".card-tag");
      return { text: j.textContent, moving: j.getAnimations().length + t.getAnimations().length, w: j.getComputedTextLength(), tip: g.querySelector("title").textContent }; })`);
    assert.ok(still.every(l => l.moving === 0), "no placard moves under reduced motion");
    assert.ok(still.every(l => l.w <= 34.5), "every title fits the glass");
    assert.ok(still.some(l => l.text.endsWith("…") && l.tip.length > l.text.length), "a long title keeps its ellipsis and a tooltip");
  } finally { await b.close(); }
}

run(process.argv[2]).then(() => attention(process.argv[2])).then(() => printer(process.argv[2])).then(() => budget(process.argv[2])).then(() => console.log("floor browser checks passed"), e => { console.error(e.stack || e); process.exit(1); });
