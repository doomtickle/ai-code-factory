// Drives the Thoughtfield's "What do you need from me?" chip and pop-up (scripts/dashboard-ui/field-shown.js) in headless Chrome against
// `factory dashboard --demo`. Run by test_field_shown_browser.py:   node scripts/test_field_shown_browser.js <dashboard url with token> [png]
// Checks: the demo's block that is already there at load does not pop up; the chip opens the panel and "When was it busiest?" is gone;
// a new id pops it up by itself, and an unchanged id does not reopen a closed panel; sixteen lines fit without scrolling; a selection (by Range or mouse drag) survives polls with the same text and a changed text replaces it; COPY is dark for
// 2.5 s after the text changes, a click in the hold writes nothing and one after it writes exactly the text on screen with the real
// clipboard API, under the page's own CSP (no violation); hostile text is plain text; hidden characters raise the warning; nothing is
// sent to the Coordinator (only GETs of /api/state and /api/field leave the page).
"use strict";
const { spawn } = require("node:child_process");
const assert = require("node:assert");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");

async function launch(url, media, init, grantClipboard = true) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "field-shown-test."));
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
  // Headless Chrome has no permission prompt; a desktop one lets a page write the clipboard from a click, so grant that and nothing else.
  if (grantClipboard) await send("Browser.grantPermissions", { permissions: ["clipboardSanitizedWrite"] });
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
    async resize(width, height = 900) {
      await s("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
      await new Promise(r => setTimeout(r, 300));
    },
    async click(x, y) {
      for (const type of ["mouseMoved", "mousePressed", "mouseReleased"])
        await s("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons: type === "mousePressed" ? 1 : 0, clickCount: 1 });
    },
    async drag(x0, y0, x1, y1) {
      await s("Input.dispatchMouseEvent", { type: "mouseMoved", x: x0, y: y0 });
      await s("Input.dispatchMouseEvent", { type: "mousePressed", x: x0, y: y0, button: "left", buttons: 1, clickCount: 1 });
      await s("Input.dispatchMouseEvent", { type: "mouseMoved", x: (x0 + x1) / 2, y: (y0 + y1) / 2, button: "left", buttons: 1 });
      await s("Input.dispatchMouseEvent", { type: "mouseMoved", x: x1, y: y1, button: "left", buttons: 1 });
      await s("Input.dispatchMouseEvent", { type: "mouseReleased", x: x1, y: y1, button: "left", buttons: 0, clickCount: 1 });
    },
    async key(key, code, text) {
      await s("Input.dispatchKeyEvent", { type: "keyDown", key, code, ...(text ? { text } : {}) });
      await s("Input.dispatchKeyEvent", { type: "keyUp", key, code });
    },
    async shot(file) {
      const r = await s("Page.captureScreenshot", { format: "png" });
      fs.writeFileSync(file, Buffer.from(r.data, "base64"));
    },
    async close() {
      child.kill("SIGKILL");
      await new Promise(r => child.once("exit", r));
      try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 }); } catch (e) { /* Chrome still flushing its profile: a leftover temp folder is harmless */ }
    },
  };
}


const HOOK = `(() => { const real = window.fetch.bind(window); window.__polls = 0; window.__mut = null; window.__reqs = []; window.__csp = []; window.__writes = []; window.__wres = [];
  window.fetch = async (u, o) => { window.__reqs.push([String((o && o.method) || "GET"), String(u).split("?")[0]]);
    const r = await real(u, o); if (!String(u).startsWith("/api/state")) return r;
    const j = await r.json(); if (window.__mut) window.__mut(j); window.__polls++; return new Response(JSON.stringify(j), { status: 200 }); };
  document.addEventListener("securitypolicyviolation", e => window.__csp.push(e.violatedDirective + " " + e.blockedURI));
  const w = navigator.clipboard.writeText.bind(navigator.clipboard);
  navigator.clipboard.writeText = t => { window.__writes.push(t); return w(t).then(v => { window.__wres.push("ok"); return v; }, e => { window.__wres.push(String(e)); throw e; }); };
  })()`;

const LINES16 = Array.from({ length: 16 }, (_, i) => `${i + 1}. Merge factory-developer/factory/topic-${i}`).join("\n");

