// Drives the Thoughtfield (field.html, field-sky.js) in headless Chrome (the sandbox's chrome-headless-shell, over the
// DevTools pipe) against a running `factory dashboard --demo`. Run by test_field_browser.py:
//   node scripts/test_field_browser.js <dashboard url with token>
// Each job is one packet, and none may sit behind the witness: at every viewport size below, with the demo's jobs and
// with /api/field padded to 65 jobs (one past MAXJOB), every job and held write has a packet on screen, above the horizon and
// below the header, no two overlap, and no vertex of the witness model (projected here from its own meshes, all of
// them, independently of the page's outline) falls inside a packet. The head turns to follow the pointer, so that is
// checked again with the pointer on the leftmost and the rightmost packet. With no witness outline at all, the packets
// still show on the sky and the page throws nothing. Every star drawn sits in its own job's packet:
// past MAXJOB jobs the shader's slots would wrap, so the oldest job's stars are hidden rather than drawn in the newest's. A packet takes hover and a click across its
// whole body and a ring round it, the click opens that job's monitor, and the packet stays marked while it is open. A lane starting a job sends one
// comet from the witness to its crystal, the job ending sends one light back, and reduced motion sends none (`comet`).
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
  const pending = new Map(), listeners = [], contexts = new Map(), errors = [];
  listeners.push(m => {
    if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception ? m.params.exceptionDetails.exception.description : m.params.exceptionDetails.text);
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
    errors,   // uncaught exceptions on the page so far
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
    if (pad) assert.strictEqual(ids.length - cards.length, pad, "padded past MAXJOB jobs");
    await ready(b, ids.length);
    await settle(b);
    const st = await b.ev(`LF.sky.starSlots()`);
    assert.ok(st.drawn > 0, `stars are drawn: ${JSON.stringify(st)}`);
    assert.deepStrictEqual(st.wrong, [], "every star drawn is in its own job's packet");
    assert.strictEqual(st.lo, Math.max(0, ids.length - cards.length - 64), "only the newest 64 jobs hold a slot");
    if (pad > 64) assert.ok(st.hidden > 0, "the oldest job's stars are hidden, not drawn in another packet");
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
    // with no typed line, the keys 1 to 4 are the lenses (the main page has focus), and Escape comes home
    await b.key("4", "Digit4");
    await until(`LF.sky.view === "time"`, "key 4 shows the busiest time");
    await b.key("Escape", "Escape");
    await until(`LF.sky.view === "home"`, "Escape comes home");
    await b.key("2", "Digit2");
    await until(`LF.sky.view === "stuck"`, "key 2 shows what is stuck");
    await b.ev(`LF.sky.show("home")`);
    await until(`LF.sky.packets().length >= 6`, "and the packets come back again");
    // no witness outline at all (some odd viewport size): the packets ring a stand-in instead, and nothing throws per frame
    const n = await b.ev(`LF.sky.packets().length`);
    assert.strictEqual(await b.ev(`LF.sky.noOutline(true)`), true, "the stand-in outline is in use");
    await b.sleep(2500); await settle(b);
    const off = await b.ev(`(() => { const top = document.querySelector(".top").getBoundingClientRect().bottom; return LF.sky.packets().filter(p => p.x - p.r < 0 || p.x + p.r > innerWidth || p.y - p.r < top || p.y + p.r > LF.W.rimAt(p.x)).map(p => p.id); })()`);
    assert.strictEqual(await b.ev(`LF.sky.packets().length`), n, "every packet still shows with no outline");
    assert.deepStrictEqual(off, [], "and is on the sky");
    assert.deepStrictEqual(b.errors, [], "no page errors with no outline");
    assert.strictEqual(await b.ev(`LF.sky.noOutline(false)`), false, "the real outline comes back");
  } finally { await b.close(); }
}

