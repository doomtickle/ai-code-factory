// Drives talking to LOIS in Terminal 0's field frame (scripts/dashboard-ui/term-mic.js) in headless Chrome, with Chrome's fake
// microphone playing the WAV test_term_mic_browser.py writes and its stub voice service. Run by that file:
//   node scripts/test_term_mic_browser.js <dashboard url with token> <stub voice port> <wav>      (prints {"held": seconds} last)
// The stub answers each /listen with what this script queued for it (POST /__next), else no words.
// Checks, with the pane docked: the strip sits in the pane under the grid; holding the mic key listens and types the words
// (the Python side checks the body and the Coordinator's terminal); Always on lights, a "Lois, ..." line waits 2 s with a
// countdown and is typed, any line waits too (no wake word), a bare name shows nothing, Escape on the main page and a click on the line cancel,
// and nothing is posted while LOIS speaks. No press grows the pane or moves focus into it; the frame's requests go only to the
// voice port with no CSP violation; the main page posts no audio and never sees the words. In a second browser getUserMedia
// refuses: the refusal is said, both keys go dark, nothing is sent.
"use strict";
const { spawn } = require("node:child_process");
const assert = require("node:assert");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path"), http = require("node:http");


async function launch(url, init, wav) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "term-mic-test."));
  const child = spawn(process.env.FACTORY_SHOT_CHROME || "chrome-headless-shell", ["--headless", "--no-sandbox", "--disable-gpu",
    "--disable-dev-shm-usage", "--hide-scrollbars", "--no-first-run", "--disable-extensions", "--remote-debugging-pipe",
    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", `--use-file-for-fake-audio-capture=${wav}`, "--autoplay-policy=no-user-gesture-required",
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
  await s("Page.addScriptToEvaluateOnNewDocument", { source: init });
  const loaded = new Promise(r => { const f = m => { if (m.method === "Page.loadEventFired") { listeners.splice(listeners.indexOf(f), 1); r(); } }; listeners.push(f); });
  await s("Page.navigate", { url });
  await loaded;
  const evIn = async (contextId, expr) => {
    const r = await s("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true, ...(contextId ? { contextId } : {}) });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  };
  return {
    send, s, sleep: ms => new Promise(r => setTimeout(r, ms)),
    ev: expr => evIn(null, expr),
    // Terminal 0's frame: the terminal origin's context whose page is /term/field (the control unit shares that origin).
    async term(expr) {
      if (this.tc && contexts.has(this.tc)) { try { return await evIn(this.tc, expr); } catch (e) { if (!/context/i.test(e.message)) throw e; } }
      for (const c of contexts.values()) {
        if (!c.origin.startsWith("http://127.0.0.1:") || !(c.auxData && c.auxData.isDefault)) continue;
        let p; try { p = await evIn(c.id, "location.pathname"); } catch (e) { continue; }
        if (p === "/term/field") { this.tc = c.id; return evIn(c.id, expr); }
      }
      throw new Error("no terminal frame");
    },
    // With TERM_MIC_SHOTS set, a PNG of the page into that folder (for a person to look at; never committed).
    async shot(name) {
      if (!process.env.TERM_MIC_SHOTS) return;
      const { data } = await s("Page.captureScreenshot", { format: "png" });
      fs.writeFileSync(path.join(process.env.TERM_MIC_SHOTS, name + ".png"), Buffer.from(data, "base64"));
    },
    async escape() {
      for (const type of ["rawKeyDown", "keyUp"]) await s("Input.dispatchKeyEvent", { type, key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
    },
    async mouse(type, x, y) { await s("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons: type === "mousePressed" ? 1 : 0, clickCount: 1 }); },
    async click(x, y) { for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) await this.mouse(type, x, y); },
    async close() {
      const gone = new Promise(r => child.exitCode !== null || child.signalCode !== null ? r() : child.once("exit", r));
      child.kill(); await gone; fs.rmSync(profile, { recursive: true, force: true, maxRetries: 3 });
    },
  };
}

// In every document (the page and its frames): what it fetched and any CSP violation.
const HOOK = `(() => { const real = window.fetch.bind(window); window.__reqs = []; window.__csp = [];
  window.fetch = (u, o) => { window.__reqs.push([String((o && o.method) || "GET"), String(u)]); return real(u, o); };
  window.__gum = []; if (navigator.mediaDevices) { const g = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices); navigator.mediaDevices.getUserMedia = c => { window.__gum.push("ask"); return g(c).then(x => { window.__gum.push("ok"); return x; }, e => { window.__gum.push(e.name); throw e; }); }; }
  document.addEventListener("securitypolicyviolation", e => window.__csp.push(e.violatedDirective + " " + e.blockedURI));
  })()`;

// The owner saying no. chrome-headless-shell's fake UI grants whatever the permission says, so the refusal is the one thing
// faked: the browser's own error, after its own asynchronous answer.
const REFUSE = `(() => { if (navigator.mediaDevices) navigator.mediaDevices.getUserMedia = () => new Promise((_, no) => setTimeout(() => no(new DOMException("Permission denied", "NotAllowedError")), 50)); })();`;

async function until(f, ms, what) {
  const end = Date.now() + ms;
  for (;;) { let v; try { v = await f(); } catch (e) { v = false; } if (v) return v; if (Date.now() > end) throw new Error("timed out: " + what); await new Promise(r => setTimeout(r, 100)); }
}

let VOICE = 0;
function stub(method, p, body) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port: VOICE, path: p, method, headers: { "Content-Type": "application/json" } }, res => {
      let d = ""; res.on("data", c => d += c); res.on("end", () => resolve(JSON.parse(d || "{}")));
    });
    req.on("error", reject); req.end(body ? JSON.stringify(body) : undefined);
  });
}
const posts = async () => (await stub("GET", "/__posts")).n;
const say = text => stub("POST", "/__next", { text });

// Where an element of the terminal frame is on the page: the frame's box plus the element's box in it.
async function spot(b, id) {
  const f = await b.ev(`(() => { const r = document.querySelector(".term-frame").getBoundingClientRect(); return { x: r.x, y: r.y }; })()`);
  const k = await b.term(`(() => { const r = document.getElementById(${JSON.stringify(id)}).getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; })()`);
  return { x: f.x + k.x + k.w / 2, y: f.y + k.y + k.h / 2, box: { x: f.x + k.x, y: f.y + k.y, w: k.w, h: k.h } };
}
const pane = b => b.ev(`({ grown: document.getElementById("term-panel").classList.contains("grown"), focused: document.activeElement === document.querySelector(".term-frame") })`);
async function calm(b, what) { assert.deepStrictEqual(await pane(b), { grown: false, focused: false }, what + ": the pane neither grows nor takes focus"); }
const strip = b => b.term(`(() => { const v = document.getElementById("voice"); return { always: v.dataset.always, ptt: v.dataset.ptt, status: document.getElementById("voice-status").textContent, pending: document.getElementById("voice-pending").hidden ? null : document.getElementById("voice-pending-text").textContent, left: document.getElementById("voice-pending-left").textContent }; })()`);

// Wait in the frame itself for a line to wait in the strip, and answer its words at once (one round trip: polling from here
// is slow while this host's software-rendered sky holds the thread the frame shares, and the window is only 2 s).
const waiting = (b, ms) => b.term(`new Promise(r => { const p = document.getElementById("voice-pending"), end = Date.now() + ${ms};
  const go = () => !p.hidden ? r(document.getElementById("voice-pending-text").textContent) : Date.now() > end ? r(null) : setTimeout(go, 10); go(); })`);

async function enter(b) {
  await until(() => b.ev(`!!document.getElementById("hud") && !document.getElementById("hud").hidden`), 15000, "the page");
  await until(() => b.term(`document.getElementById("mic").getAttribute("aria-disabled") === "false"`), 15000, "the mic key lights once /health says listen");
  await b.sleep(1200);   // the pane settles
}

let held = 0;   // seconds the mic key was held, for the Python side's check of the body's length

async function main(url, port, wav) {
  VOICE = Number(port);
  await granted(url, wav);
  await refused(url, wav);
  console.log(JSON.stringify({ held }));
}

async function granted(url, wav) {
  const b = await launch(url, HOOK, wav);
  try {
    await enter(b);
    // The strip is inside the docked pane, under the terminal's grid, and the pane is still small.
    const p = await b.ev(`(() => { const r = document.getElementById("term-panel").getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height, vh: innerHeight }; })()`);
    const v = (await spot(b, "voice")).box, t = (await spot(b, "term")).box;
    assert.ok(v.x >= p.x - 1 && v.x + v.w <= p.x + p.w + 1 && v.y >= p.y && v.y + v.h <= p.y + p.h + 1, `the strip is in the pane: ${JSON.stringify([v, p])}`);
    assert.ok(t.y + t.h <= v.y + 0.5, `the grid ends above the strip: ${JSON.stringify([t, v])}`);
    assert.ok(p.h < p.vh * .45, "the pane is docked and small");
    assert.strictEqual(await b.term(`document.getElementById("mic").title`), "Hold to talk");
    assert.strictEqual((await strip(b)).always, "off", "always on is off by default");

    // Push to talk: hold the key 1.5 s with the mouse.
    await say("start the review of the auth branch");
    const k = await spot(b, "mic");
    const t0 = Date.now();
    await b.mouse("mouseMoved", k.x, k.y); await b.mouse("mousePressed", k.x, k.y);
    await until(async () => (await strip(b)).ptt === "listening", 5000, "listening while held");
    assert.strictEqual((await strip(b)).status, "Listening… let go to send");
    await b.sleep(1500);
    await b.mouse("mouseReleased", k.x, k.y);
    held = (Date.now() - t0) / 1000;
    await until(async () => (await strip(b)).ptt === "idle", 10000, "transcribed and typed");
    assert.strictEqual(await posts(), 1);
    await calm(b, "holding the mic key");

    // Always on: the toggle lights, and lines without the name are dropped.
    const a = await spot(b, "always");
    await b.click(a.x, a.y);
    await until(async () => (await strip(b)).always === "live", 8000, "always on is listening");
    assert.strictEqual((await strip(b)).status, "Always on: listening");
    await b.shot("always-on");
    assert.strictEqual(await b.term(`localStorage.getItem("lois-always-on")`), "1", "remembered in this browser");
    await calm(b, "switching always on");
    let n = await posts();
    await until(async () => await posts() >= n + 1, 8000, "an utterance is posted");   // a phrase with no words queued: nothing at all
    await b.sleep(300);
    assert.strictEqual((await strip(b)).pending, null);

    // "Lois, run the tests." waits 2 s with its countdown, then is typed.
    await b.term(`(() => { const el = document.getElementById("voice-pending"); window.__left = []; window.__text = []; new MutationObserver(() => { window.__left.push(document.getElementById("voice-pending-left").textContent); window.__text.push(document.getElementById("voice-pending-text").textContent); }).observe(el, { childList: true, characterData: true, subtree: true }); })()`);
    await say("Lois, run the tests.");
    await until(() => b.term(`window.__text.length > 0`), 8000, "the line waits in the strip");
    await b.shot("waiting");   // this line is typed anyway, so a slow shot can't spoil the check
    // The countdown starts at 2 s and only goes down until the line goes. Recorded in the frame as it is drawn: on this host the
    // software-rendered sky shares the frame's thread, so its timers run late and a poll from here sees only a few of the steps.
    await until(async () => (await strip(b)).pending === null, 8000, "sent when the window ends");
    assert.deepStrictEqual([...new Set(await b.term(`window.__text`))], ["run the tests."], "the words after the name, and only those");
    const seen = [...new Set(await b.term(`window.__left`))].map(parseFloat);
    assert.ok(seen.length >= 3 && seen[0] === 2 && seen.every((x, i) => i === 0 || x < seen[i - 1]), `the countdown runs: ${seen}`);

    // No wake word: a line that does not start with the name waits and is typed whole.
    await say("nice weather today");
    assert.strictEqual(await waiting(b, 8000), "nice weather today", "a line without LOIS waits too");
    await until(async () => (await strip(b)).pending === null, 8000, "sent when the window ends");
    assert.strictEqual((await strip(b)).status, "Always on: listening");

    // Only the name: posted to /listen, then nothing shown and nothing typed.
    n = await posts();
    await say("Lois.");
    await until(async () => await posts() >= n + 1, 8000, "the next utterance");
    for (let i = 0; i < 10; i++) { assert.strictEqual((await strip(b)).pending, null, "a bare name shows nothing"); await b.sleep(100); }
    assert.strictEqual((await strip(b)).status, "Always on: listening");

    // Escape on the main page cancels a waiting line.
    await say("Louis, drop this one");
    assert.strictEqual(await waiting(b, 8000), "drop this one", "the mishearing counts");
    await b.escape();
    await until(async () => (await strip(b)).pending === null, 1000, "Escape cancels");
    assert.strictEqual((await strip(b)).status, "Cancelled. Nothing was sent.", JSON.stringify(await strip(b)));
    await calm(b, "Escape");

    // So does a click on it.
    const where = (await spot(b, "voice-status")).box;   // the waiting line takes the status line's place
    await say("Hey Lois, click me away");
    assert.strictEqual(await waiting(b, 8000), "click me away", "the next line waits");
    await b.click(where.x + 40, where.y + where.h / 2);
    await until(async () => (await strip(b)).pending === null, 1000, "a click cancels");
    assert.strictEqual((await strip(b)).status, "Cancelled. Nothing was sent.");
    await calm(b, "a click on the waiting line");

    // While LOIS speaks (and 300 ms after) nothing is heard.
    await b.ev(`Object.defineProperty(window.LiveVoice, "speaking", { configurable: true, get: () => true })`);
    await until(async () => (await strip(b)).always === "hushed", 2000, "hushed while she speaks");
    await until(async () => (await strip(b)).status === "Always on: paused while LOIS speaks", 4000, "the strip says so once the last message has had its time");
    await b.sleep(800);   // one in flight when the hush came may still land
    n = await posts();
    await say("Lois, said while she speaks");
    await b.sleep(6000);   // two phrases go by, and the hush is renewed past its 5 s staleness
    assert.strictEqual(await posts(), n, "nothing is posted while she speaks");
    assert.strictEqual((await strip(b)).always, "hushed");
    await stub("GET", "/__posts");
    await b.ev(`Object.defineProperty(window.LiveVoice, "speaking", { configurable: true, get: () => false })`);
    await until(async () => (await strip(b)).always === "live", 2000, "listening again after she stops");
    assert.strictEqual(await waiting(b, 8000), "said while she speaks", "the next line is heard");
    await b.escape();
    await until(async () => (await strip(b)).pending === null, 1000, "cancelled");

    // Off again: the lamp goes out, the choice is remembered, and nothing more is posted.
    await b.click(a.x, a.y);
    await until(async () => (await strip(b)).always === "off", 2000, "always on is off");
    assert.strictEqual(await b.term(`localStorage.getItem("lois-always-on")`), "0");
    n = await posts(); await b.sleep(3000);
    assert.strictEqual(await posts(), n, "nothing is heard with it off");
    await calm(b, "switching it off");

    const reqs = await b.term(`window.__reqs`);
    const voice = reqs.filter(([, u]) => !u.startsWith("/"));
    voice.forEach(([, u]) => assert.match(u, new RegExp(`^http://127\\.0\\.0\\.1:${VOICE}/(health|listen)$`), u));
    assert.deepStrictEqual(await b.term(`window.__csp`), [], "no CSP violation in the frame");
    assert.deepStrictEqual(await b.ev(`window.__csp`), [], "no CSP violation on the page");
    assert.ok(!(await b.ev(`window.__reqs`)).some(([, u]) => u.includes("/listen")), "the main page never posts audio");
    const text = await b.ev(`document.body.innerText`);
    for (const words of ["auth branch", "run the tests", "drop this one"]) assert.ok(!text.includes(words), "the main page never sees the words");
  } finally {
    await b.close();
  }
}

async function refused(url, wav) {
  const b = await launch(url, REFUSE + HOOK, wav);
  try {
    await enter(b);
    const before = await posts();
    const k = await spot(b, "mic");
    await b.mouse("mouseMoved", k.x, k.y); await b.mouse("mousePressed", k.x, k.y); await b.sleep(800); await b.mouse("mouseReleased", k.x, k.y);
    await until(async () => (await strip(b)).status.startsWith("Microphone blocked."), 5000, "a refusal is said");
    assert.deepStrictEqual(await b.term(`window.__gum`), ["ask", "NotAllowedError"]);
    assert.strictEqual(await b.term(`document.getElementById("mic").getAttribute("aria-disabled")`), "true");
    assert.strictEqual(await b.term(`document.getElementById("always").getAttribute("aria-disabled")`), "true");
    const a = await spot(b, "always");
    await b.click(a.x, a.y); await b.sleep(1000);
    assert.deepStrictEqual(await b.term(`window.__gum`), ["ask", "NotAllowedError"], "not asked for again");
    assert.strictEqual((await strip(b)).always, "off");
    assert.strictEqual(await posts(), before, "nothing was posted");
    await calm(b, "a refused microphone");
    await b.shot("refused");
    if (process.env.TERM_MIC_SHOTS) {
      await b.s("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
      await b.sleep(2500); await b.shot("refused-390");
    }
  } finally {
    await b.close();
  }
}

main(process.argv[2], process.argv[3], process.argv[4]).catch(e => { console.error(e); process.exit(1); });