async function main(url, png) {
  const b = await launch(url, undefined, HOOK);
  try {
    const poll = async () => { const n = await b.ev(`window.__polls`); for (let i = 0; i < 40 && (await b.ev(`window.__polls`)) <= n; i++) await b.sleep(250); };
    const rect = id => b.ev(`(() => { const r = document.getElementById(${JSON.stringify(id)}).getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height, b: r.bottom, r: r.right }; })()`);
    const shown = () => b.ev(`!document.getElementById("shown").hidden`);
    for (let i = 0; i < 40 && !(await b.ev(`window.__polls > 1`)); i++) await b.sleep(250);

    await b.sleep(600);
    assert.strictEqual(await b.ev(`document.getElementById("hud").hidden`), false, "the HUD is up");
    await b.sleep(2300);

    assert.strictEqual(await shown(), false, "a block already there at load is not news: no pop-up");
    assert.strictEqual(await b.ev(`document.querySelector('#chips [data-v="time"]')`), null, "the busiest chip is gone");
    assert.strictEqual(await b.ev(`document.getElementById("shown-key").textContent`), "What do you need from me?");

    // The chip opens the panel with the demo's block.
    await b.ev(`document.getElementById("shown-key").click()`);
    assert.strictEqual(await shown(), true, "the chip opens the panel");
    const demoText = await b.ev(`document.getElementById("shown-text").textContent`);
    assert.match(demoText, /^1\. Decide: push factory\/greet-empty-name/);
    assert.match(await b.ev(`document.getElementById("shown-age").textContent`), /^\d+ min ago$/);
    assert.strictEqual(await b.ev(`getComputedStyle(document.getElementById("shown-text")).whiteSpace`), "pre-wrap");
    assert.strictEqual(await b.ev(`document.getElementById("shown-copy").disabled`), false, "a block that was not swapped in just now can be copied");
    assert.strictEqual(await b.ev(`document.getElementById("shown-warn").hidden`), true);
    assert.strictEqual(await b.ev(`document.getElementById("shown-key").getAttribute("aria-expanded")`), "true");
    if (png) await b.shot(png);

    // COPY writes the text on screen, with the real clipboard API under the page's CSP.
    const copyKey = await rect("shown-copy");
    await b.click(copyKey.x + copyKey.w / 2, copyKey.y + copyKey.h / 2);   // a real pointer press: the clipboard wants a user gesture
    await b.sleep(300);
    assert.deepStrictEqual(await b.ev(`window.__writes`), [demoText], "COPY writes the text on screen and nothing else");
    assert.deepStrictEqual(await b.ev(`window.__wres`), ["ok"], "the browser accepted the write (no permission or CSP change needed)");
    assert.deepStrictEqual(await b.ev(`window.__csp`), [], "no CSP violation");
    await b.ev(`document.getElementById("shown-close").click()`);
    assert.strictEqual(await shown(), false, "close closes it");
    await poll(); await poll();
    assert.strictEqual(await shown(), false, "the same id on the next polls does not reopen a closed panel");

    // A new id pops it up by itself; sixteen lines fit; COPY is dark for 2.5 s.
    await b.ev(`window.__writes.length = 0; window.__mut = j => { j.shown = { id: "aaaaaaaaaaa1", t: j.now - 5, text: ${JSON.stringify(LINES16)} }; }`);
    await poll(); await b.sleep(100);
    assert.strictEqual(await shown(), true, "a new id pops the panel up");
    assert.strictEqual(await b.ev(`document.getElementById("shown-copy").disabled`), true, "COPY is dark after the text changes");
    assert.strictEqual(await b.ev(`document.getElementById("shown-age").textContent`), "just now");
    const pre = await b.ev(`(() => { const p = document.getElementById("shown-text"); return { sh: p.scrollHeight, ch: p.clientHeight }; })()`);
    assert.ok(pre.sh <= pre.ch + 1, `16 lines fit without scrolling (${pre.sh} > ${pre.ch})`);
    const box = await rect("shown");
    assert.ok(box.y >= 56 && box.b <= 900 * 0.565, `the panel sits in the sky above the horizon (${box.y}..${box.b})`);
    assert.ok(box.x >= 0 && box.r <= 600, "and in the left corner, clear of the face and the decision row");
    await b.ev(`document.getElementById("shown-copy").click()`);
    assert.deepStrictEqual(await b.ev(`window.__writes`), [], "a click in the hold writes nothing");
    await b.sleep(2600);
    assert.strictEqual(await b.ev(`document.getElementById("shown-copy").disabled`), false, "COPY lights after the hold");
    await b.ev(`document.getElementById("shown-copy").click()`);
    await b.sleep(300);
    assert.deepStrictEqual(await b.ev(`window.__writes`), [LINES16]);

    // The owner can select part of the text and copy it with the browser's own copy: a poll with the same text leaves the selection alone.
    assert.strictEqual(await b.ev(`getComputedStyle(document.getElementById("shown-text")).userSelect`), "text");
    const sel = "Merge factory-developer/factory/topic-1";
    await b.ev(`(() => { const t = document.getElementById("shown-text").firstChild, i = t.data.indexOf(${JSON.stringify(sel)}), r = document.createRange();
      r.setStart(t, i); r.setEnd(t, i + ${sel.length}); const g = getSelection(); g.removeAllRanges(); g.addRange(r); })()`);
    assert.strictEqual(await b.ev(`getSelection().toString()`), sel);
    await b.ev(`(() => { window.__node = document.getElementById("shown-text").firstChild; window.__muts = 0;
      new MutationObserver(m => { window.__muts += m.length; }).observe(document.getElementById("shown-text"), { subtree: true, childList: true, characterData: true }); })()`);
    await poll(); await poll(); await b.sleep(100);
    assert.strictEqual(await b.ev(`window.__muts`), 0, "polls with the same text leave the <pre> untouched (not even a rewrite of the same string)");
    assert.strictEqual(await b.ev(`document.getElementById("shown-text").firstChild === window.__node`), true, "the same text is not rewritten");
    assert.strictEqual(await b.ev(`getSelection().toString()`), sel, "a poll with the same text keeps the selection");
    assert.strictEqual(await b.ev(`document.getElementById("shown-text").textContent`), LINES16);
    // A drag with the pointer selects too (nothing on the page swallows it), and survives a poll.
    await b.ev(`getSelection().removeAllRanges()`);
    const pr = await rect("shown-text");
    await b.drag(pr.x + 4, pr.y + 30, pr.x + pr.w - 40, pr.y + 50);
    const dragged = await b.ev(`getSelection().toString()`);
    assert.ok(dragged.length > 10 && LINES16.includes(dragged.replace(/\n+$/, "")), `a mouse drag selects text of the panel (${JSON.stringify(dragged)})`);
    await poll();
    assert.strictEqual(await b.ev(`getSelection().toString()`), dragged, "and a poll keeps it");

    // A block swapped in just before a click is never the one written.
    await b.ev(`window.__writes.length = 0; window.__mut = j => { j.shown = { id: "aaaaaaaaaaa2", t: j.now, text: "second" }; }`);
    await poll(); await b.sleep(100);
    assert.strictEqual(await b.ev(`document.getElementById("shown-text").textContent`), "second", "changed text replaces the panel content");
    assert.strictEqual(await b.ev(`getSelection().toString()`), "", "and a selection of the old text is gone with it");
    await b.ev(`document.getElementById("shown-copy").click()`);
    assert.deepStrictEqual(await b.ev(`window.__writes`), [], "the swap re-arms the hold");

    // Hostile text is text; hidden characters raise the warning.
    const hostile = "<img src=x onerror=window.__pwn=1><script>window.__pwn=1</script>\nrm -rf ~ ⟨U+202E⟩ done";
    await b.ev(`window.__mut = j => { j.shown = { id: "aaaaaaaaaaa3", t: j.now, text: ${JSON.stringify(hostile)} }; }`);
    await poll(); await b.sleep(100);
    assert.strictEqual(await b.ev(`document.getElementById("shown-text").textContent`), hostile);
    assert.strictEqual(await b.ev(`document.getElementById("shown-text").children.length`), 0, "no element came from the text");
    assert.strictEqual(await b.ev(`window.__pwn`), undefined);
    assert.strictEqual(await b.ev(`document.getElementById("shown-warn").hidden`), false, "flagged characters show a warning");

    // Old blocks shrink; none posted shows the empty line; neither closes the panel.
    await b.ev(`window.__mut = j => { j.shown = { id: "aaaaaaaaaaa3", t: j.now - 700, text: "old news" }; }`);
    await poll(); await b.sleep(100);
    assert.strictEqual(await b.ev(`document.getElementById("shown").classList.contains("old")`), true);
    assert.strictEqual(await shown(), true, "an old block does not close itself");
    await b.ev(`window.__mut = j => { j.shown = null; }`);
    await poll(); await b.sleep(100);
    assert.strictEqual(await shown(), true);
    assert.strictEqual(await b.ev(`document.getElementById("shown-text").textContent`), "Nothing posted yet. Ask me in the terminal.");
    assert.strictEqual(await b.ev(`document.getElementById("shown-copy").disabled`), true, "nothing to copy");

    // Display only: the pop-up added no request. Everything the page sent was a GET (the polls, the voice service's health check).
    const reqs = await b.ev(`window.__reqs`);
    assert.ok(reqs.length > 0 && reqs.every(r => r[0] === "GET"), "the page sent nothing but GETs");
    assert.ok(!reqs.some(r => /shown|second|old news/i.test(r[1])), "no request carries the block");
    assert.deepStrictEqual(await b.ev(`window.__csp`), []);
  } finally {
    await b.close();
  }
}

main(process.argv[2], process.argv[3]).then(() => console.log("ok"), e => { console.error(e); process.exit(1); });
