// The factory floor (doomtickle/passdesk-ops#161): the Lanes bay as a pixel-art
// shop floor, drawn from the /api/state app.js already polls (it hands each one
// over as a "factory:state" event). Display only: no request of its own, no key,
// nothing that acts. Clicking a station asks app.js to open that lane's monitor
// ("factory:open-lane"), as clicking a module does. Agent-written text goes in
// with textContent only.
//
// Work flows left to right: the Coordinator's console dispatches a work order down
// the dispatch rail to a developer's in-tray; the developer's branch is parked on
// its out-tray; the hand-off rail carries it to a reviewer's in-tray; approved work
// drops from the reviewer onto the outbound belt and leaves by the OUT door. Every
// hop is read from lane state the page already has (a lane's state, task, reviews
// and tail), never from the server.
//
// Floor events (doomtickle/passdesk-ops#162): for the sound layer (sound.js) or a
// future shift ticket, the floor announces each crate's fate on document as a CustomEvent
// ("factory-floor:passed", "factory-floor:confiscated", "factory-floor:docked").
// They are fired from play() below, once per event, whether or not a crate is
// drawn, and never for the first poll's history. "docked" fires on a new branch line
// in a lane's log (the branch crate parked on its out-tray). detail is a frozen object of
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
  // Left to right: the console column (console, then any other roles' bay), the dispatch
  // rail, the developers' bay, the hand-off rail, the reviewers' bay, the drop rail, then
  // the arch, bin and door. The scene is as wide as the bay's shape asks (never under
  // 320): the extra width goes to the stations.
  const WALL = 46, ROW = 56, TOP = 10, BASE = 40, MIN_W = 320, MIN_CELL = 50;
  const RAIL_A = 57.5, BAYS_X = 62, CHUTE = [46, WALL + 48];   // the dispatch rail's centre; where the bays start; where work orders leave the console
  const CON_Y = WALL + 14, OTHER_Y = WALL + 82, OTHER_W = 48;   // the console's label line; the other-roles bay's top; its station width
  const RIGHT = 140;                                            // from the last bay's right edge to the scene's
  let W = MIN_W, RAIL_B = 0, RAIL_C = 0, ARCH_X = 262, DOOR_X = 306, BIN_X = 284, RAIL_END = 0;
  const MAX_CRATES = 14, SPEED = 70;         // crates in flight at once; units per second
  const MAX_PARKED = 4, MAX_BINNED = 5;      // crates drawn at the arch; confiscated crates kept in the bin

  let on = false, state = null, primed = false;
  let svg = null, layout = "", H = 0, stations = new Map(), parts = {};
  const seen = new Set(), branchSeen = new Map();
  const laneSeen = new Map();   // lane name -> { state, task, pair } as of the previous poll
  const parked = new Set();     // lanes with a branch crate on the out-tray
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
  // Developers and reviewers each get a bay; any other role shares the small one beside the console.
  const bayOf = lane => (lane.role === "developer" ? "dev" : lane.role === "reviewer" ? "rev" : "oth");

  // ---------- the scene
  // The widest grids (fewest rows) whose stations still fit, for the bay's aspect ratio.
  function plan(nd, nr, no) {
    const box = host.getBoundingClientRect(), aspect = box.width > 0 && box.height > 0 ? box.width / box.height : 1.6;
    const most = Math.max(nd, nr, 1), main = nd || nr;
    for (let rows = 1; rows <= most; rows++) {
      // Same rows, fewest columns: seven lanes are 4 + 3, not 6 + 1.
      const dcols = nd ? Math.ceil(nd / rows) : 0, rcols = nr ? Math.ceil(nr / rows) : 0;
      const mrows = Math.max(dcols ? Math.ceil(nd / dcols) : 0, rcols ? Math.ceil(nr / rcols) : 0);
      const h = WALL + Math.max(main ? TOP + mrows * ROW : 0, no ? OTHER_Y - WALL + 6 + no * ROW : 80) + BASE;
      const fixed = BAYS_X + (nd ? 6 : 0) + (nr ? 6 : 0) + (nd && nr ? 8 : 0) + RIGHT, cells = dcols + rcols;
      const w = Math.max(MIN_W, Math.round(h * aspect / 8) * 8);
      const cw = cells ? Math.floor((w - fixed) / cells) : 0;
      if (!cells) return { dcols, rcols, cw, mrows, w, h };
      if (cw >= MIN_CELL || rows === most) {
        const c = Math.max(cw, MIN_CELL);
        return { dcols, rcols, cw: c, mrows, w: fixed + cells * c, h };
      }
    }
    return { dcols: 0, rcols: 0, cw: 0, mrows: 0, w: MIN_W, h: WALL + 80 + BASE };
  }

  // A belt: a dark strip with rollers that turn while the factory works.
  function belt(x, y, w, h, vertical) {
    const g = S("g");
    g.append(R(x, y, w, h, "belt"));
    g.append(vertical ? S("line", { x1: x + w / 2, y1: y, x2: x + w / 2, y2: y + h }, "roll roll-v")
      : S("line", { x1: x, y1: y + h / 2, x2: x + w, y2: y + h / 2 }, "roll roll-h"));
    return g;
  }

  function build(lanes, p) {
    W = p.w; H = p.h;
    svg = S("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "xMidYMid meet", role: "group" }, "fl");
    stations = new Map();
    parts = {};
    const groups = { dev: [], rev: [], oth: [] };
    lanes.forEach((lane, i) => groups[bayOf(lane)].push([lane, i]));   // i is the lane's number: its key
    const cw = p.cw, devW = p.dcols * cw, revW = p.rcols * cw;
    const devX = BAYS_X, revX = devX + (p.dcols ? devW + 6 + 8 : 0);
    const endX = p.rcols ? revX + revW + 6 : p.dcols ? devX + devW + 6 : BAYS_X;
    RAIL_B = devX + devW + 6 + 3.5; RAIL_C = endX + 3.5;
    ARCH_X = RAIL_C + 50; BIN_X = ARCH_X + 28; DOOR_X = W - 18;
    const yOut = H - 32;
    RAIL_END = WALL + TOP + p.mrows * ROW;

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

    // Bays: a faint dashed outline and a painted label each. A shorter bay sits centred beside a taller one.
    const place = (list, name, x0, cols, cellW, top) => {
      if (!list.length) return;
      const rows = Math.ceil(list.length / cols), y0 = top;
      svg.append(R(x0 - 3, y0 - 6, cols * cellW + 6, rows * ROW + 5, "bay-line"), T(x0, y0 - 1, name, "bay-t"));
      list.forEach(([lane, i], k) => {
        const x = x0 + (k % cols) * cellW, y = y0 + Math.floor(k / cols) * ROW;
        const st = station(lane, i, cellW);
        st.g.setAttribute("transform", `translate(${x} ${y})`);
        st.x = x + cellW / 2; st.y = y + 40;
        st.inTray = [x + cellW / 2 - 18.5, y + 28]; st.outTray = [x + cellW / 2 + 18.5, y + 28];
        stations.set(lane.name, st);
        parts.stations.push(st.g);
      });
    };
    parts.stations = [];
    const center = rows => WALL + TOP + Math.round((p.mrows - rows) * ROW / 2);
    place(groups.dev, "DEVELOPERS", devX + 3, p.dcols, cw, center(p.dcols ? Math.ceil(groups.dev.length / p.dcols) : 0));
    place(groups.rev, "REVIEWERS", revX + 3, p.rcols, cw, center(p.rcols ? Math.ceil(groups.rev.length / p.rcols) : 0));
    const roles = [...new Set(groups.oth.map(([l]) => String(l.role || "")))];
    place(groups.oth, roles.length === 1 && roles[0] ? clip(roles[0].toUpperCase(), 12) : "OTHER", 4, 1, OTHER_W, OTHER_Y + 6);
    // Stations go on after the bays so no outline is drawn over a desk.
    const stationLayer = S("g", null, "stations");
    stationLayer.append(...parts.stations);

    // Rails, drawn as belts: dispatch (console to developers), hand-off (developers to
    // reviewers) and the drop from the reviewers to the outbound belt.
    const rails = S("g", null, "rails"), top = WALL + 14, label = (x, text) => T(x, RAIL_END + 5, text, "belt-t", "middle");
    if (p.dcols) {
      rails.append(belt(CHUTE[0] - 2, CHUTE[1] - 2, RAIL_A + 2.5 - (CHUTE[0] - 2), 4, false), belt(RAIL_A - 2.5, top, 5, RAIL_END - top, true), label(RAIL_A, "DISPATCH"));
    }
    if (p.dcols && p.rcols) rails.append(belt(RAIL_B - 2.5, top, 5, RAIL_END - top, true), label(RAIL_B, "HAND-OFF"));
    if (p.rcols) rails.append(belt(RAIL_C - 2.5, top, 5, yOut - top, true));
    rails.append(belt(50, yOut, DOOR_X - 50, 5, false), T(64, yOut + 12, "OUTBOUND ▸ OUT", "belt-t"));
    svg.append(rails);

    // The Coordinator's console: no worker, a screen that lights while Terminal 0 is attached,
    // a lamp, and the chute work orders leave by.
    const con = S("g", null, "console");
    con.append(T(26, CON_Y, "COORDINATOR", "label sm", "middle"));
    con.append(R(6, CON_Y + 4, 40, 60, "console-body"), R(10, CON_Y + 8, 32, 20, "console-scr"));
    for (const [y, w] of [[11, 18], [14, 24], [17, 14], [20, 21]]) con.append(R(12, CON_Y + y, w, 1, "term-l"));
    con.append(R(12, CON_Y + 24, 3, 2, "term-l"), R(42, CHUTE[1] - 4, 4, 8, "chute"), R(36, CHUTE[1] - 2, 4, 4, "lamp"));
    con.append(R(12, CON_Y + 38, 18, 7, "tickets"), R(14, CON_Y + 40, 12, .8, "tick-l"), R(14, CON_Y + 42.5, 9, .8, "tick-l"));
    con.append(R(10, CON_Y + 48, 32, 4, "desk-top"), R(11, CON_Y + 52, 30, 6, "desk-front"));
    parts.note = T(26, CON_Y + 62, "", "plate-t", "middle");
    con.append(parts.note);
    parts.console = con;
    svg.append(con);

    // The outbound belt runs under the X-ray arch to the OUT door; the bin is below it.
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

    parts.door = R(DOOR_X, yOut - 22, 14, 30, "door");
    svg.append(parts.door, T(DOOR_X + 7, yOut - 24, "OUT", "label sm", "middle"));
    const binBox = S("g", null, "bin");
    binBox.append(R(BIN_X, H - 12, 18, 10, "bin-body"), R(BIN_X - 1, H - 13, 20, 2, "bin-lip"), T(BIN_X + 9, H - 5, "!", "bin-t", "middle"));
    parts.bin = binBox;
    svg.append(binBox);

    svg.append(stationLayer);
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
    const st = { g, lane: lane.name, cx, cw, sig: "" };
    st.title = S("title");
    g.append(st.title, R(1, 1, cw - 2, ROW - 2, "hit"), R(cx - 21, 42, 42, 8, "mat"));
    g.append(...worker(cx, 30, `hue-${hue(lane.role)}`));
    g.append(R(cx - 16, 30, 32, 4, "desk-top"), R(cx - 15, 34, 30, 9, "desk-front"), R(cx - 13, 37, 8, 1, "drawer"), R(cx + 5, 37, 8, 1, "drawer"));
    // An in-tray left of the desk and an out-tray right of it; a finished branch is parked on the out-tray.
    g.append(R(cx - 22, 28, 7, 2, "tray"), R(cx + 15, 28, 7, 2, "tray"));
    st.park = crateShape(cx + 15, 22, "crate branch park off");
    g.append(st.park);
    // The placard: a paper note on the desk with the job in a few words, and under it, on a
    // reviewer's, the developer it is reviewing. Hidden until draw() has text for it.
    st.card = S("g", null, "card off");
    st.cardBg = R(cx - 17, 35, 34, 6, "card-bg");
    st.cardJob = T(cx, 39.5, "", "card-t", "middle");
    st.cardTag = T(cx, 44.5, "", "card-tag", "middle");
    st.card.append(st.cardBg, st.cardJob, st.cardTag);
    g.append(st.card);
    g.append(R(cx - 13, 27, 2, 3, "stand"), R(cx - 18, 17, 12, 10, "bezel"), R(cx - 17, 18, 10, 8, "scr"));
    g.append(R(cx + 12, 22, 2, 8, "post"), R(cx + 10, 19, 6, 3, "beacon"));
    // Props: CSS shows the one that matches the lane's state.
    const bubble = S("g", null, "p p-held");
    bubble.append(R(cx + 3, 0, 10, 10, "bubble"), R(cx + 4, 10, 2, 2, "bubble"), T(cx + 8, 8, "!", "bubble-t", "middle"));
    const flag = S("g", null, "p p-done");
    flag.append(R(cx + 24, 13, 1, 15, "pole"), R(cx + 25, 13, 7, 6, "flag"), T(cx + 28.5, 18, "✓", "flag-t", "middle"));
    const fix = S("g", null, "p p-revise");
    fix.append(R(cx + 20, 8, 15, 8, "fix"), T(cx + 27.5, 14.5, "FIX", "fix-t", "middle"), R(cx + 27, 16, 1, 12, "pole"));
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

  // Placard text. All of it is agent-written (the server only trims it), so it goes in as text.
  const CHAR_W = 1.8;   // scene units per character of the placard font
  const devTag = name => {
    const m = /^developer(?:-lane(\d+))?$/.exec(name);
    return m ? `DEV ${m[1] || 1}` : clip(name.toUpperCase(), 8);
  };
  // The rendered width of a text node, or an estimate while it isn't laid out (floor hidden).
  const width = (node, s) => {
    let w = 0;
    try { w = s ? node.getComputedTextLength() : 0; } catch (e) { /* not rendered */ }
    return w > 0 ? w : s.length * CHAR_W;
  };
  // Put s in node, cut with an ellipsis until it is at most max units wide; returns what fits.
  function fit(node, s, max) {
    let out = s;
    node.textContent = out;
    for (let n = s.length; n > 1 && width(node, out) > max; n--) node.textContent = out = clip(s, n - 1);
    return out;
  }
  function placard(st, lane) {
    const desk = typeof lane.desk === "string" ? lane.desk : "";
    const reviews = lane.role === "reviewer" && typeof lane.reviews === "string" ? lane.reviews : "";
    const sig = JSON.stringify([desk, reviews, lane.task, lane.state]);
    if (st.sig === sig) return;
    st.sig = sig;
    const maxW = Math.max(20, Math.min(36, st.cw - 4));
    const job = fit(st.cardJob, desk, maxW - 4), tag = reviews ? fit(st.cardTag, `▸ ${devTag(reviews)}`, maxW - 4) : fit(st.cardTag, "", 0);
    const w = Math.min(maxW, Math.ceil(Math.max(width(st.cardJob, job), width(st.cardTag, tag))) + 4);
    st.cardBg.setAttribute("x", st.cx - w / 2);
    st.cardBg.setAttribute("width", w);
    st.cardBg.setAttribute("height", tag ? (job ? 11 : 6) : 6);
    st.cardJob.setAttribute("y", job ? 39.5 : 0);
    st.cardTag.setAttribute("y", job ? 44.5 : 39.5);
    st.card.setAttribute("class", `card${job || tag ? "" : " off"}${tag ? " rev" : ""}`);
    const first = String(lane.task || "").split("\n")[0];
    st.title.textContent = job || tag ? clip(first, 300) : "";
  }

  // Placards are measured in the pixel font: measure again once it has loaded.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { stations.forEach(st => { st.sig = ""; }); if (on && state) draw(); });

  // A branch crate waits on the out-tray of a lane that has handed back a branch.
  function showPark(st) {
    const cls = `crate branch park${parked.has(st.lane) ? "" : " off"}`;
    if (st.park.getAttribute("class") !== cls) st.park.setAttribute("class", cls);
  }

  function draw() {
    const lanes = Array.isArray(state.lanes) ? state.lanes : [];
    const nd = lanes.filter(l => bayOf(l) === "dev").length, nr = lanes.filter(l => bayOf(l) === "rev").length;
    const p = plan(nd, nr, lanes.length - nd - nr);
    const want = JSON.stringify([lanes.map(l => [l.name, bayOf(l), l.role]), p]);
    if (want !== layout || !svg) { layout = want; build(lanes, p); }

    // A developer a running reviewer is reviewing gets a lit station.
    const watched = new Set(lanes.filter(l => l && l.role === "reviewer" && (l.state === "running" || l.state === "held")
      && typeof l.reviews === "string" && l.reviews).map(l => l.reviews));
    const counts = {};
    lanes.forEach(lane => {
      const st = stations.get(lane.name);
      if (!st) return;
      const s = PROP[lane.state] || "idle";
      counts[s] = (counts[s] || 0) + 1;
      const reviewing = watched.has(lane.name);
      const cls = `st s-${s}${reviewing ? " reviewed" : ""}`;
      if (st.g.getAttribute("class") !== cls) st.g.setAttribute("class", cls);
      placard(st, lane);
      showPark(st);
      const desk = typeof lane.desk === "string" && lane.desk ? `, ${lane.desk}` : "";
      const reviews = lane.role === "reviewer" && typeof lane.reviews === "string" && lane.reviews ? `, reviewing ${lane.reviews}` : "";
      const branch = parked.has(lane.name) ? ", branch ready" : "";
      st.g.setAttribute("aria-label", `Open lane ${lane.name}, ${LABEL[lane.state] || lane.state}${desk}${reviews}${branch}`);
    });

    const at = new Date((Number(state.now) || Date.now() / 1000) * 1000), hr = at.getHours();
    const shift = hr >= 7 && hr < 17 ? "day" : (hr >= 17 && hr < 20) || (hr >= 5 && hr < 7) ? "dusk" : "night";
    svg.setAttribute("class", `fl ${shift}${state.totals && state.totals.running ? " working" : ""}${motionOff.matches ? " still" : ""}`);
    const t = state.totals || {};
    parts.signTop.textContent = `LOIS WORKS · ${shift === "night" ? "NIGHT SHIFT" : shift === "dusk" ? "SWING SHIFT" : "DAY SHIFT"}`;
    parts.signBot.textContent = `JOBS ${t.jobs ?? 0} · RUN ${t.running ?? 0} · $${Number(t.cost || 0).toFixed(2)}`;

    const attached = !!(state.term && state.term.running);
    parts.console.setAttribute("class", `console${state.bridge ? " lit" : ""}${attached ? " in" : ""}`);
    parts.note.textContent = !state.bridge ? "BRIDGE DOWN" : attached ? "ON DUTY" : "BACK SOON";

    // Crates waiting at the arch: one per undecided held write (at most MAX_PARKED drawn),
    // each named by its write: tool · role · repo · branch.
    const gate = Array.isArray(state.gate) ? state.gate : [];
    const held = gate.filter(c => c && c.decided !== "approve" && c.decided !== "decline");
    const waiting = held.length;
    parts.arch.setAttribute("class", `arch${waiting ? " holding" : ""}`);
    const names = held.slice(0, MAX_PARKED).map(c => [c.tool, c.role, c.repo, c.branch].map(v => String(v || "")).filter(Boolean).join(" · ") || "held write");
    const parkedNames = JSON.stringify(names);
    if (parts.parked !== parkedNames) {
      parts.parked = parkedNames;
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
  // A work order: a paper ticket with two lines on it, not a crate.
  function ticketShape() {
    const g = S("g", null, "ticket");
    g.append(R(-3.5, -5, 7, 5, "ticket-bg"), R(-2.5, -4, 5, .6, "ticket-l"), R(-2.5, -2.4, 3.5, .6, "ticket-l"));
    return g;
  }
  // Move a crate (or, for cls "ticket", a work order) along points [[x, y], ...] at SPEED,
  // then call done(). It is drawn at 0,0 and moved with a transform, in SVG units.
  function travel(cls, pts, done) {
    if (crates >= MAX_CRATES) return false;
    const c = cls === "ticket" ? ticketShape() : crateShape(-3.5, -6, cls);
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

  // Where a broker call's crate starts: a running lane of that role, else its first lane, else the console.
  function origin(role) {
    const lanes = Array.isArray(state.lanes) ? state.lanes : [];
    const lane = lanes.find(l => l.role === role && (l.state === "running" || l.state === "held")) || lanes.find(l => l.role === role);
    const st = lane && stations.get(lane.name);
    return st ? [st.x, st.y] : [26, CON_Y + 66];
  }
  // Crates are drawn with their bottom centre on the point: this is the outbound belt's top.
  const OUT_Y = () => H - 32;
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

  // ---------- work flowing from the Coordinator to the door
  // Park a branch crate on a lane's out-tray (kept across polls, drawn by draw()); true when it is on screen.
  function park(name) {
    parked.add(name);
    const st = stations.get(name);
    if (st && on && svg) { showPark(st); return true; }
    return false;
  }
  function unpark(name) {
    parked.delete(name);
    const st = stations.get(name);
    if (st) showPark(st);
  }
  // Rail paths between trays. Each starts and ends on a tray, with the crate's feet on the rail's centre line.
  const dispatchPath = st => [CHUTE, [RAIL_A, CHUTE[1]], [RAIL_A, st.inTray[1]], st.inTray];
  const handoffPath = (dev, rev) => [dev.outTray, [RAIL_B, dev.outTray[1]], [RAIL_B, rev.inTray[1]], rev.inTray];
  const dropPath = rev => [rev.inTray, rev.outTray, [RAIL_C, rev.outTray[1]], [RAIL_C, OUT_Y()], [DOOR_X + 10, OUT_Y()]];

  // What changed in each lane since the last poll becomes a trip. History (the first poll, a
  // lane's first appearance, anything while the floor is hidden or motion is reduced) is
  // recorded but not played.
  function flow(animate) {
    // A revised branch goes back on its developer's tray unless the developer has already started on it.
    const repark = home => () => { const now = laneSeen.get(home); if (!now || now.state !== "running") park(home); };
    const later = [];   // un-animated re-parks wait until every lane's new state is recorded
    for (const lane of Array.isArray(state.lanes) ? state.lanes : []) {
      const was = laneSeen.get(lane.name), task = String(lane.task || "");
      const running = lane.state === "running";
      const pair = lane.role === "reviewer" && (running || lane.state === "held") && typeof lane.reviews === "string" ? lane.reviews : "";
      laneSeen.set(lane.name, { state: lane.state, task, pair });
      if (!primed || !was) continue;
      const st = stations.get(lane.name), dev = typeof lane.reviews === "string" ? stations.get(lane.reviews) : null;
      if (running && (was.state !== "running" || was.task !== task)) unpark(lane.name);   // it has taken its next job
      if (animate && st && lane.role === "developer" && running && task && was.task !== task) {
        flash(parts.console, "blink", 500);
        travel("ticket", dispatchPath(st));
      }
      if (st && dev && pair && pair !== was.pair) {      // the reviewer took the work: the developer's crate is gone, played or not
        if (animate) travel("crate branch", handoffPath(dev, st));
        unpark(dev.lane);
      }
      if (animate && st && lane.state === "approved" && was.state !== "approved") {
        travel("crate ok", dropPath(st), () => flash(parts.door, "open", 700));
      }
      if (st && dev && lane.state === "revise" && was.state !== "revise") {
        const home = dev.lane;
        if (!animate || !travel("crate fix", handoffPath(dev, st).reverse(), repark(home))) later.push(repark(home));
      }
    }
    later.forEach(f => f());
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
    flow(animate);
    // A reviewer already naming this lane means the hand-off is history: nothing is waiting on its tray.
    const beingReviewed = name => (Array.isArray(state.lanes) ? state.lanes : []).some(l => l && l.role === "reviewer" && l.reviews === name);
    for (const lane of Array.isArray(state.lanes) ? state.lanes : []) {
      let set = branchSeen.get(lane.name);
      const fresh = !set;
      if (fresh) { set = new Set(); branchSeen.set(lane.name, set); }
      for (const e of Array.isArray(lane.tail) ? lane.tail : []) {
        if (e.kind !== "branch") continue;
        const k = `${e.ts}\u0000${e.text}`;
        if (set.has(k)) continue;
        set.add(k);
        if (!primed && lane.state === "done" && !beingReviewed(lane.name)) park(lane.name);   // already waiting when the page loaded
        if (fresh || !primed) continue;
        const drawn = park(lane.name);
        announce("factory-floor:docked", { lane: String(lane.name), role: String(lane.role || ""), branch: clip(String(e.text || ""), 200), animated: drawn });
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
