// Drives the Thoughtfield (field.html, field-sky.js) in headless Chrome (the sandbox's chrome-headless-shell, over the
// DevTools pipe) against a running `factory dashboard --demo`. Run by test_field_browser.py:
//   node scripts/test_field_browser.js <dashboard url with token>
// Each job is one packet, and none may sit behind the witness: at every viewport size below, with the demo's jobs and
// with /api/field padded to 64 jobs (MAXJOB), every job and held write has a packet on screen, above the horizon and
// below the header, no two overlap, and no vertex of the witness model (projected here from its own meshes, all of
// them, independently of the page's outline) falls inside a packet. The head turns to follow the pointer, so that is
// checked again with the pointer on the leftmost and the rightmost packet. A packet takes hover and a click across its
// whole body and a ring round it, the click opens that job's monitor, and the packet stays marked while it is open.
"use strict";
const assert = require("node:assert");
const { spawn } = require("node:child_process");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");

async function launch(url, media, init) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "field-test."));
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
    s,   // a raw DevTools call on the page
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

// /api/field padded with made-up jobs: many orders, some failed, a few running, a few blocked requests.
const PAD = n => `(() => { const real = window.fetch.bind(window);
  window.fetch = async (u, o) => { const r = await real(u, o); if (!String(u).startsWith("/api/field")) return r;
    const j = await r.json(), now = j.now || 1200, have = j.jobs.length;
    for (let k = 0; have + k < ${n}; k++) { const start = now - 600 + k * 9, ord = "pad-order-" + Math.floor(k / 3), run = k % 17 === 5, rev = k % 3 === 2;
      j.jobs.push({ id: "20261023-pad" + String(k).padStart(3, "0"), lane: rev ? "reviewer" : "developer", role: rev ? "reviewer" : "developer", order: ord, label: "Padded job " + k, task: "Padded job " + k,
        model: k % 4 ? "sonnet-5-5" : "opus-5-5", status: run ? "running" : k % 7 === 3 ? "failed" : "done", verdict: "", start, end: run ? null : start + 6, cost: run ? null : .05 + (k % 9) * .17,
        actions: Array.from({ length: 3 + k % 11 }, (_, a) => [start + a * .4, rev ? 4 : a % 4]) }); }
    j.blocked = (j.blocked || []).concat([{ t: now - 300, role: "developer" }, { t: now - 200, role: "developer" }]);
    return new Response(JSON.stringify(j), { status: 200, headers: { "Content-Type": "application/json" } }); }; })()`;
const ENTERED = `try { sessionStorage.setItem("lois-entered", "1"); } catch (e) {}`;
const SIZES = [[1920, 1080], [1440, 900], [1366, 600], [1280, 720], [1024, 768], [768, 1024], [390, 844], [360, 640]];

// Every packet against every projected vertex of the witness above the horizon; plus the screen, the header, each other.
const AUDIT = `(() => { const Sky = LF.sky, W = LF.W, cam = LF.camera, list = Sky.packets(), w = innerWidth, h = innerHeight, out = [];
  const any = list[0] && Sky.jobMesh(list.find(p => p.kind !== "gold").id), root = any.parent, scene = root.parent, v = new THREE.Vector3(), pts = [];
  scene.children.forEach(ch => { if (ch === root) return; ch.traverse(o => { const p = o.isMesh && o.geometry.attributes.position; if (!p) return; o.updateMatrixWorld(true);
    for (let i = 0; i < p.count; i++) { v.fromBufferAttribute(p, i).applyMatrix4(o.matrixWorld).project(cam); const x = (v.x * .5 + .5) * w, y = (-v.y * .5 + .5) * h; if (y < W.rimAt(x)) pts.push(x, y); } }); });
  const top = document.querySelector(".top").getBoundingClientRect().bottom;
  list.forEach((p, k) => { const name = p.kind + " " + p.id + " at " + Math.round(p.x) + "," + Math.round(p.y) + " r" + p.r.toFixed(1);
    for (let i = 0; i < pts.length; i += 2) if ((pts[i] - p.x) ** 2 + (pts[i + 1] - p.y) ** 2 < (p.r + 2) ** 2) { out.push(name + ": behind the witness"); break; }
    if (p.x - p.r < 0 || p.x + p.r > w || p.y - p.r < top || p.y + p.r > W.rimAt(p.x)) out.push(name + ": off the sky (header " + Math.round(top) + ")");
    if (p.r < 4) out.push(name + ": too small to see");
    list.slice(k + 1).forEach(q => { if (Math.hypot(p.x - q.x, p.y - q.y) < p.r + q.r) out.push(name + ": overlaps " + q.id); }); });
  return { bad: out, n: list.length, verts: pts.length / 2, ids: list.map(p => p.id) }; })()`;

async function ready(b, want) {
  for (let i = 0; i < 120; i++) { if (await b.ev(`!!(window.LF && LF.sky && LF.sky.packets().length >= ${want} && !document.getElementById("hud").hidden)`)) return; await b.sleep(250); }
  throw new Error("the sky never showed " + want + " packets: " + await b.ev(`window.LF && LF.sky ? LF.sky.packets().length : "no sky"`));
}

// packets glide to new spots: wait until none has moved half a pixel in 400 ms
async function settle(b) {
  let last = "";
  for (let i = 0; i < 60; i++) { const now = await b.ev(`JSON.stringify(LF.sky.packets().map(p => [p.id, p.x, p.y, p.r]))`); if (last) { const A = JSON.parse(last), B = JSON.parse(now); if (A.length === B.length && A.every((p, k) => p[0] === B[k][0] && Math.abs(p[1] - B[k][1]) < .5 && Math.abs(p[2] - B[k][2]) < .5 && Math.abs(p[3] - B[k][3]) < .5)) return; } last = now; await b.sleep(400); }
  throw new Error("the packets never settled");
}