// Several orders in different states: "without hovering, can you say what each order is, what state it is in and what order its steps ran in?"
// /api/field is replaced with named orders (a running review, an approved one, changes requested then a fix, a failure, a lone job...) and /api/state
// with one held write on fix-login, so the labels, step numbers and state marks can be checked against what the data says.
const RICH = `(() => { const real = window.fetch.bind(window);
  let k = 0; const J = (code, lane, role, order, task, st, verdict, start, end, cost) => { const id = "20261106-" + String(100000 + k) + "-00000" + (k++).toString(16); return { id, lane, role, order: order === "=" ? id : order, label: task, task, model: "sonnet-5-5", status: st, verdict, start, end, cost,
    actions: Array.from({ length: 6 }, (_, a) => [start + a * .5, role === "reviewer" ? 4 : a % 4]) }; };
  window.fetch = async (u, o) => { const r = await real(u, o), s = String(u);
    if (s.startsWith("/api/field")) { k = 0; const j = await r.json(), t = (j.now || 700) - 180;
      j.jobs = [J("a01", "developer", "developer", "rate-limit", "Add rate limit", "done", "", t, t + 9, .8), J("a02", "reviewer-lane2", "reviewer", "rate-limit", "Review rate limit", "running", "", t + 12, null, null),
        J("b01", "developer", "developer", "fix-login", "Fix login redirect", "done", "", t + 3, t + 14, 1.1), J("b02", "reviewer-lane2", "reviewer", "fix-login", "Review login", "done", "approved", t + 16, t + 22, .4),
        J("c01", "developer-lane3", "developer", "docs-pass", "Docs pass", "done", "", t + 20, t + 40, .9), J("c02", "reviewer-lane2", "reviewer", "docs-pass", "Review docs", "done", "revise", t + 42, t + 50, .3),
        J("c03", "developer-lane3", "developer", "docs-pass", "Docs fixes", "done", "", t + 52, t + 60, .5), J("d01", "developer", "developer", "migrate-db", "Migrate db", "failed", "", t + 30, t + 38, 1.4),
        J("e01", "developer-lane2", "developer", "cache-warm", "Warm caches", "running", "", (j.now || 700) - 6, null, null), J("f01", "developer-lane3", "developer", "=", "Tidy packaging metadata", "done", "", t + 70, t + 80, .2),
        J("g01", "developer", "developer", "search-index", "Search index", "done", "", t + 85, t + 100, .7), J("g02", "reviewer-lane2", "reviewer", "search-index", "Review search", "done", "approved", t + 102, t + 110, .3),
        J("h01", "developer-lane2", "developer", "dark-mode", "Dark mode", "done", "", t + 115, t + 130, .6), J("h02", "reviewer", "reviewer", "dark-mode", "Review dark", "done", "revise", t + 132, t + 140, .3)];
      return new Response(JSON.stringify(j), { status: 200, headers: { "Content-Type": "application/json" } }); }
    if (s.startsWith("/api/state")) { const j = await r.json(); j.gate = [{ id: "g1", repo: "r", branch: "factory/fix-login", title: "push", kind: "push", decided: "" }];
      return new Response(JSON.stringify(j), { status: 200, headers: { "Content-Type": "application/json" } }); }
    return r; }; })()`;
const RICH_ORDERS = { "rate-limit": ["Reviewing", "run"], "fix-login": ["Waiting for you", "wait"], "docs-pass": ["Done", "done"], "migrate-db": ["Failed", "bad"], "cache-warm": ["Working", "run"], "Tidy packaging metadata": ["Done", "done"], "search-index": ["Approved", "ok"], "dark-mode": ["Changes requested", "rev"] };
const LABELS = `(() => { const Sky = LF.sky, list = Sky.packets(), pk = list.map(p => [p.x - p.r, p.y - p.r, p.x + p.r, p.y + p.r]);
  const labels = [...document.querySelectorAll("#tags .tag.ord.on")].map(e => { const r = e.getBoundingClientRect(); return { text: e.textContent, cls: e.className, r: [r.left, r.top, r.right, r.bottom] }; });
  const over = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
  const bust = r => [0, .25, .5, .75, 1].some(k => Sky.overWitness(r[0] + (r[2] - r[0]) * k, r[1], 0) || Sky.overWitness(r[0] + (r[2] - r[0]) * k, r[3], 0) || Sky.overWitness(r[0] + (r[2] - r[0]) * k, (r[1] + r[3]) / 2, 0));
  return { labels: labels.map(l => l.text), tones: labels.map(l => l.cls), onCrystal: labels.filter(l => pk.some(b => over(l.r, b))).map(l => l.text), onBust: labels.filter(l => bust(l.r)).map(l => l.text),
    onEach: labels.filter((l, i) => labels.some((m, k) => k !== i && over(l.r, m.r))).map(l => l.text), off: labels.filter(l => l.r[0] < 0 || l.r[2] > innerWidth || l.r[1] < document.querySelector(".top").getBoundingClientRect().bottom - 1 || l.r[3] > LF.W.rimAt((l.r[0] + l.r[2]) / 2)).map(l => l.text),
    orders: Sky.orderList().map(o => ({ name: o.name, word: o.word, tone: o.tone })),
    pips: [...document.querySelectorAll("#pips .pip")].map(e => ({ cls: e.className, n: e.querySelector(".pn") ? e.querySelector(".pn").textContent : "", mark: !!e.querySelector(".pm") })),
    packets: list.map(p => ({ key: p.key, order: p.order, n: p.n, tone: p.tone, r: p.r })) }; })()`;
