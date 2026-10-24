// Drives the factory floor in headless Chrome (the sandbox's chrome-headless-shell, over
// the DevTools pipe) against a running `factory dashboard --demo`. Run by
// test_floor_browser.py:   node scripts/test_floor_browser.js <dashboard url with token>
// Checks, for every station (developers and reviewers): nothing lies over it (the topmost
// element at its centre is the station), a click opens that lane's monitor, Enter and Space
// do too, and it wears glasses. Job titles wider than the console glass slide, clipped to it;
// short ones and everything under prefers-reduced-motion stay still (ellipsis + tooltip).
// It also types the Konami code to check the factory radio badge (radio.js) shows, hides and remembers.
"use strict";
const { spawn } = require("node:child_process");
const assert = require("node:assert");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");

async function launch(url, media) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "floor-test."));
  const child = spawn(process.env.FACTORY_SHOT_CHROME || "chrome-headless-shell", ["--headless", "--no-sandbox", "--disable-gpu",
    "--disable-dev-shm-usage", "--hide-scrollbars", "--no-first-run", "--disable-extensions", "--remote-debugging-pipe",
    `--user-data-dir=${profile}`, "--window-size=1280,900", "about:blank"], { stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"] });
  let next = 1, buf = "";
  const pending = new Map(), listeners = [];
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
      fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    },
  };
}

async function floorOn(b) {
  for (let i = 0; i < 40 && !(await b.ev(`document.querySelectorAll(".lane").length > 0`)); i++) await b.sleep(250);
  await b.ev(`document.getElementById("floor-key").click()`);
  for (let i = 0; i < 40 && !(await b.ev(`document.querySelectorAll(".fl .st").length >= 5`)); i++) await b.sleep(250);
  await b.sleep(500);
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

run(process.argv[2]).then(() => console.log("floor browser checks passed"), e => { console.error(e.stack || e); process.exit(1); });