async function audit(b, label, expect) {
  await settle(b);
  const a = await b.ev(AUDIT);
  assert.ok(a.verts > 1000, `${label}: the witness model was projected (${a.verts} vertices)`);
  assert.deepStrictEqual(a.bad, [], `${label}`);
  assert.strictEqual(a.n, expect.length, `${label}: one packet per job and held write`);
  assert.deepStrictEqual(a.ids.slice().sort(), expect.slice().sort(), `${label}: every job has its packet, the oldest too`);
}

async function layouts(url, pad) {
  const b = await launch(url, undefined, ENTERED + (pad ? PAD(pad) : ""));
  try {
    const field = async () => (await b.ev(`fetch("/api/field").then(r => r.json()).then(j => j.jobs.map(x => x.id))`));
    const cards = await b.ev(`fetch("/api/state").then(r => r.json()).then(s => (s.gate || []).map(c => c.id))`);
    const ids = (await field()).concat(cards);
    if (pad) assert.strictEqual(ids.length - cards.length, pad, "padded to MAXJOB jobs");
    await ready(b, ids.length);
    for (const [w, h] of SIZES) {
      await b.resize(w, h);
      const label = `${pad ? pad + " jobs" : "demo"} at ${w}x${h}`;
      await audit(b, label, ids);
      // the head turns toward the pointer: check again with it on the outermost packets
      for (const side of ["left", "right"]) {
        const p = await b.ev(`LF.sky.packets().sort((a, b) => a.x - b.x)[${side === "left" ? 0 : "LF.sky.packets().length - 1"}]`);
        await b.s("Input.dispatchMouseEvent", { type: "mouseMoved", x: p.x, y: p.y }); await b.sleep(1500);
        await audit(b, `${label}, head turned ${side}`, ids);
      }
    }
  } finally { await b.close(); }
}

async function picking(url) {
  const b = await launch(url, undefined, ENTERED);
  try {
    await b.resize(1440, 900); await ready(b, 6); await b.sleep(1500);
    const jobs = await b.ev(`LF.sky.packets().filter(p => p.kind !== "gold")`);
    const p = jobs.find(x => x.kind === "done") || jobs[0];
    // hover at the edge of the ring round it, not just the middle
    await b.s("Input.dispatchMouseEvent", { type: "mouseMoved", x: p.x + Math.max(20, p.r * 1.2), y: p.y }); await b.sleep(600);
    const hover = await b.ev(`({ card: !document.querySelector(".skycard").hidden && document.querySelector(".skycard").textContent, cursor: document.body.style.cursor, mark: LF.sky.packets().find(q => q.id === ${JSON.stringify(p.id)}).mark })`);
    const job = await b.ev(`(j => ({ lane: j.lane, task: j.task, label: j.label }))(LF.sky.job(${JSON.stringify(p.id)}))`);
    assert.ok(hover.card && hover.card.includes(job.task || job.label), `hovering its ring shows the job's card: ${hover.card}`);
    assert.strictEqual(hover.cursor, "pointer");
    assert.strictEqual(hover.mark, "hover", "the hovered packet wears the ring");
    await b.click(p.x, p.y); await b.sleep(800);
    const open = await b.ev(`({ cur: FieldMonitor.current(), mark: LF.sky.packets().find(q => q.id === ${JSON.stringify(p.id)}).mark, others: LF.sky.packets().filter(q => q.id !== ${JSON.stringify(p.id)} && q.mark === "open").length })`);
    assert.ok(open.cur && open.cur.lane === job.lane, `a click opens ${job.lane}'s monitor: ${JSON.stringify(open.cur)}`);
    assert.strictEqual(open.cur.job, p.id, "on that job");
    assert.strictEqual(open.mark, "open", "the packet stays marked while its monitor is open");
    assert.strictEqual(open.others, 0, "and no other packet is");
    await b.ev(`FieldMonitor.close()`); await b.s("Input.dispatchMouseEvent", { type: "mouseMoved", x: 5, y: 300 }); await b.sleep(700);
    assert.strictEqual(await b.ev(`LF.sky.packets().find(q => q.id === ${JSON.stringify(p.id)}).mark`), "last", "closed, it keeps a faint mark");
    // a click on empty sky opens nothing
    await b.click(40, 300); await b.sleep(300);
    assert.strictEqual(await b.ev(`FieldMonitor.current()`), null, "empty sky takes no click");
    // the question views still pour the stars out, and the packets step aside
    const r = await b.ev(`(() => { const r = LF.sky.show("spend"); return r.say; })()`);
    assert.ok(/cost the most/.test(r), r);
    const until = async (expr, what) => { for (let i = 0; i < 40; i++) { if (await b.ev(expr)) return; await b.sleep(250); } assert.fail(what); };
    await until(`LF.sky.packets().length === 0`, "packets fade out while a question's shape is up");
    await b.ev(`LF.sky.show("home")`);
    await until(`LF.sky.packets().length >= 6`, "and come back");
  } finally { await b.close(); }
}

const url = process.argv[2];
(process.env.FIELD_ONLY === "picking" ? picking(url) : layouts(url, 0).then(() => layouts(url, 64)).then(() => picking(url))).then(() => console.log("field browser checks passed"), e => { console.error(e.stack || e); process.exit(1); });
