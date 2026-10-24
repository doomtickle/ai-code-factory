// The factory floor (doomtickle/passdesk-ops#161, redrawn for #163): the Lanes bay as one
// module of the rack, a brushed panel with an amber tube in it, drawn from the /api/state
// app.js already polls (it hands each one over as a "factory:state" event). Display only:
// no request of its own, no key, nothing that acts. Clicking a station asks app.js to open
// that lane's monitor ("factory:open-lane"), as clicking a module does. Agent-written text
// goes in with textContent only.
//
// Inside the tube one loop track carries every trip, clockwise. The Coordinator's dispatch
// console feeds the track on the left; developers are docked along the top side, reviewers
// along the bottom, any other role on the right side. The gate (the broker) stands on the
// bottom side after the reviewers, and the OUT portal drops off the track just past it. A
// work order rides from dispatch to a developer; the developer's branch is parked on its
// console; the hand-off rides round the right end to a reviewer; approved work rides on
// through the gate and out of the portal; a revise rides back the way it came. Every hop is
// read from lane state the page already has (a lane's state, task, reviews and tail), never
// from the server. The people are programs: a seated person in a jacket tinted by the role's colour, with
// circuit lines in that colour and an identity disc behind the head that carries the state.
//
// Floor events (doomtickle/passdesk-ops#162): for the sound layer (sound.js) or a
// future shift ticket, the floor announces each crate's fate on document as a CustomEvent
// ("factory-floor:passed", "factory-floor:confiscated", "factory-floor:docked").
// They are fired from play() below, once per event, whether or not a crate is
// drawn, and never for the first poll's history. "docked" fires on a new branch line
// in a lane's log (the branch crate parked on its console). detail is a frozen object of
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
  const L = (x1, y1, x2, y2, cls) => S("line", { x1, y1, x2, y2 }, cls);
  const clip = (s, n) => (s.length > n ? s.slice(0, Math.max(1, n - 1)) + "…" : s);

  const host = $("floor"), key = $("floor-key"), bay = document.querySelector(".lanes-bay");
  if (!host || !key || !bay) return;
  const STORE = "fc1984-floor";
  const motionOff = matchMedia("(prefers-reduced-motion: reduce)");

  // Scene geometry, in SVG units (one unit is one "pixel" of the art). The panel is H
  // tall and as wide as the bay's shape asks (never under MIN_W, never too narrow for its
  // stations); the extra width spreads the stations along the track.
  const H = 228, MIN_W = 320, MIN_CELL = 50, MAX_CELL = 80, MAX_ASPECT = 2.2;   // a station's cell: at least MIN_CELL wide, never wider than MAX_CELL
  const GLASS_X = 13, GLASS_Y = 33, GLASS_B = 190;         // the tube's glass: GLASS_X..W - GLASS_X, GLASS_Y..GLASS_B
  const DEV_H = 52, REV_H = 41, OTHER_W = 50;             // station cell heights above and below the track; other roles' cell width
  const RAD = 24, HALF = 4.5;                             // the track's corner radius and half width
  const TY = 90, BY = TY + 2 * RAD + 4;                   // the track's top and bottom centre lines: a shallow loop, its ends near-semicircles
  const MID = (TY + BY) / 2;                              // where a light bridge crosses the loop
  const LX = 70;                                          // the track's left side; the right side (RX) follows W
  const CON_X = 40, CON_Y = TY - 12, CHUTE = [CON_X + 20, CON_Y + 6];   // the dispatch console, above the track's left end: centre, top, where work orders leave it
  const ARCH_X = LX + 54, OUT_X = LX + 30;                // the gate on the bottom side; the OUT portal's drop
  const BIN_X = ARCH_X - 12, PILE_Y = BY + 38;            // the derez field under the gate: binned crates pile here
  const QUEUE_X = ARCH_X + 12;                            // held crates wait here, before the gate (the bottom side flows leftward)
  let W = MIN_W, RX = 0;
  const MAX_CRATES = 14, SPEED = 110;        // crates in flight at once; units per second
  const MAX_PARKED = 4, MAX_BINNED = 5;      // crates drawn at the gate; confiscated crates kept in the derez field

  let on = false, state = null, primed = false;
  let svg = null, layout = "", stations = new Map(), parts = {};
  const seen = new Set(), branchSeen = new Map();
  const laneSeen = new Map();   // lane name -> { state, task, pair } as of the previous poll
  const parked = new Set();     // lanes with a branch crate on the console
  let crates = 0, denied = 0;   // crates in flight; confiscations seen since the page loaded (the panel's counter)
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

  // ---------- roles: a circuit colour each (the built-in roles fixed, others hashed)
  const HUE = { developer: 0, reviewer: 1, researcher: 2 };
  function hue(role) {
    if (role in HUE) return HUE[role];
    let h = 0;
    for (const ch of role) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return 3 + (h % 4);
  }
  // Developers dock on the top side, reviewers on the bottom; any other role docks on the right side.
  const bayOf = lane => (lane.role === "developer" ? "dev" : lane.role === "reviewer" ? "rev" : "oth");

  // ---------- the scene
  // The panel's width: what the bay's shape asks for, never narrower than the stations need.
  function plan(nd, nr, no) {
    const box = host.getBoundingClientRect(), aspect = box.width > 0 && box.height > 0 ? box.width / box.height : 1.6;
    const right = no ? 8 + no * OTHER_W + 10 : 40;                     // from the track's right side to the panel's edge
    const devSpan = LX + RAD, revSpan = QUEUE_X + MAX_PARKED * 9 + 6;   // where each row's stations may start
    const need = Math.max(devSpan + nd * MIN_CELL, revSpan + nr * MIN_CELL, LX + 2 * RAD + 60) + RAD + right;
    const w = Math.max(MIN_W, need, Math.round(H * Math.min(aspect, MAX_ASPECT) / 8) * 8);
    return { nd, nr, no, w, right, devSpan, revSpan };
  }

  // Points along the track's corner arcs, so a crate turns a corner in a few straight hops.
  const arc = (cx, cy, a0, a1) => [1, 2].map(k => { const a = (a0 + (a1 - a0) * k / 3) * Math.PI / 180; return [cx + RAD * Math.cos(a), cy + RAD * Math.sin(a)]; });
  const trackPath = () => `M${LX + RAD},${TY} H${RX - RAD} A${RAD},${RAD} 0 0 1 ${RX},${TY + RAD} V${BY - RAD} A${RAD},${RAD} 0 0 1 ${RX - RAD},${BY} H${LX + RAD} A${RAD},${RAD} 0 0 1 ${LX},${BY - RAD} V${TY + RAD} A${RAD},${RAD} 0 0 1 ${LX + RAD},${TY} Z`;

  function build(lanes, p) {
    W = p.w; RX = W - p.right;
    svg = S("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "xMidYMid meet", role: "group" }, "fl");
    stations = new Map();
    parts = {};
    const groups = { dev: [], rev: [], oth: [] };
    lanes.forEach((lane, i) => groups[bayOf(lane)].push([lane, i]));   // i is the lane's number: its key
    const glassW = W - 2 * GLASS_X, glassH = GLASS_B - GLASS_Y;

    // Paint and glass: gradients, the hazard stripe, the tube's phosphor, the glass clip.
    const defs = S("defs");
    const grad = (id, stops, attrs) => {
      const g = S("linearGradient", Object.assign({ id }, attrs));
      stops.forEach(([o, cls]) => g.append(S("stop", { offset: o }, cls)));
      return g;
    };
    defs.append(grad("fl-plate", [[0, "plate-a"], [0.4, "plate-b"], [1, "plate-c"]], { x1: 0, y1: 0, x2: 0, y2: 1 }));
    defs.append(grad("fl-beam", [[0, "beam-a"], [1, "beam-b"]], { x1: 0, y1: 0, x2: 0, y2: 1 }));
    defs.append(grad("fl-lcd", [[0, "lcd-a"], [1, "lcd-b"]], { x1: 0, y1: 0, x2: 1, y2: 1 }));
    const tube = S("radialGradient", { id: "fl-tube", cx: 0.5, cy: 0.42, r: 0.8 });
    tube.append(S("stop", { offset: 0 }, "tube-a"), S("stop", { offset: 0.72 }, "tube-b"));
    defs.append(tube);
    const vig = S("radialGradient", { id: "fl-vig", cx: 0.5, cy: 0.5, r: 0.72 });
    vig.append(S("stop", { offset: 0.6 }, "vig-a"), S("stop", { offset: 1 }, "vig-b"));
    defs.append(vig);
    const hazard = S("pattern", { id: "fl-hazard", width: 8, height: 8, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
    hazard.append(R(0, 0, 4, 8, "hz-1"), R(4, 0, 4, 8, "hz-2"));
    defs.append(hazard);
    const brush = S("pattern", { id: "fl-brush", width: 3, height: 1, patternUnits: "userSpaceOnUse" });
    brush.append(R(0, 0, 1, 1, "brush"));
    defs.append(brush);
    const scan = S("pattern", { id: "fl-scan", width: 1, height: 3, patternUnits: "userSpaceOnUse" });
    scan.append(R(0, 0, 1, 1, "scan-l"));
    defs.append(scan);
    const grille = S("pattern", { id: "fl-grille", width: 3, height: 1, patternUnits: "userSpaceOnUse" });
    grille.append(R(0, 0, 1, 1, "scan-l"));
    defs.append(grille);
    const clipP = S("clipPath", { id: "fl-glass" });
    clipP.append(S("rect", { x: GLASS_X, y: GLASS_Y, width: glassW, height: glassH, rx: 6 }));
    defs.append(clipP);
    svg.append(defs);

    // The panel: brushed plate, four screws, silk title, the LCD readout, the hazard stripe.
    svg.append(S("rect", { width: W, height: H, fill: "url(#fl-plate)" }), S("rect", { width: W, height: H, fill: "url(#fl-brush)" }));
    for (const [x, y] of [[9, 9], [W - 9, 9], [9, H - 9], [W - 9, H - 9]]) {
      svg.append(S("circle", { cx: x, cy: y, r: 3.5 }, "screw-rim"), S("circle", { cx: x, cy: y, r: 2.2 }, "screw"), L(x - 1.5, y, x + 1.5, y, "screw-slot"));
    }
    svg.append(T(16, 17, "LOIS WORKS", "silk big"));
    parts.signTop = T(16, 26, "", "silk dim");
    svg.append(parts.signTop);
    svg.append(S("rect", { x: W - 128, y: 7, width: 112, height: 19, rx: 1.5 }, "lcd-bezel"), S("rect", { x: W - 127, y: 8, width: 110, height: 17, rx: 1, fill: "url(#fl-lcd)" }, "lcd-glass"));
    parts.signBot = T(W - 72, 20.5, "", "lcd-t", "middle");
    svg.append(parts.signBot);
    svg.append(S("rect", { x: 0, y: H - 4, width: W, height: 4, fill: "url(#fl-hazard)" }));

    // The tube: bezel, glass, phosphor, a faint grid.
    svg.append(S("rect", { x: GLASS_X - 3, y: GLASS_Y - 3, width: glassW + 6, height: glassH + 6, rx: 8 }, "bezel"));
    svg.append(S("rect", { x: GLASS_X, y: GLASS_Y, width: glassW, height: glassH, rx: 6, fill: "url(#fl-tube)" }, "glass"));
    const grid = S("g", { "clip-path": "url(#fl-glass)" }, "grid");
    for (let x = GLASS_X + 7; x < W - GLASS_X; x += 20) grid.append(L(x, GLASS_Y, x, GLASS_B, "grid-l"));
    for (let y = GLASS_Y + 9; y < GLASS_B; y += 20) grid.append(L(GLASS_X, y, W - GLASS_X, y, "grid-l"));
    svg.append(grid);

    // The track: a light wall with a dark bed and chevrons that run while the factory works.
    const d = trackPath();
    const track = S("g", null, "track");
    track.append(S("path", { d }, "lightwall"), S("path", { d }, "bed"), S("path", { d }, "chevrons"));
    svg.append(track);
    svg.append(T(GLASS_X + 4, BY - 17, "DEVELOPERS ▲", "silk amber dim"), T(GLASS_X + 4, BY - 9, "REVIEWERS ▼", "silk green dim"));

    // Dispatch: the Coordinator's console on the left, the I/O beam above it, the User in the seat
    // while Terminal 0 is attached; the spur feeds the track.
    const con = S("g", null, "console");
    con.append(R(CON_X - 1, GLASS_Y, 2, CON_Y - 25 - GLASS_Y, "io-beam"));
    con.append(T(CON_X + 4, GLASS_Y + 11, "I/O", "silk dim"));
    con.append(...program(CON_X, CON_Y, "hue-user", "user"));
    con.append(R(CON_X - 20, CON_Y, 40, 12, "console-body"), R(CON_X - 18, CON_Y + 1.5, 36, 9, "console-scr"));
    con.append(T(CON_X, CON_Y + 7.6, "DISPATCH", "card-t console-t", "middle"));
    con.append(R(CHUTE[0], CHUTE[1] - 0.6, LX + RAD - CHUTE[0], 1.2, "spur"), R(LX + RAD - 0.6, CHUTE[1], 1.2, TY - HALF - CHUTE[1], "spur"), R(CHUTE[0] - 2, CHUTE[1] - 2, 4, 4, "lamp"));
    parts.note = T(CON_X, CON_Y + 21, "", "plate-t", "middle");
    con.append(parts.note, T(CON_X, CON_Y + 29, "COORDINATOR", "silk dim", "middle"));
    parts.console = con;
    svg.append(con);

    // Stations: developers in a row above the top side, reviewers below the bottom side, other
    // roles to the right of the right side. Each is centred in an equal cell along its span.
    parts.stations = [];
    const place = (list, x0, x1, cellH, originY, kind) => {
      if (!list.length) return;
      // Stations share the span equally; a cell is centred in its slot so the keycap stays by its figure.
      const slot = Math.max(MIN_CELL, (x1 - x0) / list.length), cw = Math.min(MAX_CELL, slot);
      list.forEach(([lane, i], k) => {
        const x = x0 + k * slot + (slot - cw) / 2, st = station(lane, i, cw, kind);
        st.g.setAttribute("transform", `translate(${x} ${originY})`);
        st.x = x + cw / 2;
        st.y = kind === "dev" ? TY - HALF : kind === "rev" ? BY + HALF : originY + 44;
        st.inTray = kind === "dev" ? [st.x, TY - HALF] : kind === "rev" ? [st.x, BY + HALF] : [x - 4, originY + 44];
        st.outTray = st.inTray;
        stations.set(lane.name, st);
        parts.stations.push(st.g);
      });
    };
    place(groups.dev, p.devSpan, RX - RAD, DEV_H, TY - HALF - DEV_H, "dev");
    place(groups.rev, p.revSpan, RX - RAD, REV_H, BY + HALF, "rev");
    place(groups.oth, RX + HALF + 4, RX + HALF + 4 + groups.oth.length * OTHER_W, DEV_H, MID - 26, "oth");

    // Light bridges (a reviewer to the developer it reviews) are redrawn by draw().
    parts.bridges = S("g", null, "bridges");
    svg.append(parts.bridges);

    // The gate on the bottom side: two posts, a beam with a lamp, a scanner line. Its lamp
    // and the panel's three LEDs below the glass follow the same classes (holding, ok, no).
    const arch = S("g", null, "arch");
    arch.append(R(ARCH_X - 9, BY - 13, 2.5, 26, "arch-post"), R(ARCH_X + 6.5, BY - 13, 2.5, 26, "arch-post"), R(ARCH_X - 10, BY - 15, 20, 2.5, "arch-top"));
    arch.append(R(ARCH_X - 6.5, BY - 12.5, 13, 25, "arch-beam"), R(ARCH_X - 2, BY - 17.5, 4, 2, "arch-lamp"));
    arch.append(L(ARCH_X - 6, BY - 10, ARCH_X + 6, BY - 10, "scanner"));
    arch.append(T(ARCH_X, BY - 20, "GATE", "silk amber", "middle"));
    const ledY = H - 20;
    for (const [k, [cls, text]] of [["led-allow", "ALLOW"], ["led-hold", "HOLD"], ["led-deny", "DENY"]].entries()) {
      const x = 24 + k * 26;
      arch.append(S("circle", { cx: x, cy: ledY, r: 3.6 }, "led-rim"), S("circle", { cx: x, cy: ledY, r: 2.6 }, `led ${cls}`), T(x, ledY + 10, text, "silk dim", "middle"));
    }
    arch.append(R(W / 2 - 28, ledY - 6, 56, 11, "lamp-held"));
    parts.heldText = T(W / 2, ledY + 1.8, "", "lamp-t", "middle");
    arch.append(parts.heldText);
    parts.arch = arch;
    svg.append(arch);
    // The denied counter: red digits in a dark window, counting confiscations since the page loaded.
    svg.append(T(W - 62, ledY + 2, "DENIED", "silk dim", "end"), S("rect", { x: W - 58, y: ledY - 8.5, width: 42, height: 17, rx: 1.5 }, "counter-bezel"), S("rect", { x: W - 57, y: ledY - 7.5, width: 40, height: 15, rx: 1 }, "counter-glass"));
    parts.denied = T(W - 37, ledY + 4, "00", "counter-t", "middle");
    svg.append(parts.denied);

    // The OUT portal: a wall of light where approved work drops off the track, and the
    // derez field under the gate where confiscated crates land.
    parts.door = R(OUT_X - 4, BY + HALF, 8, GLASS_B - BY - HALF, "door");
    parts.door.setAttribute("fill", "url(#fl-beam)");
    svg.append(parts.door, T(OUT_X - 6, BY + 26, "OUT", "silk amber", "end"));
    parts.queue = S("g", null, "queue");
    svg.append(parts.queue);
    const bin = S("g", null, "bin");
    bin.append(R(BIN_X - 2, PILE_Y + 1, 30, 1, "bin-line"), T(BIN_X + 13, PILE_Y + 8, "DEREZ", "silk red", "middle"));
    parts.bin = bin;
    svg.append(bin);
    parts.pile = S("g", null, "pile");
    svg.append(parts.pile);

    const stationLayer = S("g", null, "stations");
    stationLayer.append(...parts.stations);
    svg.append(stationLayer);
    // Crates in flight are clipped by the glass, so they leave the picture at its edge.
    parts.fx = S("g", { "clip-path": "url(#fl-glass)" }, "fx");
    svg.append(parts.fx);
    // Glass over everything inside the bezel: scanlines, grille, vignette, a rolling band.
    const over = S("g", { "clip-path": "url(#fl-glass)" }, "over");
    over.append(S("rect", { x: GLASS_X, y: GLASS_Y, width: glassW, height: glassH, fill: "url(#fl-scan)" }, "scan"),
      S("rect", { x: GLASS_X, y: GLASS_Y, width: glassW, height: glassH, fill: "url(#fl-grille)" }, "grille"),
      S("rect", { x: GLASS_X, y: GLASS_Y - glassH, width: glassW, height: glassH * 0.22 }, "band"),
      S("rect", { x: GLASS_X, y: GLASS_Y, width: glassW, height: glassH, fill: "url(#fl-vig)" }, "vig"));
    svg.append(over);
    host.replaceChildren(svg);
  }

  // A seated program, front view, its feet on y and centred on cx: a person in a jacket tinted
  // by the role's colour (rim-lit so it stands off the panel), a round head with hair, a neck, glowing
  // glasses and hands on the console, on a chair, with the identity disc behind the head. The hue
  // class sets --hue for the jacket, the lines and the disc. cls marks the figure (e.g. "user") for CSS.
  function program(cx, y, hueCls, cls) {
    const g = S("g", null, `fig ${hueCls}${cls ? " " + cls : ""}`);
    const P = (d, c) => S("path", { d }, c);
    const at = (dx, dy) => `${cx + dx} ${y + dy}`;
    g.append(S("circle", { cx, cy: y - 16, r: 7.5 }, "disc"));
    g.append(S("rect", { x: cx - 6.8, y: y - 13.5, width: 13.6, height: 11.5, rx: 2.4 }, "chair"),
      R(cx - 0.6, y - 3, 1.2, 2.6, "chair-post"), R(cx - 4.5, y - 1, 9, 1, "chair-base"));
    g.append(P(`M${at(-1.7, -11.8)} L${at(-4.3, -10.9)} Q${at(-5.8, -10.3)} ${at(-5.8, -8.5)} L${at(-5.6, -3)} Q${at(-5.6, -1.7)} ${at(-4.3, -1.7)} L${at(4.3, -1.7)} Q${at(5.6, -1.7)} ${at(5.6, -3)} L${at(5.8, -8.5)} Q${at(5.8, -10.3)} ${at(4.3, -10.9)} L${at(1.7, -11.8)} Z`, "suit"),
      P(`M${at(-1.9, -11.8)} L${at(0, -8.6)} L${at(1.9, -11.8)}`, "collar"),
      R(cx - 0.4, y - 8.6, 0.8, 6, "circuit"), R(cx - 4, y - 3.6, 8, 0.8, "circuit faint"));
    const armL = S("g", null, "arm arm-l"), armR = S("g", null, "arm arm-r");
    for (const [arm, k] of [[armL, -1], [armR, 1]]) {
      const d = `M${at(k * 4.6, -9.6)} Q${at(k * 7.4, -7.2)} ${at(k * 6.4, -4.4)} L${at(k * 3.4, -2.4)}`;
      arm.append(P(d, "sleeve-rim"), P(d, "sleeve"), S("circle", { cx: cx + k * 3.2, cy: y - 2, r: 1.25 }, "skin hand"));
    }
    const head = S("g", null, "hd");
    head.append(R(cx - 1.2, y - 13.2, 2.4, 2.6, "skin neck"), S("ellipse", { cx, cy: y - 16.2, rx: 3.5, ry: 3.9 }, "skin"),
      P(`M${at(-3.8, -16.6)} Q${at(-4, -20.9)} ${at(0, -20.9)} Q${at(4, -20.9)} ${at(3.8, -16.6)} Q${at(2, -18.7)} ${at(0, -18.5)} Q${at(-2, -18.7)} ${at(-3.8, -16.6)} Z`, "hair"),
      S("rect", { x: cx - 3.7, y: y - 17.3, width: 7.4, height: 2.8, rx: 0.9 }, "shade-frame"),
      S("rect", { x: cx - 3.3, y: y - 17, width: 3, height: 2.1, rx: 0.6 }, "shade"), S("rect", { x: cx + 0.3, y: y - 17, width: 3, height: 2.1, rx: 0.6 }, "shade"),
      R(cx - 2.8, y - 16.7, 1, 0.5, "glint"), R(cx + 0.8, y - 16.7, 1, 0.5, "glint"),
      P(`M${at(-1, -13.9)} Q${at(0, -13.3)} ${at(1, -13.9)}`, "mouth"));
    g.append(armL, armR, head);
    return [g];
  }

  function station(lane, i, cw, kind) {
    const cx = cw / 2, g = S("g", { tabindex: "0", role: "button" }, "st");
    const st = { g, lane: lane.name, cx, cw, sig: "" };
    st.title = S("title");
    const hueCls = `hue-${hue(lane.role)}`;
    g.classList.add(hueCls);
    const below = kind === "rev";                       // the figure sits below its console, under the track
    const conY = below ? 0 : DEV_H - 14;                // the console's top, in the cell
    const feet = below ? conY + 12 + 27 : conY;         // the figure's feet line
    const cellH = below ? REV_H : DEV_H;
    g.append(st.title, R(1, 1, cw - 2, cellH - 2, "hit"));
    g.append(...program(cx, feet, hueCls));
    // The console: a slab with a lit edge where it meets the track, and the job on its glass.
    g.append(R(cx - 20, conY, 40, 12, "console-body"), R(cx - 18, conY + 1.5, 36, 9, "console-scr"));
    if (kind === "oth") g.append(R(-12, conY + 5.4, cx - 20 + 12, 1.2, "lip"));
    else g.append(R(cx - 20, below ? conY - 2 : conY + 12, 40, 2, "lip"));
    // The placard: the job in a few words on the console's glass, and under it, on a
    // reviewer's, the developer it is reviewing. Hidden until draw() has text for it.
    st.card = S("g", { "clip-path": `url(#fl-card-${i})` }, "card off");
    const cardClip = S("clipPath", { id: `fl-card-${i}` });
    cardClip.append(R(cx - 18, conY + 1.5, 36, 9));
    st.card.append(cardClip);
    st.cardBg = R(cx - 18, conY + 1.5, 36, 9, "card-bg");
    st.cardJob = T(cx, conY + 6, "", "card-t", "middle");
    st.cardTag = T(cx, conY + 10.6, "", "card-tag", "middle");
    st.card.append(st.cardBg, st.cardJob, st.cardTag);
    g.append(st.card);
    // A finished branch is parked on the console as a data crate.
    st.park = crateShape(cx + 12, below ? conY + 13 : conY - 7, "crate branch park off");
    g.append(st.park);
    // Props: CSS shows the one that matches the lane's state. Held: a Recognizer hovers over
    // the figure. Done: a lit check. Revise: a FIX tag. Failed: derez fragments. Idle: z's.
    const recog = S("g", null, "p p-held");
    const ry = below ? feet - 22 : 1, rx = below ? cx + 10 : cx - 12, rw = below ? 20 : 24, rh = below ? 7 : 8;
    recog.append(S("polygon", { points: `${rx},${ry} ${rx + rw},${ry} ${rx + rw},${ry + rh} ${rx + rw - 4},${ry + rh} ${rx + rw - 4},${ry + rh / 2} ${rx + 4},${ry + rh / 2} ${rx + 4},${ry + rh} ${rx},${ry + rh}` }, "recog"));
    recog.append(T(rx + rw + 2, ry + rh - 1, "HOLD", "silk red"));
    const done = S("g", null, "p p-done");
    done.append(R(cx + 10, conY + (below ? 14 : -8), 10, 7, "flag"), T(cx + 15, conY + (below ? 19.6 : -2.4), "✓", "flag-t", "middle"));
    const fix = S("g", null, "p p-revise");
    fix.append(R(cx + 9, conY + (below ? 14 : -8), 12, 7, "fix"), T(cx + 15, conY + (below ? 19.4 : -2.6), "FIX", "fix-t", "middle"));
    const frag = S("g", null, "p p-fail");
    for (const [dx, dy, w] of [[-9, -24, 2], [8, -22, 1.5], [-11, -19, 1.5], [10, -17, 2], [-7, -27, 1], [6, -27, 1.5]]) frag.append(R(cx + dx, feet + dy, w, w, "frag"));
    const zz = S("g", null, "p p-idle");
    zz.append(T(cx + 7, feet - 20, "z", "zz d0"), T(cx + 10.5, feet - 24.5, "z", "zz d1"), T(cx + 14, feet - 29, "Z", "zz d2"));
    g.append(recog, done, fix, frag, zz);
    if (i < 9) g.append(R(2, 2, 9, 9, "kc"), T(6.5, 9, i + 1, "kc-t", "middle"));
    // The lane's name sits inside the loop, by its side of the track (other roles: under the cell).
    const nameY = kind === "dev" ? cellH + HALF + 12 : kind === "rev" ? -HALF - 8 : cellH + 8;
    st.name = T(cx, nameY, clip(lane.name, Math.floor(cw / 4)), "name", "middle");
    g.append(st.name);
    const open = () => document.dispatchEvent(new CustomEvent("factory:open-lane", { detail: lane.name }));
    g.addEventListener("click", open);
    g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    return st;
  }

  // ---------- each poll
  const LABEL = { running: "working", held: "held at the gate", revise: "asked to revise", done: "done", approved: "approved",
    idle: "idle", cancelled: "cancelled", failed: "failed", interrupted: "interrupted" };
  const PROP = { running: "running", held: "held", revise: "revise", done: "done", approved: "done", idle: "idle", cancelled: "fail", failed: "fail", interrupted: "fail" };

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
  // Text that is wider than the glass slides sideways (to its end and back, pausing at each),
  // clipped to the glass; text that fits sits still in the middle. Reduced motion keeps the ellipsis.
  const SLIDE = 9, PAUSE = 0.18;   // units per second; the share of each pass spent waiting at an end
  function show(st, node, s, maxW, slide) {
    for (const a of node.getAnimations ? node.getAnimations() : []) a.cancel();
    node.setAttribute("text-anchor", "middle");
    node.setAttribute("x", st.cx);
    node.textContent = s;
    if (!s || width(node, s) <= maxW) return s;
    if (!slide || !node.animate) return fit(node, s, maxW);
    const over = width(node, s) - maxW;
    node.setAttribute("text-anchor", "start");
    node.setAttribute("x", st.cx - maxW / 2);
    const to = `translate(${-over}px, 0px)`, rest = { transform: "translate(0px, 0px)" };
    node.animate([{ ...rest, offset: 0 }, { ...rest, offset: PAUSE }, { transform: to, offset: 1 - PAUSE }, { transform: to, offset: 1 }],
      { duration: Math.max(1500, (over / SLIDE) * 1000 / (1 - 2 * PAUSE)), direction: "alternate", iterations: Infinity, easing: "linear" });
    return s;
  }
  function placard(st, lane) {
    const desk = typeof lane.desk === "string" ? lane.desk : "";
    const reviews = lane.role === "reviewer" && typeof lane.reviews === "string" ? lane.reviews : "";
    const slide = !motionOff.matches;
    const sig = JSON.stringify([desk, reviews, lane.task, lane.state, slide]);
    if (st.sig === sig) return;
    st.sig = sig;
    const maxW = 34;
    st.card.setAttribute("class", "card");   // measured on screen: a hidden card has no text length
    const job = show(st, st.cardJob, desk, maxW, slide), tag = show(st, st.cardTag, reviews ? `▸ ${devTag(reviews)}` : "", maxW, slide);
    // One line sits in the middle of the glass; two share it.
    const top = Number(st.cardBg.getAttribute("y"));
    st.cardJob.setAttribute("y", tag ? top + 4.6 : top + 6.6);
    st.cardTag.setAttribute("y", job ? top + 9 : top + 6.6);
    st.card.setAttribute("class", `card${job || tag ? "" : " off"}${tag ? " rev" : ""}`);
    const first = String(lane.task || "").split("\n")[0];
    st.title.textContent = job || tag ? clip(first, 300) : "";
  }
  // Placards are measured in the pixel font: measure again once it has loaded.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { stations.forEach(st => { st.sig = ""; }); if (on && state) draw(); });

  // A branch crate waits on the console of a lane that has handed back a branch.
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

    // A developer a running reviewer is reviewing gets a light bridge from that reviewer.
    const pairs = lanes.filter(l => l && l.role === "reviewer" && (l.state === "running" || l.state === "held")
      && typeof l.reviews === "string" && l.reviews).map(l => [l.name, l.reviews]);
    const watched = new Set(pairs.map(([, dev]) => dev));
    const counts = {};
    lanes.forEach(lane => {
      const st = stations.get(lane.name);
      if (!st) return;
      const s = PROP[lane.state] || "idle";
      counts[s] = (counts[s] || 0) + 1;
      const reviewing = watched.has(lane.name);
      const cls = `st hue-${hue(lane.role)} s-${s}${reviewing ? " reviewed" : ""}`;
      if (st.g.getAttribute("class") !== cls) st.g.setAttribute("class", cls);
      placard(st, lane);
      showPark(st);
      const desk = typeof lane.desk === "string" && lane.desk ? `, ${lane.desk}` : "";
      const reviews = lane.role === "reviewer" && typeof lane.reviews === "string" && lane.reviews ? `, reviewing ${lane.reviews}` : "";
      const branch = parked.has(lane.name) ? ", branch ready" : "";
      st.g.setAttribute("aria-label", `Open lane ${lane.name}, ${LABEL[lane.state] || lane.state}${desk}${reviews}${branch}`);
    });
    const bridgeKey = JSON.stringify(pairs);
    if (parts.bridged !== bridgeKey) {
      parts.bridged = bridgeKey;
      parts.bridges.replaceChildren(...pairs.flatMap(([rev, dev]) => {
        const a = stations.get(rev), b = stations.get(dev);
        if (!a || !b || a === b) return [];
        const pts = `${a.x},${BY - 16} ${a.x},${MID} ${b.x},${MID} ${b.x},${TY + 16}`;
        return [S("polyline", { points: pts }, "bridge"), S("polyline", { points: pts }, "bridge-run")];
      }));
    }

    const at = new Date((Number(state.now) || Date.now() / 1000) * 1000), hr = at.getHours();
    const shift = hr >= 7 && hr < 17 ? "day" : (hr >= 17 && hr < 20) || (hr >= 5 && hr < 7) ? "dusk" : "night";
    svg.setAttribute("class", `fl ${shift}${state.totals && state.totals.running ? " working" : ""}${motionOff.matches ? " still" : ""}`);
    const t = state.totals || {};
    parts.signTop.textContent = `${shift === "night" ? "NIGHT SHIFT" : shift === "dusk" ? "SWING SHIFT" : "DAY SHIFT"} · THE FLOOR · FC-1984`;
    parts.signBot.textContent = `JOBS ${t.jobs ?? 0} · RUN ${t.running ?? 0} · $${Number(t.cost || 0).toFixed(2)}`;

    const attached = !!(state.term && state.term.running);
    parts.console.setAttribute("class", `console${state.bridge ? " lit" : ""}${attached ? " in" : ""}`);
    parts.note.textContent = !state.bridge ? "BRIDGE DOWN" : attached ? "ON DUTY" : "BACK SOON";

    // Crates waiting at the gate: one per undecided held write (at most MAX_PARKED drawn),
    // each named by its write: tool · role · repo · branch.
    const gate = Array.isArray(state.gate) ? state.gate : [];
    const held = gate.filter(c => c && c.decided !== "approve" && c.decided !== "decline");
    const waiting = held.length;
    parts.arch.setAttribute("class", `arch${waiting ? " holding" : ""}`);
    parts.heldText.textContent = waiting ? `HELD ${waiting}` : "GATE CLEAR";
    const names = held.slice(0, MAX_PARKED).map(c => [c.tool, c.role, c.repo, c.branch].map(v => String(v || "")).filter(Boolean).join(" · ") || "held write");
    const parkedNames = JSON.stringify(names);
    if (parts.parked !== parkedNames) {
      parts.parked = parkedNames;
      parts.queue.replaceChildren(...names.map((name, j) => crateShape(QUEUE_X + j * 9, BY - 6, "crate held", name)));
    }
    drawPile();
    parts.denied.textContent = String(Math.min(denied, 99)).padStart(2, "0");

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
    g.append(R(x, y, 7, 6, "crate-box"), L(x, y, x + 7, y + 6, "crate-x"), L(x + 7, y, x, y + 6, "crate-x"));
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
  // The derez field: the last MAX_BINNED confiscated crates, two rows under the gate.
  function drawPile() {
    if (!parts.pile) return;
    const key = JSON.stringify(binned);
    if (parts.piled === key) return;
    parts.piled = key;
    parts.pile.replaceChildren(...binned.map((label, i) => {
      const row = i < 3 ? 0 : 1, col = row ? i - 3 : i;
      return crateShape(BIN_X - 0.5 + (row ? 3 : 0) + col * 6.5, PILE_Y - 6 - row * 5, "crate bad", label);
    }));
  }
  function binCrate(label) {
    binned.push(label);
    if (binned.length > MAX_BINNED) binned.shift();
    denied++;
    if (parts.denied) parts.denied.textContent = String(Math.min(denied, 99)).padStart(2, "0");
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
    const x = BIN_X + 14, y = PILE_Y - 14;
    const g = S("g", { transform: `translate(${x} ${y}) rotate(-8)` }, "stamp");
    g.append(R(-30, -7, 60, 10, "stamp-box"), T(0, 1, "CONFISCATED", "stamp-t", "middle"));
    parts.fx.append(g);
    g.animate([{ opacity: 0, transform: `translate(${x}px, ${y}px) rotate(-8deg) scale(1.6)` },
               { opacity: 1, transform: `translate(${x}px, ${y}px) rotate(-8deg) scale(1)`, offset: 0.12 },
               { opacity: 1, transform: `translate(${x}px, ${y}px) rotate(-8deg) scale(1)`, offset: 0.8 },
               { opacity: 0, transform: `translate(${x}px, ${y}px) rotate(-8deg) scale(1)` }],
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
    return st ? [st.x, st.y] : [CON_X, CON_Y + 12];
  }
  // Crates ride the bottom side of the track to the gate: this is its centre line. A broker
  // call's crate cuts straight across the loop to it, light-cycle fashion, then rides to the gate.
  const OUT_Y = () => BY;
  function toArch(role, then) {
    const [x, y] = origin(role), oy = OUT_Y();
    return travel("crate", [[x, y], [x, oy], [ARCH_X - 4, oy]], then);
  }
  // Past the gate the track runs on to the OUT portal, where the crate drops off the picture.
  function pass(fromX) {
    const oy = OUT_Y();
    flash(parts.arch, "ok", 500);
    return travel("crate ok", [[fromX, oy], [OUT_X, oy], [OUT_X, GLASS_B + 12]]);
  }
  function confiscate(fromX, label) {
    const oy = OUT_Y();
    flash(parts.arch, "no", 700);
    const landed = () => { stamp(); binCrate(label); };
    const moved = travel("crate bad", [[fromX, oy], [BIN_X + 4, oy + 14], [BIN_X + 9, PILE_Y]], landed);
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
      case "STAGED": case "HELD": toArch(role); return false;    // it waits in the queue (drawn from the gate cards)
      case "OK": case "WRITTEN": return pass(ARCH_X + 4);
      case "NO": case "FAILED": return confiscate(ARCH_X + 4, line(ev));
      case "JOB": {
        const st = stations.get(words[0]);
        if (st && words[1] === "done") pop(st, "+1");
        return false;
      }
      default: return false;
    }
  }

  // ---------- work flowing from the Coordinator to the portal
  // Park a branch crate on a lane's console (kept across polls, drawn by draw()); true when it is on screen.
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
  // Trips along the track. Each starts and ends on a console, with the crate's feet on the
  // track's centre line in between; corners are turned in a few hops (arc()).
  const dispatchPath = st => [CHUTE, [LX + RAD, CHUTE[1]], [LX + RAD, TY], [st.x, TY], st.inTray];
  const handoffPath = (dev, rev) => [dev.outTray, [dev.x, TY], [RX - RAD, TY], ...arc(RX - RAD, TY + RAD, 270, 360), [RX, TY + RAD], [RX, BY - RAD],
    ...arc(RX - RAD, BY - RAD, 0, 90), [RX - RAD, BY], [rev.x, BY], rev.inTray];
  const dropPath = rev => [rev.inTray, rev.outTray, [rev.x, BY], [OUT_X, BY], [OUT_X, GLASS_B + 12]];

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
