// The factory floor (doomtickle/passdesk-ops#161): the Lanes bay as a pixel-art
// shop floor, drawn from the /api/state app.js already polls (it hands each one
// over as a "factory:state" event). Display only: no request of its own, no key,
// nothing that acts. Clicking a station asks app.js to open that lane's monitor
// ("factory:open-lane"), as clicking a module does. Agent-written text goes in
// with textContent only.
//
// Floor events (doomtickle/passdesk-ops#162): for a future sound layer or shift
// ticket, the floor announces each crate's fate on document as a CustomEvent
// ("factory-floor:passed", "factory-floor:confiscated", "factory-floor:docked").
// They are fired from play() below, once per event, whether or not a crate is
// drawn, and never for the first poll's history. detail is a frozen object of
// strings and booleans; its text is agent-written, so a listener must insert it
// with textContent only. Not bubbling, not cancelable: nothing can veto the floor.
"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const S = (tag, attrs, cls) => {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs || {}) n.setAttribute(k, attrs[k]);
    if (cls) n.setAttribute("class", cls);
    return n;
  };
  const T = (x, y, text, cls, anchor) => {
    const n = S("text", { x, y, "text-anchor": anchor || "start" }, cls);
    n.textContent = String(text);
    return n;
  };
  const R = (x, y, w, h, cls) => S("rect", { x, y, width: w, height: h }, cls);
  const clip = (s, n) => (s.length > n ? s.slice(0, Math.max(1, n - 1)) + "…" : s);

  const host = $("floor"), key = $("floor-key"), bay = document.querySelector(".lanes-bay");
  if (!host || !key || !bay) return;
  const STORE = "fc1984-floor";
  const motionOff = matchMedia("(prefers-reduced-motion: reduce)");

  // Scene geometry, in SVG units (one unit is one "pixel" of the art).
  // The scene is as wide as the bay's shape asks (never under 320): the extra width
  // goes to the stations, between the booth and the gatehouse.
  const WALL = 46, ROW = 56, BASE = 40, ZONE_X = 66, DOCK_X = 18, MIN_W = 320, MIN_CELL = 46;
  let W = MIN_W, ZONE_W = 188, ARCH_X = 262, DOOR_X = 306, BIN_X = 284;
  const MAX_CRATES = 14, SPEED = 70;         // crates in flight at once; units per second
  const MAX_PARKED = 4, MAX_BINNED = 5;      // crates drawn at the arch; confiscated crates kept in the bin

  let on = false, state = null, primed = false;
  let svg = null, layout = "", H = 0, stations = new Map(), parts = {};
  const seen = new Set(), branchSeen = new Map();
  let crates = 0;
  const binned = [];   // the last MAX_BINNED confiscations' event lines; page memory only, gone on reload

  // ---------- on / off
  function setOn(v, save) {
    on = v;
    key.setAttribute("aria-pressed", String(v));
    bay.classList.toggle("floor-on", v);
    host.hidden = !v;
    if (save) { try { localStorage.setItem(STORE, v ? "1" : "0"); } catch (e) { /* no storage: this tab only */ } }
    if (v && state) draw();
  }
  key.addEventListener("click", () => setOn(!on, true));
  try { if (localStorage.getItem(STORE) === "1") setOn(true, false); } catch (e) { /* no storage: off */ }

  // ---------- roles: a hard-hat colour each (the built-in roles fixed, others hashed)
  const HUE = { developer: 0, reviewer: 1, researcher: 2 };
  function hue(role) {
    if (role in HUE) return HUE[role];
    let h = 0;
    for (const ch of role) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return 3 + (h % 4);
  }

  // ---------- the scene
  // The widest grid (fewest rows) whose stations still fit, for the bay's aspect ratio.
  function plan(n) {
    const box = host.getBoundingClientRect(), aspect = box.width > 0 && box.height > 0 ? box.width / box.height : 1.6;
    for (let cols = n; cols >= 1; cols--) {
      const rows = Math.ceil(n / cols), h = WALL + rows * ROW + BASE, w = Math.max(MIN_W, Math.round(h * aspect / 8) * 8);
      // Same rows, fewest columns: seven lanes are 4 + 3, not 6 + 1.
      if ((w - 132) / cols >= MIN_CELL || cols === 1) return { cols: Math.ceil(n / rows), rows, w, h };
    }
    return { cols: 1, rows: n, w: MIN_W, h: WALL + n * ROW + BASE };
  }

  function build(lanes, p) {
    const cols = p.cols;
    W = p.w; H = p.h; ZONE_W = W - 132; ARCH_X = W - 58; DOOR_X = W - 14; BIN_X = W - 36;
    svg = S("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "xMidYMid meet", role: "group" }, "fl");
    stations = new Map();
    parts = {};

    // Back wall: brick, three windows on the sky, the shift sign, hazard stripes.
    svg.append(R(0, 0, W, WALL, "wall"));
    for (let y = 4; y < WALL - 6; y += 6) svg.append(S("line", { x1: 0, y1: y, x2: W, y2: y }, "mortar"));
    parts.windows = [];
    for (const x of [14, W - 92, W - 50]) {
      const g = S("g", null, "win");
      g.append(R(x, 8, 34, 20, "sky"));
      for (const [sx, sy] of [[4, 4], [15, 9], [27, 3], [22, 14], [9, 13]]) g.append(R(x + sx, 8 + sy, 1, 1, "star"));
      g.append(R(x + 16, 8, 2, 20, "mullion"), R(x, 17, 34, 2, "mullion"), R(x - 1, 27, 36, 2, "sill"));
      parts.windows.push(g);
      svg.append(g);
    }
    const sign = S("g", null, "sign");
    sign.append(R(W / 2 - 75, 6, 150, 26, "sign-bg"), R(W / 2 - 73, 8, 146, 22, "sign-face"));
    parts.signTop = T(W / 2, 17, "LOIS WORKS", "sign-t", "middle");
    parts.signBot = T(W / 2, 26, "", "sign-b", "middle");
    sign.append(parts.signTop, parts.signBot);
    svg.append(sign);
    for (let x = 0; x < W; x += 8) svg.append(S("polygon", { points: `${x},${WALL - 4} ${x + 4},${WALL - 4} ${x + 8},${WALL} ${x + 4},${WALL}` }, "hazard"));

    // Floor: concrete with expansion joints.
    svg.append(R(0, WALL, W, H - WALL, "concrete"));
    for (let x = 0; x <= W; x += 32) svg.append(S("line", { x1: x, y1: WALL, x2: x, y2: H }, "joint"));
    for (let y = WALL + 28; y < H; y += 28) svg.append(S("line", { x1: 0, y1: y, x2: W, y2: y }, "joint"));

    // The foreman's booth: the Coordinator.
    const booth = S("g", null, "booth");
    booth.append(R(4, WALL + 6, 56, 48, "booth-wall"), R(8, WALL + 10, 48, 24, "booth-glass"));
    const foreman = S("g", null, "foreman");
    foreman.append(...worker(32, WALL + 34, "hue-f"));
    booth.append(foreman, R(12, WALL + 34, 40, 4, "desk-top"), R(13, WALL + 38, 38, 12, "desk-front"));
    booth.append(R(14, WALL + 24, 11, 9, "scr scr-coord"), R(18, WALL + 33, 3, 1, "stand"));
    parts.boothNote = T(32, WALL + 47, "", "plate-t", "middle");
    booth.append(parts.boothNote, T(32, WALL + 4, "FOREMAN", "label", "middle"));
    parts.booth = booth;
    svg.append(booth);

    // The broker's gatehouse: guard, X-ray arch over the outbound belt, the door out, the bin.
    const gate = S("g", null, "gatehouse");
    const gx = W - 58;
    gate.append(R(gx, WALL + 6, 54, 40, "booth-wall"), R(gx + 4, WALL + 10, 46, 18, "booth-glass guard-glass"));
    gate.append(...worker(gx + 27, WALL + 30, "hue-g"), R(gx + 8, WALL + 28, 38, 4, "desk-top"), R(gx + 9, WALL + 32, 36, 10, "desk-front"));
    gate.append(T(gx + 27, WALL + 4, "BROKER", "label", "middle"), T(gx + 27, WALL + 40, "POLICY · AUDIT", "plate-t", "middle"));
    svg.append(gate);

    const belts = S("g", null, "belts");
    const yOut = H - 32, yDock = H - 16;
    belts.append(R(ZONE_X - 6, yOut, DOOR_X - ZONE_X + 6, 5, "belt"), S("line", { x1: ZONE_X - 6, y1: yOut + 2.5, x2: DOOR_X, y2: yOut + 2.5 }, "roll roll-out"));
    belts.append(R(DOCK_X + 12, yDock, ZONE_X + ZONE_W - DOCK_X - 12, 5, "belt"), S("line", { x1: DOCK_X + 12, y1: yDock + 2.5, x2: ZONE_X + ZONE_W, y2: yDock + 2.5 }, "roll roll-dock"));
    belts.append(T(ZONE_X, yOut - 2, "OUTBOUND ▸ BROKER", "belt-t"), T(ZONE_X + ZONE_W, yDock + 12, "◂ TO DOCK", "belt-t", "end"));
    svg.append(belts);

    const arch = S("g", null, "arch");
    arch.append(R(ARCH_X - 2, yOut - 18, 3, 24, "arch-post"), R(ARCH_X + 13, yOut - 18, 3, 24, "arch-post"), R(ARCH_X - 3, yOut - 21, 20, 5, "arch-top"));
    arch.append(R(ARCH_X + 1, yOut - 15, 12, 18, "arch-beam"), R(ARCH_X + 5, yOut - 20, 6, 2, "arch-lamp"));
    arch.append(T(ARCH_X + 7, yOut - 23, "X-RAY", "label sm", "middle"));
    parts.arch = arch;
    svg.append(arch);
    parts.queue = S("g", null, "queue");
    svg.append(parts.queue);
    parts.pile = S("g", null, "pile");
    svg.append(parts.pile);

    svg.append(R(DOOR_X, yOut - 22, 14, 30, "door"), T(DOOR_X + 7, yOut - 24, "OUT", "label sm", "middle"));
    const bin = S("g", null, "bin");
    bin.append(R(BIN_X, H - 12, 18, 10, "bin-body"), R(BIN_X - 1, H - 13, 20, 2, "bin-lip"), T(BIN_X + 9, H - 5, "!", "bin-t", "middle"));
    parts.bin = bin;
    svg.append(bin);

    const dock = S("g", null, "dock");
    dock.append(R(2, H - 36, 26, 34, "dock-frame"), R(5, H - 33, 20, 28, "dock-door"));
    for (let y = H - 30; y < H - 6; y += 4) dock.append(S("line", { x1: 5, y1: y, x2: 25, y2: y }, "slat"));
    parts.dockName = T(15, H - 38, "", "label sm", "middle");
    dock.append(T(15, H - 44, "DOCK", "label", "middle"), parts.dockName);
    parts.dock = dock;
    svg.append(dock);

    // One station per lane, in lane order, numbered like the modules.
    const cw = ZONE_W / cols;
    lanes.forEach((lane, i) => {
      const x = ZONE_X + (i % cols) * cw, y = WALL + Math.floor(i / cols) * ROW;
      const st = station(lane, i, cw);
      st.g.setAttribute("transform", `translate(${x} ${y})`);
      st.x = x + cw / 2; st.y = y + 40;
      stations.set(lane.name, st);
      svg.append(st.g);
    });
    parts.fx = S("g", null, "fx");
    svg.append(parts.fx);
    host.replaceChildren(svg);
  }

  // A seated worker in a hard hat, centred on cx with the desk line at y.
  function worker(cx, y, hueCls) {
    const head = S("g", null, "hd");
    head.append(R(cx - 3, y - 16, 6, 6, "skin"), R(cx - 2, y - 14, 1, 1, "eye"), R(cx + 1, y - 14, 1, 1, "eye"),
      R(cx - 4, y - 19, 8, 3, `hat ${hueCls}`), R(cx - 5, y - 17, 10, 1, `brim ${hueCls}`));
    return [R(cx - 6, y - 10, 12, 12, "chair"), R(cx - 5, y - 9, 10, 9, "overalls"), R(cx - 2, y - 9, 4, 2, "shirt"),
      head, R(cx - 7, y - 7, 2, 7, "arm arm-l"), R(cx + 5, y - 7, 2, 7, "arm arm-r")];
  }

  function station(lane, i, cw) {
    const cx = cw / 2, g = S("g", { tabindex: "0", role: "button" }, "st");
    const st = { g, lane: lane.name };
    g.append(R(1, 1, cw - 2, ROW - 2, "hit"), R(cx - 21, 42, 42, 8, "mat"));
    g.append(...worker(cx, 30, `hue-${hue(lane.role)}`));
    g.append(R(cx - 16, 30, 32, 4, "desk-top"), R(cx - 15, 34, 30, 9, "desk-front"), R(cx - 13, 37, 8, 1, "drawer"), R(cx + 5, 37, 8, 1, "drawer"));
    g.append(R(cx - 13, 27, 2, 3, "stand"), R(cx - 18, 17, 12, 10, "bezel"), R(cx - 17, 18, 10, 8, "scr"));
    g.append(R(cx + 12, 22, 2, 8, "post"), R(cx + 10, 19, 6, 3, "beacon"));
    // Props: CSS shows the one that matches the lane's state.
    const bubble = S("g", null, "p p-held");
    bubble.append(R(cx + 3, 0, 10, 10, "bubble"), R(cx + 4, 10, 2, 2, "bubble"), T(cx + 8, 8, "!", "bubble-t", "middle"));
    const flag = S("g", null, "p p-done");
    flag.append(R(cx + 17, 10, 1, 20, "pole"), R(cx + 18, 10, 9, 6, "flag"), T(cx + 22.5, 15, "✓", "flag-t", "middle"));
    const fix = S("g", null, "p p-revise");
    fix.append(R(cx + 15, 12, 16, 8, "fix"), T(cx + 23, 18.5, "FIX", "fix-t", "middle"), R(cx + 22, 20, 1, 10, "pole"));
    const smoke = S("g", null, "p p-fail");
    for (const [dx, d] of [[-14, 0], [-11, 1], [-15, 2]]) smoke.append(S("circle", { cx: cx + dx, cy: 15, r: 2.5 }, `puff d${d}`));
    const zz = S("g", null, "p p-idle");
    zz.append(T(cx + 6, 12, "z", "zz d0"), T(cx + 10, 7, "z", "zz d1"), T(cx + 14, 3, "Z", "zz d2"));
    g.append(bubble, flag, fix, smoke, zz);
    if (i < 9) g.append(R(2, 2, 9, 9, "kc"), T(6.5, 9, i + 1, "kc-t", "middle"));
    st.name = T(cx, 54, clip(lane.name, Math.floor(cw / 4)), "name", "middle");
    g.append(st.name);
    const open = () => document.dispatchEvent(new CustomEvent("factory:open-lane", { detail: lane.name }));
    g.addEventListener("click", open);
    g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    return st;
  }

  // ---------- each poll
  const LABEL = { running: "working", held: "held at the gate", revise: "asked to revise", done: "done", approved: "approved",
    idle: "idle", cancelled: "cancelled", failed: "failed" };
  const PROP = { running: "running", held: "held", revise: "revise", done: "done", approved: "done", idle: "idle", cancelled: "fail", failed: "fail" };

  function draw() {
    const lanes = Array.isArray(state.lanes) ? state.lanes : [];
    const p = plan(Math.max(lanes.length, 1));
    const want = JSON.stringify([lanes.map(l => [l.name, l.role]), p]);
    if (want !== layout || !svg) { layout = want; build(lanes, p); }

    const counts = {};
    lanes.forEach(lane => {
      const st = stations.get(lane.name);
      if (!st) return;
      const s = PROP[lane.state] || "idle";
      counts[s] = (counts[s] || 0) + 1;
      const cls = `st s-${s}`;
      if (st.g.getAttribute("class") !== cls) st.g.setAttribute("class", cls);
      st.g.setAttribute("aria-label", `Open lane ${lane.name}, ${LABEL[lane.state] || lane.state}`);
    });

    const at = new Date((Number(state.now) || Date.now() / 1000) * 1000), hr = at.getHours();
    const shift = hr >= 7 && hr < 17 ? "day" : (hr >= 17 && hr < 20) || (hr >= 5 && hr < 7) ? "dusk" : "night";
    svg.setAttribute("class", `fl ${shift}${state.totals && state.totals.running ? " working" : ""}${motionOff.matches ? " still" : ""}`);
    const t = state.totals || {};
    parts.signTop.textContent = `LOIS WORKS · ${shift === "night" ? "NIGHT SHIFT" : shift === "dusk" ? "SWING SHIFT" : "DAY SHIFT"}`;
    parts.signBot.textContent = `JOBS ${t.jobs ?? 0} · RUN ${t.running ?? 0} · $${Number(t.cost || 0).toFixed(2)}`;
    parts.dockName.textContent = clip(String(state.project || ""), 9);

    const attached = !!(state.term && state.term.running);
    parts.booth.setAttribute("class", `booth${state.bridge ? " lit" : ""}${attached ? " in" : ""}`);
    parts.boothNote.textContent = !state.bridge ? "BRIDGE DOWN" : attached ? "ON DUTY" : "BACK SOON";

    // Crates waiting at the arch: one per undecided held write (at most MAX_PARKED drawn),
    // each named by its write: tool · role · repo · branch.
    const gate = Array.isArray(state.gate) ? state.gate : [];
    const held = gate.filter(c => c && c.decided !== "approve" && c.decided !== "decline");
    const waiting = held.length;
    parts.arch.setAttribute("class", `arch${waiting ? " holding" : ""}`);
    const names = held.slice(0, MAX_PARKED).map(c => [c.tool, c.role, c.repo, c.branch].map(v => String(v || "")).filter(Boolean).join(" · ") || "held write");
    const parked = JSON.stringify(names);
    if (parts.parked !== parked) {
      parts.parked = parked;
      parts.queue.replaceChildren(...names.map((name, j) => crateShape(ARCH_X - 11 - j * 9, H - 38, "crate held", name)));
    }
    drawPile();

    const summary = [];
    if (counts.running) summary.push(`${counts.running} working`);
    if (counts.held) summary.push(`${counts.held} held at the gate`);
    if (counts.fail) summary.push(`${counts.fail} failed`);
    if (counts.idle) summary.push(`${counts.idle} idle`);
    svg.setAttribute("aria-label", `Factory floor: ${lanes.length} lane${lanes.length === 1 ? "" : "s"}${summary.length ? ", " + summary.join(", ") : ""}. ${waiting} write${waiting === 1 ? "" : "s"} waiting at the broker.`);
  }

  // ---------- crates
  // With a label the crate can be hovered (an SVG <title>) and tabbed to, and says the same to a screen reader.
  function crateShape(x, y, cls, label) {
    const g = S("g", null, cls || "crate");
    if (label) {
      g.setAttribute("tabindex", "0");
      g.setAttribute("role", "img");
      g.setAttribute("aria-label", label);
      const title = S("title");
      title.textContent = label;
      g.append(title);
    }
    g.append(R(x, y, 7, 6, "crate-box"), S("line", { x1: x, y1: y, x2: x + 7, y2: y + 6 }, "crate-x"), S("line", { x1: x + 7, y1: y, x2: x, y2: y + 6 }, "crate-x"));
    return g;
  }
  // Move a crate along points [[x, y], ...] at SPEED, then call done(). The crate is
  // drawn at 0,0 and moved with a transform, in SVG units.
  function travel(cls, pts, done) {
    if (crates >= MAX_CRATES) return false;
    const c = crateShape(-3.5, -6, cls);
    parts.fx.append(c);
    crates++;
    let total = 0;
    const lens = pts.slice(1).map((p, k) => { const d = Math.hypot(p[0] - pts[k][0], p[1] - pts[k][1]); total += d; return d; });
    let acc = 0;
    const frames = pts.map((p, k) => {
      if (k) acc += lens[k - 1];
      return { transform: `translate(${p[0]}px, ${p[1]}px)`, offset: total ? acc / total : k / Math.max(1, pts.length - 1) };
    });
    const anim = c.animate(frames, { duration: Math.max(400, (total / SPEED) * 1000), easing: "linear", fill: "forwards" });
    anim.onfinish = () => { c.remove(); crates--; if (done) done(); };
    anim.oncancel = () => { crates--; };
    return true;
  }
  // The bin's pile: the last MAX_BINNED confiscated crates, two rows tucked behind the bin's lip.
  function drawPile() {
    if (!parts.pile) return;
    const key = JSON.stringify(binned);
    if (parts.piled === key) return;
    parts.piled = key;
    parts.pile.replaceChildren(...binned.map((label, i) => {
      const row = i < 3 ? 0 : 1, col = row ? i - 3 : i;
      return crateShape(BIN_X - 0.5 + (row ? 3 : 0) + col * 6.5, H - 17 - row * 5, "crate bad", label);
    }));
  }
  function binCrate(label) {
    binned.push(label);
    if (binned.length > MAX_BINNED) binned.shift();
    drawPile();
  }
  const BAD = new Set(["DENY", "NO", "FAILED"]), GOOD = new Set(["ALLOW", "OK", "WRITTEN"]);
  function announce(name, detail) {
    document.dispatchEvent(new CustomEvent(name, { bubbles: false, cancelable: false, detail: Object.freeze(detail) }));
  }
  const line = ev => `${ev.t} ${ev.kind} ${ev.text}`;   // the printer's line, as app.js titles it
  function flash(node, cls, ms) {
    node.classList.remove(cls);
    void node.getBoundingClientRect();
    node.classList.add(cls);
    setTimeout(() => node.classList.remove(cls), ms);
  }
  function stamp() {
    const g = S("g", { transform: `translate(${BIN_X - 16} ${H - 18}) rotate(-8)` }, "stamp");
    g.append(R(-30, -7, 60, 10, "stamp-box"), T(0, 1, "CONFISCATED", "stamp-t", "middle"));
    parts.fx.append(g);
    g.animate([{ opacity: 0, transform: `translate(${BIN_X - 16}px, ${H - 18}px) rotate(-8deg) scale(1.6)` },
               { opacity: 1, transform: `translate(${BIN_X - 16}px, ${H - 18}px) rotate(-8deg) scale(1)`, offset: 0.12 },
               { opacity: 1, transform: `translate(${BIN_X - 16}px, ${H - 18}px) rotate(-8deg) scale(1)`, offset: 0.8 },
               { opacity: 0, transform: `translate(${BIN_X - 16}px, ${H - 18}px) rotate(-8deg) scale(1)` }],
              { duration: 2200, easing: "ease-out" }).onfinish = () => g.remove();
    flash(parts.bin, "hit-bin", 600);
  }
  function pop(st, text) {
    const n = T(st.x, st.y - 34, text, "pop", "middle");
    parts.fx.append(n);
    n.animate([{ opacity: 1, transform: "translateY(0px)" }, { opacity: 0, transform: "translateY(-12px)" }], { duration: 1400, easing: "ease-out" })
      .onfinish = () => n.remove();
  }

  // Where a broker call's crate starts: a running lane of that role, else its first lane, else the booth.
  function origin(role) {
    const lanes = Array.isArray(state.lanes) ? state.lanes : [];
    const lane = lanes.find(l => l.role === role && (l.state === "running" || l.state === "held")) || lanes.find(l => l.role === role);
    const st = lane && stations.get(lane.name);
    return st ? [st.x, st.y] : [62, WALL + 50];
  }
  // Crates are drawn with their bottom centre on the point: these are the belts' tops.
  const OUT_Y = () => H - 32, DOCK_Y = () => H - 16;
  function toArch(role, then) {
    const [x, y] = origin(role), oy = OUT_Y();
    return travel("crate", [[x, y], [x, oy], [ARCH_X - 4, oy]], then);
  }
  function pass(fromX) {
    const oy = OUT_Y();
    flash(parts.arch, "ok", 500);
    return travel("crate ok", [[fromX, oy], [DOOR_X + 10, oy]]);
  }
  function confiscate(fromX, label) {
    const oy = OUT_Y();
    flash(parts.arch, "no", 700);
    const landed = () => { stamp(); binCrate(label); };
    const moved = travel("crate bad", [[fromX, oy], [BIN_X - 2, oy - 10], [BIN_X + 9, H - 8]], landed);
    if (!moved) binCrate(label);
    return moved;
  }

  // Draws the event's crate; true when one was launched (false: none free, or nothing to draw).
  function broker(ev) {
    const words = String(ev.text || "").split(" ");
    const role = words[1] || "";
    switch (ev.kind) {
      case "ALLOW": return toArch(role, () => pass(ARCH_X - 4));
      case "DENY": {
        if (toArch(role, () => confiscate(ARCH_X - 4, line(ev)))) return true;
        binCrate(line(ev));   // too many crates moving: bin it unseen
        return false;
      }
      case "HELD": toArch(role); return false;    // it waits in the queue (drawn from the gate cards)
      case "OK": case "WRITTEN": return pass(ARCH_X - 12);
      case "NO": case "FAILED": return confiscate(ARCH_X - 12, line(ev));
      case "JOB": {
        const st = stations.get(words[0]);
        if (st && words[1] === "done") pop(st, "+1");
        return false;
      }
      default: return false;
    }
  }
  function docked(st) {
    const dy = DOCK_Y();
    return travel("crate branch", [[st.x, st.y], [st.x, dy], [DOCK_X + 8, dy]], () => flash(parts.dock, "open", 900));
  }

  // New events and branches since the last poll become crates. The first poll (and
  // anything that happened while the floor was hidden) is history: seen, not played.
  function play() {
    const animate = primed && on && svg && !motionOff.matches;
    for (const ev of Array.isArray(state.events) ? state.events : []) {
      const k = `${ev.t}\u0000${ev.kind}\u0000${ev.text}`;
      if (seen.has(k)) continue;
      seen.add(k);
      let moved = false;
      if (animate) moved = broker(ev);
      else if (primed && BAD.has(ev.kind)) binCrate(line(ev));   // floor hidden or motion reduced: drawn in place, not played
      if (!primed) continue;                                     // first poll: history, no event either
      const detail = { kind: String(ev.kind), role: String(ev.text || "").split(" ")[1] || "", line: clip(line(ev), 300), animated: moved };
      if (GOOD.has(ev.kind)) announce("factory-floor:passed", detail);
      else if (BAD.has(ev.kind)) announce("factory-floor:confiscated", detail);
    }
    if (seen.size > 2000) { const keep = [...seen].slice(-500); seen.clear(); keep.forEach(k => seen.add(k)); }
    for (const lane of Array.isArray(state.lanes) ? state.lanes : []) {
      let set = branchSeen.get(lane.name);
      const fresh = !set;
      if (fresh) { set = new Set(); branchSeen.set(lane.name, set); }
      for (const e of Array.isArray(lane.tail) ? lane.tail : []) {
        if (e.kind !== "branch") continue;
        const k = `${e.ts}\u0000${e.text}`;
        if (set.has(k)) continue;
        set.add(k);
        const st = stations.get(lane.name);
        if (fresh || !primed) continue;
        announce("factory-floor:docked", { lane: String(lane.name), role: String(lane.role || ""), branch: clip(String(e.text), 200), animated: !!(animate && st && docked(st)) });
      }
    }
    primed = true;
  }

  // A new bay shape may want another grid: redraw (draw() rebuilds only if it does).
  if (window.ResizeObserver) new ResizeObserver(() => { if (on && state) draw(); }).observe(host);

  document.addEventListener("factory:state", e => {
    state = e.detail;
    if (!state || typeof state !== "object") return;
    if (on) draw();
    play();
  });
})();