async function crystals(url) {
  const b = await launch(url, undefined, ENTERED + RICH);
  try {
    await b.resize(1440, 900); await ready(b, 15); await settle(b); await b.sleep(1200);
    for (const [w, h] of [[1920, 1080], [1440, 900], [1024, 768], [768, 1024], [390, 844]]) {
      await b.resize(w, h); await settle(b); await b.sleep(900);
      const L = await b.ev(LABELS), at = `${w}x${h}`;
      // every packet wears one state mark and, unless tiny, its step number; the mark is the packet's own state
      assert.strictEqual(L.pips.length, L.packets.length, `${at}: one number and mark per crystal`);
      assert.ok(L.pips.every(p => p.mark), `${at}: every crystal has a mark`);
      assert.deepStrictEqual(L.pips.map(p => (/\bp-(\w+)/.exec(p.cls) || [])[1]).sort(), L.packets.map(p => p.tone).sort(), `${at}: marks say each crystal's state`);
      assert.ok(L.pips.every(p => /\bp-(run|done|ok|rev|bad|wait)\b/.test(p.cls)), `${at}: classes come from the fixed tones`);
      L.pips.forEach(p => { if (p.n) assert.ok(/^[1-9]\d?$/.test(p.n), `${at}: step number ${p.n}`); });
      assert.ok(L.pips.filter(p => p.n).length >= L.pips.length * .6, `${at}: most crystals show a number`);
      // steps run 1, 2, 3 within an order, by start time
      const by = {}; L.packets.forEach(p => { (by[p.order] = by[p.order] || []).push(p.n); });
      Object.entries(by).forEach(([o, ns]) => assert.deepStrictEqual(ns.slice().sort(), ns.map((_, k) => k + 1), `${at}: ${o}'s steps are numbered 1..n`));
      assert.deepStrictEqual(by["docs-pass"].slice().sort(), [1, 2, 3], `${at}: docs-pass ran in three steps`);
      assert.strictEqual(by["fix-login"].length, 3, `${at}: the held write is fix-login's third step`);
      // each order says what it is and its state, in words, from its newest job
      assert.deepStrictEqual(L.orders.map(o => [o.name, o.word, o.tone]).sort(), Object.entries(RICH_ORDERS).map(([n, [w, t]]) => [n, w, t]).sort(), `${at}: the order's state in words`);
      // a label for every waiting and running order, always; none on a crystal or the witness (a phone, with no room left, may) or on another label
      for (const [n, [wd, tone]] of Object.entries(RICH_ORDERS)) if (tone === "run" || tone === "wait") assert.ok(L.labels.includes(`${n} · ${wd}`), `${at}: ${n} keeps its label (${JSON.stringify(L.labels)})`);
      L.labels.forEach(t => assert.ok(Object.entries(RICH_ORDERS).some(([n, [wd]]) => t === `${n} · ${wd}`), `${at}: label text ${t}`));
      assert.deepStrictEqual(L.off, [], `${at}: labels stay on the sky`);
      if (w >= 768) { assert.deepStrictEqual(L.onCrystal, [], `${at}: no label covers a crystal`); assert.deepStrictEqual(L.onBust, [], `${at}: no label covers the witness`); }
      assert.deepStrictEqual(L.onEach, [], `${at}: no two labels overlap`);
      if (w >= 1440) assert.strictEqual(L.labels.length, 8, `${at}: with room, every order has a label`);
      // when some are left out, a waiting, running or failed one is never the one
      if (L.labels.length < 8) ["Waiting for you", "Reviewing", "Working"].forEach(wd => assert.ok(L.labels.some(t => t.endsWith(wd)), `${at}: ${wd} kept`));
    }
    // hover: the card lists the order's steps oldest first, the hovered one marked; a click still opens that job
    await b.resize(1440, 900); await settle(b); await b.sleep(600);
    const p = await b.ev(`LF.sky.packets().find(q => q.order === "docs-pass" && q.n === 2)`);
    await b.s("Input.dispatchMouseEvent", { type: "mouseMoved", x: p.x, y: p.y }); await b.sleep(700);
    const card = await b.ev(`({ head: document.querySelector(".skycard .st").textContent, rows: [...document.querySelectorAll(".skycard .hr")].map(r => ({ n: r.querySelector(".n").textContent, t: r.querySelector(".t").textContent, w: r.querySelector(".w").textContent, cur: r.classList.contains("cur") })), text: document.querySelector(".skycard").textContent })`);
    assert.ok(/^Step 2 of 3 · Changes requested/.test(card.head), card.head);
    assert.deepStrictEqual(card.rows.map(r => r.w), ["developer-lane3 work started", "finished", "reviewer-lane2 review started", "asked for changes", "developer-lane3 work started", "finished"], "start and end of each step, in the order they happened");
    const ts = card.rows.map(r => r.t); assert.deepStrictEqual(ts, ts.slice().sort(), "oldest first");
    assert.ok(ts.every(t => /^\d\d:\d\d$/.test(t)));
    assert.deepStrictEqual(card.rows.map(r => r.cur), [false, false, true, true, false, false], "the hovered step is the one marked");
    assert.deepStrictEqual(card.rows.filter(r => r.n).map(r => r.n), ["1", "2", "3"]);
    assert.ok(card.text.includes("Click to open this job"));
    await b.click(p.x, p.y); await b.sleep(700);
    const mon = await b.ev(`FieldMonitor.current()`); assert.ok(mon && mon.job === p.id, "a click still opens that job's monitor: " + JSON.stringify(mon) + " " + p.id);
    await b.ev(`FieldMonitor.close()`);
    // the held write's step names it as waiting for the owner
    const g = await b.ev(`LF.sky.packets().find(q => q.tone === "wait")`);
    await b.s("Input.dispatchMouseEvent", { type: "mouseMoved", x: g.x, y: g.y }); await b.sleep(700);
    const gold = await b.ev(`({ head: document.querySelector(".skycard .st").textContent, text: document.querySelector(".skycard").textContent, rows: [...document.querySelectorAll(".skycard .hr .w")].map(e => e.textContent) })`);
    assert.ok(/^Step 3 of 3 · Waiting for you/.test(gold.head), gold.head);
    assert.ok(gold.rows.includes("write held, waiting for you") && gold.rows.includes("asked for changes") === false && gold.rows.includes("approved"), JSON.stringify(gold.rows));
    // reduced layout of the others: the sky's questions still pour the stars out (kinds, time, spend), then home brings the crystals back
    await b.s("Input.dispatchMouseEvent", { type: "mouseMoved", x: 5, y: 300 });
    for (const v of ["kinds", "time", "spend"]) {
      await b.ev(`LF.sky.show(${JSON.stringify(v)})`); await b.sleep(1800);
      const st = await b.ev(`LF.sky.starSlots()`); assert.ok(st.drawn > 0, `the ${v} view draws the stars: ${JSON.stringify(st)}`);
      assert.strictEqual(await b.ev(`document.querySelectorAll("#tags .tag.ord.on").length`), 0, `the ${v} view has no order labels`);
    }
    await b.ev(`LF.sky.show("home")`); await b.sleep(2500); await settle(b);
    assert.ok((await b.ev(LABELS)).labels.length >= 6, "home brings the labels back");
    assert.deepStrictEqual(b.errors, [], "no page errors");
  } finally { await b.close(); }
}

// The witness shuts her eyes when nothing has happened for a while, wakes for anything (a job, a held write, the keys; not the pointer),
// and shows a ring of light while she is working. /api/state is steered from the page so the demo's jobs and cards come and go on cue.
const STEER = `(() => { const real = window.fetch.bind(window); window.__st = { running: 0, gate: [] };
  window.fetch = async (u, o) => { const r = await real(u, o); if (!String(u).startsWith("/api/state")) return r;
    const j = await r.json(); j.totals = Object.assign({}, j.totals, { running: window.__st.running }); j.gate = window.__st.gate; j.said = []; j.lanes = [];
    return new Response(JSON.stringify(j), { status: 200, headers: { "Content-Type": "application/json" } }); }; })()`;
async function witness(url, reduced) {
  const b = await launch(url, reduced ? [{ name: "prefers-reduced-motion", value: "reduce" }] : null, ENTERED + STEER);
  const until = async (expr, what, ms = 40000) => { for (let t = 0; t < ms; t += 100) { if (await b.ev(expr)) return; await b.sleep(100); } throw new Error("never: " + what + " (" + await b.ev(`JSON.stringify({ idle: LF.W.idle, working: LF.W.working, run: LF.W.running, wait: LF.W.waiting, speaking: LF.W.speaking, blink: LF.W.m.blink, work: LF.W.s.work })`) + ")"); };
  try {
    await until(`!!(window.LF && LF.W.ready && !document.getElementById("hud").hidden)`, "the page is up");
    await b.ev(`LF.cfg.idleMs = 800`);
    await until(`LF.W.running === 0 && LF.W.waiting === 0 && !LF.W.speaking && !LF.W.thinking`, "it is quiet");
    await until(`LF.W.idle`, "idle after the quiet");
    assert.strictEqual(await b.ev(`LF.W.working`), false, "idle is not working");
    // the lid shuts and rests shut, never between .25 and .75
    await until(`LF.W.m.blink === 1`, "the eyes shut");
    const rest = [];
    for (let i = 0; i < 12; i++) { rest.push(await b.ev(`LF.W.m.blink`)); await b.sleep(150); }
    assert.deepStrictEqual([...new Set(rest)], [1], "asleep, the lid rests at 1 and no random blink opens it");
    assert.ok(await b.ev(`LF.W.s.sleep`) > .9, "the head has drooped and presence dropped");
    assert.strictEqual(await b.ev(`LF.ring.visible`), false, "no ring while asleep");
    // the pointer alone does not wake her
    for (const x of [100, 600, 1100]) { await b.s("Input.dispatchMouseEvent", { type: "mouseMoved", x, y: 300 }); await b.sleep(150); }
    await b.sleep(500);
    assert.strictEqual(await b.ev(`LF.W.idle && LF.W.m.blink === 1`), true, "moving the pointer leaves her asleep");
    // a running job wakes her and the ring comes up
    await b.ev(`window.__st.running = 2`);
    await until(`LF.W.working && !LF.W.idle`, "a running job wakes her");
    await until(`LF.W.m.blink < .02`, "eyes open");
    await until(`LF.W.s.work > .95 && LF.ring.visible && LF.U.uWork.value > .95`, "the ring is up");
    if (reduced) {
      assert.strictEqual(await b.ev(`LF.U.uStill.value`), 1, "reduced motion: a still ring");
      const z0 = await b.ev(`LF.ring.spin.rotation.z`); await b.sleep(600);
      assert.strictEqual(await b.ev(`LF.ring.spin.rotation.z`), z0, "reduced motion: the ring does not orbit");
    } else {
      const z0 = await b.ev(`LF.ring.spin.rotation.z`); await b.sleep(600);
      assert.notStrictEqual(await b.ev(`LF.ring.spin.rotation.z`), z0, "the ring orbits");
    }
    // work ends: the ring fades; later she falls asleep again
    await b.ev(`window.__st.running = 0`);
    await until(`!LF.W.working && LF.W.s.work < .02 && !LF.ring.visible`, "the ring fades out");
    await until(`LF.W.idle && LF.W.m.blink === 1`, "asleep again");
    // a held write wakes her too (and keeps her up), a key wakes her, thinking shows the ring
    await b.ev(`window.__st.gate = [{ id: "steer1", repo: "r", branch: "b", kind: "push" }]`);
    await until(`LF.W.waiting === 1 && !LF.W.idle && LF.W.m.blink < .02`, "a held write wakes her");
    await b.sleep(1500);
    assert.strictEqual(await b.ev(`LF.W.idle`), false, "she stays awake while a write waits for the owner");
    await b.ev(`window.__st.gate = []`);
    await until(`LF.W.idle && LF.W.m.blink === 1`, "asleep once the write is gone");
    await b.key("a", "KeyA");
    await until(`!LF.W.idle && LF.W.m.blink < .02`, "a key wakes her");
    await until(`LF.W.idle && LF.W.m.blink === 1`, "asleep after the key");
    await b.ev(`LF.W.thinking = true`);
    await until(`LF.W.working && !LF.W.idle && LF.W.s.work > .95`, "thinking wakes her and shows the ring");
    await b.ev(`LF.W.thinking = false`);
    await until(`!LF.W.working && LF.W.s.work < .02`, "the ring fades when she stops thinking");
    assert.deepStrictEqual(b.errors, [], "no page errors");
  } finally { await b.close(); }
}

// Terminal 0 docked (doomtickle/passdesk-ops#213): the demo has none, so /api/state is given a `term` (and a waiting card, so the list has one).
// At every size, no row of the job list or the day's totals may sit on the caption; the pane is there and small; the Terminal key grows it and,
// with a real mouse, shrinks it again (its pointerdown must not shrink it first and the click grow it back).
const TERM = `(() => { const real = window.fetch.bind(window); window.fetch = async (u, o) => { const r = await real(u, o); if (!String(u).startsWith("/api/state")) return r;
  const j = await r.json(); j.term = { port: 1, running: true }; if (!(j.gate || []).length) j.gate = [{ id: "c1", repo: "example/app", branch: "factory/field-pane", kind: "push" }];
  return new Response(JSON.stringify(j), { headers: { "Content-Type": "application/json" } }); }; })()`;
const CLEAR = `(() => { const s = document.getElementById("said").getBoundingClientRect(), out = [];
  document.querySelectorAll(".work *, .today *").forEach(e => { if (e.children.length) return; const r = e.getBoundingClientRect(); if (!r.width || !r.height) return;
    if (r.left < s.right && r.right > s.left && r.top < s.bottom && r.bottom > s.top) out.push((e.textContent || e.tagName).trim().slice(0, 40) + " at " + [r.left, r.top, r.right, r.bottom].map(Math.round)); });
  return { out, said: document.getElementById("txt").textContent.length, pane: !document.getElementById("term-panel").hidden && document.getElementById("term-panel").getBoundingClientRect().height }; })()`;
async function pane(url) {
  const b = await launch(url, undefined, ENTERED + TERM);
  try {
    await ready(b, 1);
    for (const [w, h] of SIZES.concat([[1280, 800], [1366, 768]])) {
      await b.resize(w, h); await b.sleep(900);
      const a = await b.ev(CLEAR);
      assert.ok(a.pane > 0 && a.pane < h * .45, `${w}x${h}: the pane is docked and small (${a.pane})`);
      assert.ok(a.said > 0, `${w}x${h}: a caption is showing`);
      assert.deepStrictEqual(a.out, [], `${w}x${h}: the lists clear the caption`);
    }
    await b.resize(1440, 900); await b.sleep(900);
    const key = await b.ev(`(() => { const r = document.getElementById("term-key").getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()`);
    const grown = () => b.ev(`document.getElementById("term-panel").classList.contains("grown")`);
    await b.click(...key); await b.sleep(400);
    assert.strictEqual(await grown(), true, "the Terminal key grows the pane");
    await b.click(...key); await b.sleep(400);
    assert.strictEqual(await grown(), false, "a mouse click on the Terminal key shrinks it again");
    assert.deepStrictEqual(b.errors, [], "no page errors");
  } finally { await b.close(); }
}

// The delegation comet: a lane going idle -> running sends one flare from the witness to that job's crystal, running -> done
// sends one small light back, and under reduced motion nothing flies (a brief glow instead). /api/state's lanes are steered from the page.
const STEER_LANE = `(() => { const real = window.fetch.bind(window); window.__lane = { name: "steer-lane", job: null, state: "idle", role: "developer" };
  window.fetch = async (u, o) => { const r = await real(u, o); if (!String(u).startsWith("/api/state")) return r;
    const j = await r.json(); j.said = []; j.gate = []; j.lanes = [Object.assign({}, window.__lane)];
    return new Response(JSON.stringify(j), { status: 200, headers: { "Content-Type": "application/json" } }); }; })()`;
async function comet(url, reduced) {
  const b = await launch(url, reduced ? [{ name: "prefers-reduced-motion", value: "reduce" }] : null, ENTERED + STEER_LANE);
  const until = async (expr, what, ms = 30000) => { for (let t = 0; t < ms; t += 100) { if (await b.ev(expr)) return; await b.sleep(100); } throw new Error("never: " + what + " " + await b.ev(`JSON.stringify(LF.sky.cometInfo())`)); };
  const info = () => b.ev(`LF.sky.cometInfo()`);
  try {
    await until(`!!(window.LF && LF.W.ready && LF.sky && !document.getElementById("hud").hidden && LF.sky.packets().some(p => p.kind !== "gold"))`, "the page and a crystal are up");
    const id = await b.ev(`String(LF.sky.packets().find(p => p.kind !== "gold").id)`);
    await b.sleep(2600);   // the first poll primes the narration; later changes are events
    assert.deepStrictEqual(await info(), { active: 0, waiting: 0, out: 0, back: 0, still: 0 }, "nothing flies before any job starts");
    await b.ev(`window.__lane.job = ${JSON.stringify(id)}; window.__lane.state = "running"`);
    if (reduced) {
      await until(`LF.sky.cometInfo().still === 1`, "a glow instead of a flight");
      await b.sleep(600);
      assert.deepStrictEqual(await info(), { active: 0, waiting: 0, out: 0, back: 0, still: 1 }, "reduced motion: a glow, no comet");
      await b.ev(`window.__lane.state = "approved"`);
      await until(`LF.sky.cometInfo().still === 2`, "a glow when the job ends");
      assert.deepStrictEqual(await info(), { active: 0, waiting: 0, out: 0, back: 0, still: 2 }, "reduced motion: still no comet");
    } else {
      await until(`LF.sky.cometInfo().out === 1`, "the comet leaves for the crystal");
      assert.strictEqual((await info()).active, 1, "one comet in flight");
      await until(`LF.sky.cometInfo().active === 0`, "the comet lands and its thread fades", 8000);
      await b.sleep(2400);   // more polls with the same state: still one
      assert.deepStrictEqual(await info(), { active: 0, waiting: 0, out: 1, back: 0, still: 0 }, "one comet per event, and no return yet");
      await b.ev(`window.__lane.state = "approved"`);
      await until(`LF.sky.cometInfo().back === 1`, "a light comes back from the crystal");
      await until(`LF.sky.cometInfo().active === 0`, "the light reaches her", 8000);
      await b.sleep(2400);
      assert.deepStrictEqual(await info(), { active: 0, waiting: 0, out: 1, back: 1, still: 0 }, "one return light, nothing loops");
    }
    assert.deepStrictEqual(b.errors, [], "no page errors");
  } finally { await b.close(); }
}

const url = process.argv[2];
const only = process.env.FIELD_ONLY;
(only === "pane" ? pane(url) : only === "comet" ? comet(url, 0).then(() => comet(url, 1)) : only === "crystals" ? crystals(url) : only === "picking" ? picking(url) : only === "witness" ? witness(url, 0).then(() => witness(url, 1)) : layouts(url, 0).then(() => layouts(url, 65)).then(() => picking(url)).then(() => crystals(url)).then(() => witness(url, 0)).then(() => witness(url, 1)).then(() => pane(url)).then(() => comet(url, 0)).then(() => comet(url, 1)))
  .then(() => console.log("field browser checks passed"), e => { console.error(e.stack || e); process.exit(1); });
