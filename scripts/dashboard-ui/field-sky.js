// The Thoughtfield's sky (doomtickle/passdesk-ops#207). Each of today's jobs is one packet: a hexagonal crystal capsule holding a
// small spiral of its own stars, and the one thing you hover and click for that job. Packets sit on rings laid out on screen around
// the witness's own outline (its meshes projected from the camera, above the horizon), so none is ever behind the head or neck at any
// width. The newest ring is innermost, and along a ring time runs from the left horizon, up round the head, down to the right one.
// A packet's size is its spend; its stars are its real actions, oldest at the core, in the action kinds' colours, and the band round
// its waist is its kind mix. Jobs on one branch are one order: one hull colour, side by side, joined by a thread. A running job has a
// spark at its core and turns; a held write is a gold packet beside its order, which stands still until you decide; a failed job is
// dark glass round a void. The packet whose monitor is open wears a lock ring, and keeps a faint one once it closes. The same stars
// pour out into other shapes to answer a question, drawn to scale.
// Every string here came from /api/state or /api/field and is drawn with textContent. Nothing in this file acts.
"use strict";
window.makeSky = function (scene, camera, C, U, W) {
  const MAXSTARS = 62000, MAXORD = 24, MAXJOB = 64, TARGET = 30000, TAU = 6.2832, TILT = 1.1;
  const hhmm = m0 => { const m = ((m0 % 1440) + 1440) % 1440; return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(Math.floor(m % 60)).padStart(2, "0"); };
  const hsl = (h, s, l) => new THREE.Color().setHSL(((h % 360) + 360) % 360 / 360, s, l);
  const gauss = () => { let a = 0, b = 0; while (!a) a = Math.random(); while (!b) b = Math.random(); return Math.sqrt(-2 * Math.log(a)) * Math.cos(6.2832 * b); };
  const nowMin = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60 + d.getMilliseconds() / 60000; };
  const root = new THREE.Group(); scene.add(root);
  function canvasTex(draw, n) { const c = document.createElement("canvas"); c.width = c.height = n; draw(c.getContext("2d"), n); return new THREE.CanvasTexture(c); }
  const FLARE = canvasTex((x, n) => { const m = n / 2; const g = x.createRadialGradient(m, m, 0, m, m, m); g.addColorStop(0, "rgba(255,255,255,1)"); g.addColorStop(.12, "rgba(255,255,255,.62)"); g.addColorStop(.34, "rgba(255,255,255,.12)"); g.addColorStop(1, "rgba(255,255,255,0)"); x.fillStyle = g; x.fillRect(0, 0, n, n); x.globalCompositeOperation = "lighter"; [0, Math.PI / 2].forEach(a => { x.save(); x.translate(m, m); x.rotate(a); const l = x.createLinearGradient(-m, 0, m, 0); l.addColorStop(0, "rgba(255,255,255,0)"); l.addColorStop(.5, "rgba(255,255,255,.95)"); l.addColorStop(1, "rgba(255,255,255,0)"); x.fillStyle = l; x.fillRect(-m, -1.1, n, 2.2); x.restore(); }); }, 256);
  const VOIDT = canvasTex((x, n) => { const m = n / 2, g = x.createRadialGradient(m, m, 0, m, m, m); g.addColorStop(0, "rgba(0,0,0,1)"); g.addColorStop(.44, "rgba(2,0,3,.98)"); g.addColorStop(.56, "rgba(255,70,80,.9)"); g.addColorStop(.66, "rgba(150,25,45,.42)"); g.addColorStop(1, "rgba(0,0,0,0)"); x.fillStyle = g; x.fillRect(0, 0, n, n); }, 256);
  // the lock ring: four arcs with a tick each, round the packet you're on or the one whose monitor is open
  const LOCK = canvasTex((x, n) => { const m = n / 2; x.strokeStyle = "#fff"; x.lineCap = "round"; x.lineWidth = n * .03; for (let k = 0; k < 4; k++) { const a = k * Math.PI / 2 + .32; x.beginPath(); x.arc(m, m, m * .8, a, a + .93); x.stroke(); const t = a + .465; x.beginPath(); x.moveTo(m + Math.cos(t) * m * .86, m + Math.sin(t) * m * .86); x.lineTo(m + Math.cos(t) * m * .97, m + Math.sin(t) * m * .97); x.stroke(); } }, 256);
  const sprite = (tex, color, size, op, additive = true) => { const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, color, transparent: true, opacity: op, depthWrite: false, blending: additive ? THREE.AdditiveBlending : THREE.NormalBlending })); s.scale.set(size, size, 1); return s; };

  const KINDS = [["Reading files", new THREE.Color(.4, .68, 1)], ["Editing", new THREE.Color(.95, .98, 1)], ["Running commands", new THREE.Color(.42, 1, .72)], ["Web and git requests", new THREE.Color(.78, .58, 1)], ["Reviewing", new THREE.Color(.38, .9, 1)], ["Blocked", new THREE.Color(1, .3, .36)]];
  const KCSS = KINDS.map(k => "#" + k[1].getHexString());
  const MIX = { developer: [.42, .3, .2, .08, 0], reviewer: [0, 0, 0, 0, 1] };
  const GOLD = new THREE.Color(1, .78, .3), RED = new THREE.Color(1, .3, .36), DARK = new THREE.Color(.02, 0, .02), REVIEW = new THREE.Color(.45, .95, 1), HOVER = new THREE.Color(.92, .97, 1), LOCKC = new THREE.Color(.45, .88, 1);
  let orders = [], jobs = [], O = new Map(), J = new Map(), RATE = 1600, nStars = 0, first = true, dayKey = null, gasAt = 0; const blockedSeen = new Set();
  const hashHue = s => { let h = 7; for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) % 360; return h; };
  const bad = j => /failed|cancelled|interrupted/.test(j.status);

  // ---- stars. aHome is a star's place inside its packet (a tilted disc, unit size); the shader puts it in the packet's world spot.
  const aHome = new Float32Array(MAXSTARS * 3), aMeta = new Float32Array(MAXSTARS * 4), aSeed = new Float32Array(MAXSTARS * 2), aA = new Float32Array(MAXSTARS * 3), aB = new Float32Array(MAXSTARS * 3), aJ = new Int32Array(MAXSTARS);   // aJ: which job, exactly (the shader's indexes wrap)
  for (let i = 0; i < MAXSTARS; i++) { aMeta[i * 4] = 1e6; const r = Math.random(); aSeed[i * 2] = Math.random(); aSeed[i * 2 + 1] = r > .992 ? 3.4 + Math.random() * 2.2 : r > .9 ? 1.75 + Math.random() * .9 : .85 + Math.random() * .75; }
  const VS = `attribute vec3 aHome, aA, aB; attribute vec4 aMeta; attribute vec2 aSeed;
    uniform float uT, uTime, uMorph, uAHome, uBHome, uPure, uViewAmt, uPx, uVA; uniform vec3 uOrdC[${MAXORD}]; uniform vec2 uOrdFx[${MAXORD}]; uniform vec4 uJob[${MAXJOB}], uJobP[${MAXJOB}]; uniform vec3 uKindC[6];
    varying vec3 vC; varying float vA;
    vec3 home(int ji){ vec4 P = uJobP[ji]; float a = uJob[ji].z, c = cos(a), s = sin(a); vec3 l = vec3(c * aHome.x - s * aHome.z, aHome.y, s * aHome.x + c * aHome.z);
      l = vec3(l.x, l.y * ${Math.cos(TILT).toFixed(5)} - l.z * ${Math.sin(TILT).toFixed(5)}, l.y * ${Math.sin(TILT).toFixed(5)} + l.z * ${Math.cos(TILT).toFixed(5)}); return P.xyz + l * P.w; }
    void main(){ float age = uT - aMeta.x; int ji = int(aMeta.w); if (age < 0. || uJobP[ji].w <= 0.) { gl_Position = vec4(2., 2., 2., 1.); gl_PointSize = 0.; vA = 0.; vC = vec3(0.); return; }
      float o = aMeta.z, k = aMeta.y; vec3 h = home(ji);
      #ifdef GAS
        vec4 mv = viewMatrix * vec4(h, 1.); gl_Position = projectionMatrix * mv; gl_PointSize = aSeed.y * uPx * 700. * uJobP[ji].w / -mv.z; vC = uOrdC[int(o)]; vA = .025 * (1. - uViewAmt) * min(1., age * .25) * uJob[ji].x;
      #else
        vec3 A = uAHome > .5 ? h : aA, B = uBHome > .5 ? h : aB; float d = .5, p = clamp(uMorph * (1. + d) - aSeed.x * d, 0., 1.); p = p * p * (3. - 2. * p); float fly = sin(p * 3.14159);
        vec3 q = mix(A, B, p); vec3 dd = B - A; q += vec3(-dd.y, dd.x, 0.) * fly * (.1 + .3 * (aSeed.x - .5)); q.y += fly * .05 * sin(aSeed.x * 40. + uTime);
        vec4 mv = viewMatrix * vec4(q, 1.); gl_Position = projectionMatrix * mv;
        float tw = .86 + .14 * sin(uTime * (1. + aSeed.x * 2.) + aSeed.x * 60.), flash = exp(-age * 1.2), rl = length(aHome);
        gl_PointSize = clamp(aSeed.y * uPx * mix(2.7 / -mv.z, 1.05, uViewAmt) * (1. + flash * 1.5 + fly * .35), 1., 16.);
        vec3 base = mix(uOrdC[int(o)], uKindC[int(k)], mix(.6, 1., uPure));
        float scan = uJob[ji].y * smoothstep(.06, 0., abs(fract(rl * 2.2 - uTime * .55) - .5));
        float thaw = uOrdFx[int(o)].y * smoothstep(.09, 0., abs(rl - (1. - uOrdFx[int(o)].y) * .8));
        vC = base + vec3(1.) * flash * .45 + vec3(.25, .85, 1.) * scan + vec3(1., .82, .4) * thaw * 2.;
        vA = uVA * (.5 + aSeed.x * .36) * tw * uJob[ji].x * mix(uJob[ji].w, 1., uViewAmt) * uOrdFx[int(o)].x * (1. + flash * 1.4 + scan + thaw * 3. + fly * .4);
      #endif
    }`;
  const FS = `varying vec3 vC; varying float vA; void main(){ float d = length(gl_PointCoord - .5) * 2.; if (d > 1.) discard;
    #ifdef GAS
      float a = pow(1. - d, 2.);
    #else
      float a = pow(1. - d, 1.5);
    #endif
    gl_FragColor = vec4(vC, a * vA); }`;
  const u = { uT: { value: nowMin() }, uTime: U.uTime, uMorph: { value: 1 }, uAHome: { value: 1 }, uBHome: { value: 1 }, uPure: { value: 0 }, uViewAmt: { value: 0 }, uVA: { value: 1 }, uPx: { value: 1 },
    uOrdC: { value: Array.from({ length: MAXORD }, () => new THREE.Color(.6, .7, 1)) }, uOrdFx: { value: Array.from({ length: MAXORD }, () => new THREE.Vector2(1, 0)) }, uJob: { value: Array.from({ length: MAXJOB }, () => new THREE.Vector4(1, 0, 0, 1)) }, uJobP: { value: Array.from({ length: MAXJOB }, () => new THREE.Vector4(0, 0, 0, 0)) }, uKindC: { value: KINDS.map(k => k[1]) } };
  const geo = new THREE.BufferGeometry(), attr = (n, a, s) => { const b = new THREE.BufferAttribute(a, s); geo.setAttribute(n, b); return b; };
  attr("position", new Float32Array(MAXSTARS * 3), 3); const bHome = attr("aHome", aHome, 3), bMeta = attr("aMeta", aMeta, 4); attr("aSeed", aSeed, 2); const bA = attr("aA", aA, 3), bB = attr("aB", aB, 3);
  const points = new THREE.Points(geo, new THREE.ShaderMaterial({ vertexShader: VS, fragmentShader: FS, uniforms: u, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending })); points.frustumCulled = false; points.renderOrder = 2; root.add(points);
  const gasN = 900, gHome = new Float32Array(gasN * 3), gMeta = new Float32Array(gasN * 4), gSeed = new Float32Array(gasN * 2); for (let g = 0; g < gasN; g++) gMeta[g * 4] = 1e6;
  const gGeo = new THREE.BufferGeometry(); gGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(gasN * 3), 3)); const gbHome = new THREE.BufferAttribute(gHome, 3), gbMeta = new THREE.BufferAttribute(gMeta, 4); gGeo.setAttribute("aHome", gbHome); gGeo.setAttribute("aMeta", gbMeta); gGeo.setAttribute("aSeed", new THREE.BufferAttribute(gSeed, 2));
  const gas = new THREE.Points(gGeo, new THREE.ShaderMaterial({ defines: { GAS: 1 }, vertexShader: VS, fragmentShader: FS, uniforms: u, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending })); gas.frustumCulled = false; gas.renderOrder = 1; root.add(gas);

  // ---- the packet: a hexagonal crystal capsule of unit size (apexes at ±.92), its hull, edges and a band round the waist
  const HULL = [[0, .92, 0], [0, -.92, 0]]; for (const y of [.3, -.3]) for (let k = 0; k < 6; k++) HULL.push([.74 * Math.cos(k * Math.PI / 3), y, .74 * Math.sin(k * Math.PI / 3)]);
  const triI = [], edgeI = []; for (let k = 0; k < 6; k++) { const a = 2 + k, b = 2 + (k + 1) % 6, c = 8 + k, d = 8 + (k + 1) % 6; triI.push(0, b, a, a, b, d, a, d, c, 1, c, d); edgeI.push(0, a, a, b, a, c, c, d, 1, c); }
  const polyGeo = idx => { const f = new Float32Array(idx.length * 3); idx.forEach((i, n) => f.set(HULL[i], n * 3)); const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.BufferAttribute(f, 3)); return g; };
  const faceGeo = polyGeo(triI), edgeGeo = polyGeo(edgeI), BANDN = 48;
  function bandGeo() { const pos = new Float32Array(BANDN * 18), at = q => { const s = Math.floor(q / 8) % 6, t = (q % 8) / 8, a0 = s * Math.PI / 3, a1 = a0 + Math.PI / 3; return [.765 * (Math.cos(a0) * (1 - t) + Math.cos(a1) * t), .765 * (Math.sin(a0) * (1 - t) + Math.sin(a1) * t)]; };
    for (let s = 0; s < BANDN; s++) { const [x0, z0] = at(s), [x1, z1] = at(s + 1); pos.set([x0, .06, z0, x0, -.06, z0, x1, .06, z1, x1, .06, z1, x0, -.06, z0, x1, -.06, z1], s * 18); }
    const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.BufferAttribute(pos, 3)); g.setAttribute("color", new THREE.BufferAttribute(new Float32Array(BANDN * 18), 3)); return g; }
  const basic = (o = {}) => Object.assign({ transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }, o);
  const PK = new Map();   // "j<id>" or "c<card id>" -> packet
  function makePacket(key, o, job, card) { const g = new THREE.Group(), hull = new THREE.Group(), faces = new THREE.Mesh(faceGeo, new THREE.MeshBasicMaterial(basic({ opacity: .08, side: THREE.DoubleSide }))), edges = new THREE.LineSegments(edgeGeo, new THREE.LineBasicMaterial(basic({ opacity: .85 }))), band = new THREE.Mesh(bandGeo(), new THREE.MeshBasicMaterial(basic({ vertexColors: true, opacity: .95, side: THREE.DoubleSide })));
    faces.renderOrder = 1; edges.renderOrder = 3; band.renderOrder = 3; hull.add(faces, edges, band); hull.rotation.x = .2;
    const core = sprite(FLARE, new THREE.Color(1, 1, 1), 1, 0), lock = sprite(LOCK, new THREE.Color(.6, .9, 1), 3.1, 0); core.renderOrder = 4; lock.renderOrder = 5; g.add(hull, core, lock); g.visible = false; root.add(g);
    const pk = { key, order: o, job, card, g, hull, faces, edges, band, core, lock, voidS: null, pos: new THREE.Vector3(), to: new THREE.Vector3(), w: 0, tw: 0, sx: null, sy: null, R: null, yaw0: Math.random() * TAU, placed: false, born: performance.now(), bandN: -1, spin: .3 * Math.random(), hot: 0, lockA: 0 };
    g.userData = job ? { kind: "job", job } : { kind: "gold", card }; PK.set(key, pk); seqKey = ""; return pk; }
  function dropPacket(pk) { root.remove(pk.g); [pk.faces, pk.edges, pk.band].forEach(m => m.material.dispose()); pk.band.geometry.dispose(); [pk.core, pk.lock, pk.voidS].forEach(s => s && s.material.dispose()); PK.delete(pk.key); seqKey = ""; }
  function paintBand(pk) { const j = pk.job, cols = pk.band.geometry.attributes.color, n = [0, 0, 0, 0, 0, 0]; if (j) { j.actions.forEach(a => { n[a[1]]++; }); n[5] = j.blocked || 0; }
    const tot = n.reduce((a, b) => a + b, 0), base = pk.card ? GOLD : pk.order.color; let s = 0, k = 0, acc = n[0];
    for (; s < BANDN; s++) { let c = base.clone().multiplyScalar(.35); if (tot) { const f = (s + .5) / BANDN * tot; while (k < 5 && f > acc) acc += n[++k]; c = KINDS[k][1]; } for (let v = 0; v < 6; v++) cols.setXYZ(s * 6 + v, c.r, c.g, c.b); }
    cols.needsUpdate = true; pk.bandN = j ? j.actions.length + (j.blocked || 0) * 1000 : 0; }

  // ---- orders, jobs and stars
  const oi = o => o.i % MAXORD, ji = j => j.i % MAXJOB;
  function orderFor(key, label) { let o = O.get(key); if (o) return o; o = { key, label, hue: hashHue(key), i: orders.length, rate: 1, jobs: [], frozen: false }; o.color = hsl(o.hue, .72, .62); o.spark = hsl(o.hue, .6, .82); orders.push(o); O.set(key, o); u.uOrdC.value[oi(o)].copy(hsl(o.hue, .72, .6)); return o; }
  function putStar(j, t, kind, f, hull) { if (nStars >= MAXSTARS) return -1; const i = nStars++;
    if (hull) { const a = Math.random() * TAU, b = Math.acos(2 * Math.random() - 1); aHome[i * 3] = .8 * Math.sin(b) * Math.cos(a); aHome[i * 3 + 1] = .8 * Math.cos(b); aHome[i * 3 + 2] = .8 * Math.sin(b) * Math.sin(a); }
    else { const stray = Math.random() < .1, r = Math.min(.68, Math.max(.05, .08 + .52 * Math.sqrt(f) + gauss() * (stray ? .05 : .016))), th = (i % 2) * Math.PI + 2.4 * Math.log(r / .08) + gauss() * (stray ? .5 : .14 + .015 / r); aHome[i * 3] = r * Math.cos(th); aHome[i * 3 + 1] = gauss() * (stray ? .06 : .022); aHome[i * 3 + 2] = r * Math.sin(th); }
    aMeta[i * 4] = t; aMeta[i * 4 + 1] = kind; aMeta[i * 4 + 2] = oi(j.order); aMeta[i * 4 + 3] = ji(j); aJ[i] = j.i; j.stars.push(i); return i; }
  const pickKind = role => { const mix = MIX[role] || MIX.developer; let r = Math.random(), k = 0; for (; k < mix.length; k++) { r -= mix[k]; if (r <= 0) break; } return Math.min(k, mix.length - 1); };
  const jobCost = j => j.cost != null ? j.cost : j.actions.length * .012;
  const targetOf = j => Math.max(j.actions.length ? 4 : 0, Math.round(jobCost(j) * RATE));
  function place(j, n, born) { // n more stars for this job. born: a fixed time for live stars, else each takes its real action's time
    const start = j.placed, total = Math.max(1, targetOf(j)), acts = j.actions; for (let k = 0; k < n; k++) { const f = (start + k) / total, a = acts.length ? acts[Math.min(acts.length - 1, Math.floor(f * acts.length))] : null, t = born != null ? born : a ? a[0] : j.start + f * Math.max(.1, (j.end || j.start + 1) - j.start), kind = a ? a[1] : pickKind(j.role); if (putStar(j, t, kind, Math.min(1, f)) < 0) break; j.placed++; } }
  let dirty0 = -1, dirty1 = -1; const markDirty = from => { dirty0 = dirty0 < 0 ? from : Math.min(dirty0, from); dirty1 = nStars; };   // call after placing
  function flushStars(from) { if (from == null) { if (dirty0 < 0 || dirty1 <= dirty0) { dirty0 = dirty1 = -1; return; } bHome.updateRange.offset = dirty0 * 3; bHome.updateRange.count = (dirty1 - dirty0) * 3; bMeta.updateRange.offset = dirty0 * 4; bMeta.updateRange.count = (dirty1 - dirty0) * 4; } else { bHome.updateRange.offset = bMeta.updateRange.offset = 0; bHome.updateRange.count = bMeta.updateRange.count = -1; } bHome.needsUpdate = true; bMeta.needsUpdate = true; dirty0 = dirty1 = -1; }
  function buildGas() { gasAt = nStars; if (!nStars) return; for (let g = 0; g < gasN; g++) { const i = Math.floor(Math.random() * nStars); gHome.set([aHome[i * 3] * .8 + gauss() * .04, aHome[i * 3 + 1], aHome[i * 3 + 2] * .8 + gauss() * .04], g * 3); gMeta.set([aMeta[i * 4], 0, aMeta[i * 4 + 2], aMeta[i * 4 + 3]], g * 4); gSeed.set([Math.random(), .5 + Math.random() * .9], g * 2); } gbHome.needsUpdate = true; gbMeta.needsUpdate = true; }
  // a request the gateway blocked: red sparks on the shell of the job that asked
  function placeBlocked(list) { for (const b of list) { const key = b.t + "|" + b.role; if (blockedSeen.has(key)) continue; blockedSeen.add(key); let j = null; for (const x of jobs) if (x.role === b.role && x.start <= b.t) j = x; j = j || jobs[jobs.length - 1]; if (!j) return; j.blocked = (j.blocked || 0) + 1; for (let k = 0; k < 7; k++) if (putStar(j, b.t, 5, 1, true) < 0) return; } }
  function layoutAll(field) { // the whole day from scratch: first load, a new day, or the star budget outgrown
    const hist = field.jobs; nStars = 0; blockedSeen.clear(); jobs.forEach(j => { j.blocked = 0; }); const total = hist.reduce((s, x) => s + (x.cost != null ? x.cost : (x.actions || []).length * .012), 0);
    RATE = Math.round(Math.max(150, Math.min(6000, TARGET / Math.max(total, .4))) / 50) * 50;
    jobs.slice().sort((a, b) => a.start - b.start).forEach(j => { j.placed = 0; j.stars = []; place(j, targetOf(j)); });
    placeBlocked(field.blocked || []); for (let i = nStars; i < MAXSTARS; i++) aMeta[i * 4] = 1e6; flushStars(0); buildGas(); }

  // ---- live things: running jobs (a spark at the core), held writes (gold packets), failed jobs (a void)
  const SIZE = { haiku: .2, sonnet: .28, opus: .38, fable: .38 }, sizeOf = m => { const k = String(m || "").split("-")[0]; return SIZE[k] || .28; };
  const golds = new Map(), flashes = [], ships = [], tmp = new THREE.Vector3(), tmp2 = new THREE.Vector3();
  function flashAt(pos, color, size = .22, dur = 900) { const f = sprite(FLARE, color, size, .95); f.position.copy(pos); f.renderOrder = 6; root.add(f); flashes.push({ s: f, t0: performance.now(), dur, size }); }

  function ingest(field, state) { // one poll of /api/field, with the /api/state it goes with
    const list = (field && field.jobs) || []; if (dayKey !== null && field.date !== dayKey) reset(); dayKey = field.date;
    list.forEach(f => { let j = J.get(f.id); if (!j) { const o = orderFor(f.order, f.label); j = { id: f.id, i: jobs.length, order: o, stars: [], placed: 0, blocked: 0 }; jobs.push(j); J.set(f.id, j); o.jobs.push(j); makePacket("j" + j.id, o, j, null); }
      const was = j.status; Object.assign(j, { lane: f.lane, role: f.role, label: f.label, task: f.task, model: f.model, status: f.status, verdict: f.verdict, start: f.start, end: f.end, cost: f.cost, actions: f.actions || [] });
      const pk = PK.get("j" + j.id); if (was === "running" && j.status !== "running" && pk && pk.placed && !first) flashAt(pk.pos, bad(j) ? RED.clone() : j.verdict === "revise" ? new THREE.Color(1, .65, .2) : new THREE.Color(.3, 1, .6), pk.w * 3); });
    if (first) { first = false; layoutAll(field); } else {
      const outstanding = jobs.reduce((s, j) => s + Math.max(0, targetOf(j) - j.placed), 0); if (RATE > 150 && nStars + outstanding > MAXSTARS - 1500) { layoutAll(field); }   // at the lowest rate there's nothing left to shrink: new stars just stop
      else { const from = nStars; placeBlocked(field.blocked || []); if (nStars > from) markDirty(from); } }
    jobs.forEach(j => { j.scanOn = false; }); jobs.forEach(j => { if (j.status === "running" && j.role === "reviewer") j.order.jobs.forEach(x => { if (x.role !== "reviewer") x.scanOn = true; }); });
    syncCards((state && state.gate) || []); relayout(); }
  function syncCards(cards) { const live = new Set(); orders.forEach(o => { o.frozen = false; });
    cards.forEach(c => { if (!c || typeof c.id !== "string") return; live.add(c.id); let g = golds.get(c.id); if (!g) { const topic = String(c.branch || "").replace(/^(refs\/heads\/)?factory\//, ""); let o = O.get(topic); if (!o) { const lane = (c.lanes || [])[0]; const j = jobs.slice().reverse().find(x => (lane && x.lane === lane) || x.role === c.role); o = j ? j.order : orders[orders.length - 1]; } if (!o) { o = orderFor("(held writes)", "Held writes"); } g = { card: c, order: o, pk: makePacket("c" + c.id, o, null, c) }; golds.set(c.id, g); } g.card = c; g.pk.card = c; g.pk.g.userData.card = c; g.order.frozen = !c.decided; });
    golds.forEach((g, id) => { if (live.has(id)) return; const yes = g.card.decided === "approve"; flashAt(g.pk.pos, yes ? new THREE.Color(1, .82, .4) : new THREE.Color(1, .3, .3), .5, 1400); if (yes) u.uOrdFx.value[oi(g.order)].y = 1; dropPacket(g.pk); golds.delete(id); W.onCardGone && W.onCardGone(g.card, yes); }); }
  function reset() { PK.forEach(dropPacket); golds.clear(); orders = []; jobs = []; O = new Map(); J = new Map(); nStars = 0; blockedSeen.clear(); for (let i = 0; i < MAXSTARS; i++) aMeta[i * 4] = 1e6; flushStars(0); first = true; selKey = ""; if (view !== "home") { view = "home"; u.uAHome.value = 1; u.uBHome.value = 1; vTagsEl.textContent = ""; } }

  // ---- where the packets go: rings round the witness's outline on screen, never through it
  const vp = new THREE.Vector3(), angOf = (dx, dy) => ((Math.atan2(-dx, dy) % TAU) + TAU) % TAU;   // 0 straight down, a quarter turn left, half up, three quarters right
  let sil = null, rings = null, seq = [], seqKey = "", fitKey = "";
  function outline() { // the model's meshes projected from the camera; only what's above the horizon counts (the world hides the rest)
    const g = W.geo(); if (!g.R) return null; const pts = []; scene.children.forEach(ch => { if (ch === root) return; ch.traverse(o => { const p = o.isMesh && o.geometry && o.geometry.attributes.position; if (!p) return; o.updateMatrixWorld(true); const step = Math.max(1, Math.floor(p.count / 4000)); for (let i = 0; i < p.count; i += step) { vp.fromBufferAttribute(p, i).applyMatrix4(o.matrixWorld).project(camera); const x = (vp.x * .5 + .5) * g.w, y = (-vp.y * .5 + .5) * g.h; if (y < W.rimAt(x)) pts.push(x, y); } }); });
    if (!pts.length) return null; let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9; for (let i = 0; i < pts.length; i += 2) { x0 = Math.min(x0, pts[i]); x1 = Math.max(x1, pts[i]); y0 = Math.min(y0, pts[i + 1]); y1 = Math.max(y1, pts[i + 1]); }
    const s = { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, NB: 240, box: [x0, y0, x1, y1] }; s.rho = new Float32Array(s.NB); for (let i = 0; i < pts.length; i += 2) { const dx = pts[i] - s.cx, dy = pts[i + 1] - s.cy, b = Math.min(s.NB - 1, Math.floor(angOf(dx, dy) / TAU * s.NB)); s.rho[b] = Math.max(s.rho[b], Math.hypot(dx, dy)); } return s; }
  const binAt = (s, b) => s.rho[((b % s.NB) + s.NB) % s.NB];
  function reach(s, phi, pad) { const b0 = Math.floor(phi / TAU * s.NB), win = Math.ceil(Math.atan(pad / Math.max(30, binAt(s, b0))) / TAU * s.NB) + 1; let m = 0; for (let k = -win; k <= win; k++) m = Math.max(m, binAt(s, b0 + k)); return m + pad; }
  function inOutline(x, y, pad = 0) { if (!sil) return false; const dx = x - sil.cx, dy = y - sil.cy; return Math.hypot(dx, dy) < binAt(sil, Math.floor(angOf(dx, dy) / TAU * sil.NB)) + pad; }
  function ringSlots(R, topY) { const g = W.geo(), gap = Math.max(4, R * .45), step = 2 * R + gap, margin = Math.max(12, g.h * .018), out = [], all = [];
    for (let k = 0; k < 9; k++) { const list = []; let since = step, px = null, py = 0;
      for (let i = 0; i <= 1440; i++) { const phi = i / 1440 * TAU, r = reach(sil, phi, R + margin) + k * step, x = sil.cx - Math.sin(phi) * r, y = sil.cy + Math.cos(phi) * r; if (px != null) since += Math.hypot(x - px, y - py); px = x; py = y;
        if (since < step || x < R + 8 || x > g.w - R - 8 || y < topY + R || y > W.rimAt(x) - R - 10) continue; if (all.some(p => (p[0] - x) ** 2 + (p[1] - y) ** 2 < step * step)) continue; list.push([x, y]); all.push([x, y]); since = 0; }
      if (!list.length) break; out.push(list); }
    return out; }
  // the biggest packets that fit: one ring if it can hold them all (with a space between orders, else without) at no less than .7 of
  // the largest size, else as many rings as it takes
  function fit(n, gaps) { const g = W.geo(), top = document.querySelector(".top"), tb = top && top.getBoundingClientRect(), topY = Math.max(48, tb && tb.height ? tb.bottom + 4 : 48), R0 = Math.min(42, Math.max(16, g.h * .046)); let best = null;
    for (let R = R0; R >= 4.5; R *= .92) { const t = ringSlots(R, topY), cap = t.reduce((a, l) => a + l.length, 0), one = t.length ? t[0].length : 0, r = { R, rings: t, cap };
      if (one >= n + gaps) return Object.assign(r, { gaps: true }); if (one >= n) return Object.assign(r, { gaps: false }); if (R < R0 * .7 && cap >= n) return Object.assign(r, { gaps: cap >= n + gaps });
      if (!best || cap > best.cap) best = r; }
    return Object.assign(best, { gaps: false }); }
  function relayout(force) { // the sequence: orders by their first job, each order's jobs by start and then its held writes
    if (force) { sil = null; fitKey = ""; } if (!sil) sil = outline(); if (!sil) return;
    const list = [], byOrder = new Map(); PK.forEach(pk => { if (!byOrder.has(pk.order)) byOrder.set(pk.order, []); byOrder.get(pk.order).push(pk); });
    const t0 = o => Math.min(...byOrder.get(o).map(pk => pk.job ? pk.job.start : 1e6)); [...byOrder.keys()].sort((a, b) => t0(a) - t0(b) || a.i - b.i).forEach(o => byOrder.get(o).sort((a, b) => (a.job ? a.job.start : 1e6) - (b.job ? b.job.start : 1e6)).forEach((pk, k) => { pk.gap = k === 0; list.push(pk); }));
    const key = list.map(pk => pk.key).join(",") + "|" + W.geo().w + "x" + W.geo().h; if (key === seqKey && !force) return; seqKey = key; seq = list;
    const gaps = list.filter((pk, k) => k && pk.gap).length, fk = list.length + "|" + gaps + "|" + W.geo().w + "x" + W.geo().h; if (fk !== fitKey) { fitKey = fk; rings = fit(list.length, gaps); }
    const units = []; const withGaps = rings.gaps; list.forEach((pk, k) => { if (withGaps && k && pk.gap) units.push(null); units.push(pk); });
    let end = units.length; for (const sl of rings.rings) { if (end <= 0) break; const take = Math.min(end, sl.length), part = units.slice(end - take, end); end -= take; const st = part.length > 1 ? Math.min(2, (sl.length - 1) / (part.length - 1)) : 0, off = (sl.length - 1 - (part.length - 1) * st) / 2; part.forEach((pk, k) => { if (pk) pk.slot = sl[Math.round(off + k * st)]; }); }
    const last = rings.rings[rings.rings.length - 1] || [[W.geo().w / 2, 60]]; for (let k = 0; k < end; k++) if (units[k]) units[k].slot = last[k % last.length];   // no room even at the smallest size: share the outer ring's spots rather than hide one
    const maxC = Math.max(.01, ...jobs.map(jobCost)); list.forEach(pk => { pk.R = rings.R * (pk.card ? .82 : .58 + .42 * Math.sqrt(Math.min(1, jobCost(pk.job) / maxC))); pk.sx = pk.slot[0]; pk.sy = pk.slot[1]; }); }
  const pxAt = () => 2 * (camera.position.z - C.z) * Math.tan(camera.fov * Math.PI / 360) / W.geo().h;   // world units per pixel on the packets' plane
  function toWorld(x, y, out) { const g = W.geo(); vp.set(x / g.w * 2 - 1, -(y / g.h) * 2 + 1, .5).unproject(camera).sub(camera.position).normalize(); const t = (C.z - camera.position.z) / vp.z; return out.set(camera.position.x + vp.x * t, camera.position.y + vp.y * t, C.z); }

  // ---- questions: the same stars poured into a new shape. All sizes come from counts.
  let view = "home", morphT0 = -1e9; const morphDur = 2600, vTagsEl = document.getElementById("vtags"), ns = new THREE.Vector3();
  function s2w(x, y, z, i) { const g = W.geo(); ns.set(x / g.w * 2 - 1, -(y / g.h) * 2 + 1, .5).unproject(camera).sub(camera.position).normalize(); const t = (z - camera.position.z) / ns.z; aB[i * 3] = camera.position.x + ns.x * t; aB[i * 3 + 1] = camera.position.y + ns.y * t; aB[i * 3 + 2] = z; }
  const money = v => "$" + v.toFixed(2), pct = (a, b) => Math.round(100 * a / Math.max(b, 1e-9)), plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");
  function stats() { const perOrd = orders.map(() => 0), perBucket = {}, kinds = [0, 0, 0, 0, 0, 0], actBucket = {}; for (let i = 0; i < nStars; i++) { if (aMeta[i * 4] > 1e5) continue; perOrd[jobs[aJ[i]].order.i]++; const b = Math.floor(aMeta[i * 4] / 10); perBucket[b] = (perBucket[b] || 0) + 1; } jobs.forEach(j => j.actions.forEach(a => { kinds[a[1]]++; const b = Math.floor(a[0] / 10); actBucket[b] = (actBucket[b] || 0) + 1; })); kinds[5] = blockedSeen.size; return { perOrd, perBucket, kinds, actBucket, total: kinds.reduce((a, b) => a + b, 0) }; }
  const orderCost = o => o.jobs.reduce((s, j) => s + (j.cost || 0), 0);
  const spent = () => jobs.reduce((s, j) => s + (j.cost || 0), 0);
  const tag = (x, y, big, small, cls = "") => ({ x, y, big, small, cls });
  function layoutView(v) { const g = W.geo(), st = stats(), tags = []; const rank = orders.filter(o => st.perOrd[o.i] > 0).sort((a, b) => st.perOrd[b.i] - st.perOrd[a.i]);
    if (!nStars && v !== "stuck") return { none: true, say: "Nothing has run yet today, so there's no shape to show you.", meta: "" };
    if (v === "spend") { const head = g.h * .185, outer = g.w * .47, n = Math.min(rank.length, 12), narrow = g.w < 1040, each = narrow ? (g.w * .94) / n : (2 * (outer - head)) / n, bw = each * .66, kH = (g.rim - g.h * .1) / Math.max(1, st.perOrd[rank[0].i]); let l = 0, r = 0; rank.forEach((o, k) => { const kk = Math.min(k, n - 1); o._x = narrow ? g.w * .03 + each * (kk + .5) : k % 2 === 0 ? g.cx - head - each * (l++ + .5) : g.cx + head + each * (r++ + .5); o._h = st.perOrd[o.i] * kH; });
      for (let i = 0; i < nStars; i++) { if (aMeta[i * 4] > 1e5) continue; const o = jobs[aJ[i]].order; s2w(o._x + (Math.random() - .5) * bw, W.rimAt(o._x) - 3 - Math.random() * o._h, -.35, i); }
      rank.forEach((o, k) => tags.push(tag(o._x, W.rimAt(o._x) - o._h - 8, o.label, money(orderCost(o)), (narrow && k > 1) || k >= n ? "skip" : "")));
      const top = rank.slice().sort((a, b) => orderCost(b) - orderCost(a))[0], c = orderCost(top), tot = spent(), failed = jobs.filter(bad).reduce((s, j) => s + (j.cost || 0), 0); return { tags, say: `One order cost the most: ${money(c)}, which is ${pct(c, tot)} percent of today's spend.` + (failed > 0 ? ` Jobs that failed cost ${money(failed)}.` : ""), meta: `${top.label} · about ${RATE.toLocaleString()} stars to the dollar` }; }
    if (v === "kinds") { const cx = g.cx, cy = g.h * .27, rx0 = Math.min(g.w * .3, g.h * .42), ry0 = g.h * .135, band = g.h * .06, A0 = 3.5, A1 = -.36, gap = .018, starKinds = [0, 0, 0, 0, 0, 0]; for (let i = 0; i < nStars; i++) if (aMeta[i * 4] < 1e5) starKinds[aMeta[i * 4 + 1]]++; const used = starKinds.map((c, k) => [c, k]).filter(x => x[0] > 0), sum = used.reduce((a, x) => a + x[0], 0), span = A0 - A1 - gap * (used.length - 1); let a = A0; const starts = []; used.forEach(([c, k]) => { const w = span * c / sum; starts[k] = [a, w]; const m = a - w / 2, R = 1 + band / ry0 * (w < .3 ? 1.9 : .62); tags.push(tag(cx + Math.cos(m) * rx0 * R, cy - Math.sin(m) * ry0 * R - 5, KINDS[k][0], st.kinds[k].toLocaleString() + " · " + pct(st.kinds[k], st.total) + "%")); a -= w + gap; });
      for (let i = 0; i < nStars; i++) { if (aMeta[i * 4] > 1e5) continue; const [a0, w] = starts[aMeta[i * 4 + 1]], an = a0 - Math.random() * w, fr = 1 + Math.sqrt(Math.random()) * band / ry0; s2w(cx + Math.cos(an) * rx0 * fr, cy - Math.sin(an) * ry0 * fr, -.3, i); }
      const c = st.kinds, best = [0, 1, 2, 3, 4].sort((x, y) => c[y] - c[x])[0], words = ["reading files", "editing", "running commands", "making requests", "reviewing"]; return { tags, say: `Mostly ${words[best]}: ${pct(c[best], st.total)} percent of the ${st.total.toLocaleString()} actions today.` + (c[5] ? ` The gateway blocked ${plural(c[5], "request")}.` : ""), meta: st.total.toLocaleString() + " actions today" }; }
    if (v === "time") { const bw = W.dayX(10) - W.dayX(0), keys = Object.keys(st.perBucket).map(Number), peak = keys.reduce((a, b) => st.perBucket[b] > st.perBucket[a] ? b : a, keys[0]), kH = g.h * .16 / st.perBucket[peak];
      for (let i = 0; i < nStars; i++) { if (aMeta[i * 4] > 1e5) continue; const b = Math.floor(aMeta[i * 4] / 10), x = W.dayX(b * 10 + 5 - W.dayT0()), H = st.perBucket[b] * kH, xx = x + (Math.random() - .5) * bw * .84; s2w(xx, W.rimAt(xx) - 3 - Math.random() * H, .42, i); }
      const pk = hhmm(peak * 10), px = W.dayX(peak * 10 + 5 - W.dayT0()), acts = st.actBucket[peak] || 0; tags.push(tag(px, W.rimAt(px) - st.perBucket[peak] * kH - 8, "Busiest at " + pk, acts ? plural(acts, "action") + " in 10 min" : "by spend"));
      let gapS = 0, gapL = 0, run = 0; const firstB = Math.min(...keys), last = Math.floor(nowMin() / 10); for (let b = firstB; b <= last; b++) { if (!st.perBucket[b]) { run++; if (run > gapL) { gapL = run; gapS = b - run + 1; } } else run = 0; } const gx = W.dayX((gapS + gapL / 2) * 10 - W.dayT0()); if (gapL >= 3) tags.push(tag(gx, W.rimAt(gx) - 34, "Quiet", hhmm(gapS * 10) + " to " + hhmm((gapS + gapL) * 10)));
      return { tags, say: `The day peaked at ${pk}` + (acts ? `, with ${plural(acts, "action")} in ten minutes.` : ".") + (gapL >= 3 ? ` It went quiet from ${hhmm(gapS * 10)} to ${hhmm((gapS + gapL) * 10)}.` : ""), meta: "Each column is 10 minutes, lined up with the hours below" }; }
    if (v === "stuck") { const items = [], parts = [], at = pk => () => pk.g.position; const failed = jobs.filter(bad); failed.slice(-3).forEach(j => { const pk = PK.get("j" + j.id); if (pk) items.push({ tag: "Failed", name: j.label, at: at(pk) }); }); if (failed.length) parts.push(plural(failed.length, "job") + " failed"); golds.forEach(g => { items.push({ tag: "Waiting for you", name: g.order.label, at: at(g.pk), gold: 1 }); }); if (golds.size) parts.push(plural(golds.size, "write") + " waiting for you"); const again = orders.map(o => o.jobs.filter(j => j.role === "reviewer" && j.verdict).slice(-1)[0]).filter(j => j && j.verdict === "revise"); again.forEach(j => { const pk = PK.get("j" + j.order.jobs.slice(-1)[0].id); if (pk) items.push({ tag: "Changes requested", name: j.order.label, at: at(pk) }); }); if (again.length) parts.push(plural(again.length, "order") + " with changes requested");
      stuckItems = items; if (!parts.length) return { tags: [], say: "Nothing's stuck. Everything looks healthy.", meta: "" }; const list = parts.length > 1 ? parts.slice(0, -1).join(", ") + ", and " + parts[parts.length - 1] : parts[0]; return { tags: [], say: `Here's what needs a look: ${list}. Everything else is healthy.`, meta: "Everything healthy dimmed" }; }
    return { tags: [], say: "", meta: "" }; }
  let stuckItems = [], stuckEls = [];
  function vtagEl(big, small, gold) { const el = document.createElement("div"); el.className = "tag v on" + (gold ? " gold" : ""); const b = document.createElement("b"), s = document.createElement("span"); b.textContent = big; s.textContent = small; el.append(b, s); vTagsEl.appendChild(el); return el; }
  function renderTags(list) { vTagsEl.textContent = ""; stuckEls = []; list.forEach(t => { if (t.cls === "skip") return; const el = vtagEl(t.big, t.small), w = el.offsetWidth, h = el.offsetHeight, g = W.geo(), x = Math.max(6, Math.min(g.w - w - 6, t.x - w / 2)); el.style.transform = `translate(${x}px, ${Math.max(66, t.y - h)}px)`; }); }
  function show(v) { if (v === view && v !== "home") return { same: true }; const wasHome = view === "home" || view === "stuck", toHome = v === "home" || v === "stuck"; if (!wasHome) { aA.set(aB); bA.needsUpdate = true; } const res = layoutView(v); if (res.none) return res; if (!toHome) bB.needsUpdate = true; u.uAHome.value = wasHome ? 1 : 0; u.uBHome.value = toHome ? 1 : 0; if (wasHome !== toHome || !wasHome) { morphT0 = performance.now(); u.uMorph.value = 0; }
    view = v; renderTags(res.tags); if (v === "stuck") stuckItems.forEach(it => { stuckEls.push([vtagEl(it.name, it.tag, it.gold), it]); }); return res; }

  // ---- hover and click: a packet is one target, with a generous ring round it
  let brush = null, brushAt = null, cardX = 0, cardY = 0, hovered = null, lastPick = 0, selKey = ""; const pickMark = new THREE.Object3D(); root.add(pickMark);
  const card = document.createElement("div"); card.className = "skycard lens"; card.hidden = true; document.body.appendChild(card);
  const mins = m => m < 1 ? "under a minute" : Math.round(m) + " min";
  const STATE = { __proto__: null, running: ["Running", "#8fd8ff"], done: ["Done", "#cfe6ff"], failed: ["Failed", "#ff8a8a"], cancelled: ["Cancelled", "#ff8a8a"], interrupted: ["Interrupted", "#ff8a8a"] };
  function cardFor(un) { const rows = [], s = nowMin();
    if (un.kind === "job" || un.kind === "failed") { const j = un.job, st = j.verdict === "approved" ? ["Approved", "#8af0b8"] : j.verdict === "revise" ? ["Changes requested", "#ffb84d"] : STATE[j.status] || [j.status, "#cfe6ff"]; rows.push(["st", st[0], st[1]], ["b", j.task || j.label], ["", "Job on " + j.lane + (j.model ? " · " + j.model : "")], ["", j.status === "running" ? mins(s - j.start) + " so far" : "Took " + mins((j.end || s) - j.start)], ["", j.cost != null ? "Spend: " + money(j.cost) : "Spend: shown when it ends"], ["", "Click to open this job"]); }
    else if (un.kind === "gold") { const c = un.card; rows.push(["st", c.decided ? (c.decided === "approve" ? "Approved · going out" : "Declined") : "Waiting for you", "#ffc84d"], ["b", c.title || (c.tool + " " + c.branch)], ["", c.repo + (c.branch ? " · " + c.branch : "")]); if (c.commits != null || c.file_count != null) rows.push(["", (c.commits != null ? plural(c.commits, "commit") : "") + (c.file_count != null ? " · " + plural(c.file_count, "file") : "")]); }
    else if (un.kind === "action") { const i = un.i, j = jobs[aJ[i]], k = aMeta[i * 4 + 1]; rows.push(["st", KINDS[k][0] + " · " + hhmm(aMeta[i * 4]), KCSS[k]], ["b", j.task || j.label], ["", j.lane + (j.model ? " · " + j.model : "")], ["", "Click to open this job"]); }
    card.textContent = ""; rows.forEach(([k, t, c]) => { const el = document.createElement(k === "b" ? "b" : "span"); el.textContent = t; if (k === "st") { el.className = "st"; el.style.color = c; } card.appendChild(el); }); }
  const unOf = pk => pk.job ? { kind: bad(pk.job) ? "failed" : "job", job: pk.job } : { kind: "gold", card: pk.card };
  function screenOf(pk) { const g = W.geo(); pk.g.getWorldPosition(vp); const d = vp.distanceTo(camera.position); vp.project(camera); return { x: (vp.x * .5 + .5) * g.w, y: (-vp.y * .5 + .5) * g.h, r: pk.g.scale.x / (2 * d * Math.tan(camera.fov * Math.PI / 360) / g.h) }; }
  function pick(px, py) { const g = W.geo(), home = view === "home" || view === "stuck";
    if (home) { let best = null, bd = 1e9; PK.forEach(pk => { if (!pk.g.visible) return; const P = screenOf(pk), d = Math.hypot(P.x - px, P.y - py), reachPx = Math.max(24, P.r * 1.3); if (d < reachPx && d / reachPx < bd) { bd = d / reachPx; best = pk; } }); return best ? unOf(best) : null; }
    let best = -1, bd = 196; const step = Math.max(1, Math.floor(nStars / 3000)); for (let i = 0; i < nStars; i += step) { if (aMeta[i * 4] > 1e5) continue; vp.set(aB[i * 3], aB[i * 3 + 1], aB[i * 3 + 2]); vp.project(camera); const dx = (vp.x * .5 + .5) * g.w - px, dy = (-vp.y * .5 + .5) * g.h - py, d = dx * dx + dy * dy; if (d < bd) { bd = d; best = i; } }
    if (best < 0) return null; pickMark.position.set(aB[best * 3], aB[best * 3 + 1], aB[best * 3 + 2]); return { kind: "action", i: best }; }
  function jobOf(un) { return un.job || (un.kind === "action" ? jobs[aJ[un.i]] : null); }
  const pkOfUn = un => !un ? null : un.card ? PK.get("c" + un.card.id) : (j => j ? PK.get("j" + j.id) : null)(jobOf(un));
  addEventListener("click", e => { if (W.scrubbing || (W.skyBlocked && W.skyBlocked(e.clientX, e.clientY)) || e.target.closest("button,input,textarea,form,a,iframe,.top,.work,.today,.mid,.legend,#monitor-root")) return; const un = pick(e.clientX, e.clientY); if (!un) return; if (un.kind === "gold") { selKey = "c" + un.card.id; W.onPickCard && W.onPickCard(un.card); } else { const j = jobOf(un); if (j) { selKey = "j" + j.id; W.onPickJob && W.onPickJob(j); } } });   // picked again here: packets glide, so the last hover can be stale

  // ---- per frame
  let tEff = nowMin(), scrubT = null, replaySpeed = 0, lastNow = 0;
  const tether = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial(basic({ vertexColors: true, opacity: .55 }))); tether.geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(256 * 6), 3)); tether.geometry.setAttribute("color", new THREE.BufferAttribute(new Float32Array(256 * 6), 3)); tether.frustumCulled = false; root.add(tether);
  function update(now, px, py) { const s = nowMin(), t = now / 1000, realDt = (now - lastNow) / 1000, dt = Math.min(.1, realDt); lastNow = now; u.uPx.value = Math.min(1.5, devicePixelRatio || 1) * (innerHeight / 900);
    if (scrubT != null) tEff = scrubT; else if (tEff < s - .001) tEff = replaySpeed ? Math.min(s, tEff + Math.min(1, realDt) * replaySpeed) : s; else tEff = s; if (tEff >= s) replaySpeed = 0; u.uT.value = tEff; const rewinding = tEff < s - .5;
    if (!sil && W.geo().R) relayout(true);
    orders.forEach(o => { o.rate += ((o.frozen ? 0 : 1) - o.rate) * Math.min(1, dt * .9); const fx = u.uOrdFx.value[oi(o)]; fx.y = Math.max(0, fx.y - dt * .6); });
    const mp = Math.min(1, (now - morphT0) / morphDur); u.uMorph.value = mp; const tv = view === "home" || view === "stuck" ? 0 : 1; u.uViewAmt.value += (tv - u.uViewAmt.value) * Math.min(1, dt * 1.6); u.uPure.value += ((view === "kinds" ? 1 : view === "spend" || view === "time" ? .35 : 0) - u.uPure.value) * Math.min(1, dt * 1.6); u.uVA.value += ((view === "time" ? .36 : 1) - u.uVA.value) * Math.min(1, dt * 1.6);
    const hovJob = hovered ? jobOf(hovered) : null, hovPk = pkOfUn(hovered), hovOrder = hovJob ? hovJob.order : hovPk ? hovPk.order : null, trouble = j => bad(j) || j.verdict === "revise" || j.order.frozen;
    const mon = window.FieldMonitor && FieldMonitor.current ? FieldMonitor.current() : null; if (mon) { const j = mon.job ? J.get(mon.job) : jobs.filter(x => x.lane === mon.lane).slice(-1)[0]; if (j) selKey = "j" + j.id; }
    // live stars: a job catches up with its actions and spend a few at a time, born at its packet's core
    if (view === "home" && !rewinding && mp >= 1 && nStars < MAXSTARS - 1) { const from = nStars; jobs.forEach(j => { const need = targetOf(j) - j.placed; if (need > 0) { const n = Math.min(40, Math.max(1, Math.ceil(need / 30))); place(j, n, s - Math.random() * .05); } }); if (nStars > from) markDirty(from); if (nStars > gasAt * 1.5 + 200) buildGas(); }
    flushStars();
    const wpp = pxAt(), homeAmt = 1 - u.uViewAmt.value, ease = Math.min(1, dt * 4);
    PK.forEach(pk => { const j = pk.job, o = pk.order; if (pk.sx == null || pk.R == null) return; toWorld(pk.sx, pk.sy, pk.to); pk.tw = pk.R * wpp;
      if (!pk.placed) { pk.pos.copy(pk.to); pk.w = pk.tw; pk.placed = true; } else { pk.pos.lerp(pk.to, ease); pk.w += (pk.tw - pk.w) * ease; }
      const running = j && j.status === "running", failed = j && bad(j), gold = !!pk.card, shown = gold ? !rewinding : tEff >= j.start - .01, isHov = hovPk === pk, isSel = selKey === pk.key, open = isSel && !!mon;
      const grow = Math.min(1, (now - pk.born) / 700), dim = view === "stuck" ? (gold || (j && trouble(j)) ? 1.4 : .2) : hovOrder ? (o === hovOrder ? 1.25 : .55) : 1;
      pk.hot += ((isHov ? 1 : 0) - pk.hot) * Math.min(1, dt * 8); pk.g.position.copy(pk.pos); pk.g.scale.setScalar(Math.max(1e-5, pk.w * (1 + .1 * pk.hot) * grow)); pk.g.visible = shown && homeAmt > .02;
      pk.spin += dt * (running ? .55 : gold ? .12 : .06) * o.rate; pk.hull.rotation.y = pk.yaw0 + pk.spin * 1.6;
      const col = gold ? GOLD : failed ? RED : o.color, a = homeAmt * dim;
      pk.edges.material.color.copy(col).multiplyScalar(failed ? .55 : 1); pk.edges.material.opacity = Math.min(1, (gold ? .95 + .25 * Math.sin(t * 2.6) : running ? .8 + .2 * Math.sin(t * 3 + pk.spin) : .62) * a + .35 * pk.hot);
      pk.faces.material.blending = failed ? THREE.NormalBlending : THREE.AdditiveBlending; pk.faces.material.color.copy(failed ? DARK : col); pk.faces.material.opacity = (failed ? .82 : gold ? .16 : .07 + .07 * pk.hot) * Math.min(1, a);
      if (j && pk.bandN !== j.actions.length + (j.blocked || 0) * 1000) paintBand(pk); else if (!j && pk.bandN < 0) paintBand(pk); pk.band.material.opacity = (failed ? .3 : .62) * Math.min(1, a);
      if (failed && !pk.voidS) { pk.voidS = sprite(VOIDT, new THREE.Color(1, 1, 1), 1.25, .95, false); pk.voidS.renderOrder = 3; pk.g.add(pk.voidS); } if (pk.voidS) pk.voidS.material.opacity = .95 * homeAmt;
      const sz = gold ? 1.3 : running ? (j.role === "reviewer" ? .7 : sizeOf(j.model) * 4.2) : 0, pulse = gold ? (pk.card.decided ? .7 : .86 + .22 * Math.sin(t * 2.6)) : .92 + .12 * Math.sin(t * 7 + pk.R); pk.core.material.color.copy(gold ? GOLD : j && j.role === "reviewer" ? REVIEW : o.spark); pk.core.scale.setScalar(sz * pulse + 1e-4); pk.core.material.opacity = sz ? Math.min(1, homeAmt * (view === "stuck" && !(gold || trouble(j)) ? .2 : 1)) : 0;
      pk.mark = open ? "open" : isSel ? "last" : isHov ? "hover" : ""; const lockOn = isHov ? .95 : open ? 1 : isSel ? .4 : 0; pk.lockA += (lockOn - pk.lockA) * Math.min(1, dt * 7); pk.lock.material.opacity = pk.lockA * homeAmt; pk.lock.material.color.copy(gold ? GOLD : isHov && !open ? HOVER : LOCKC); pk.lock.material.rotation = open ? t * .6 : isHov ? t * .25 : 0; pk.lock.scale.setScalar(3.1 * (isHov ? 1 + .04 * Math.sin(t * 5) : 1));
      if (j) { const n = Math.max(1, j.stars.length), dens = Math.max(.04, Math.min(1, Math.sqrt(.07 * pk.R * pk.R / n))); u.uJob.value[ji(j)].set(0, j.scanOn ? 1 : 0, pk.spin, dens); u.uJobP.value[ji(j)].set(pk.pos.x, pk.pos.y, pk.pos.z, shown ? pk.g.scale.x : 0); }
      if (running && j.role !== "reviewer" && !rewinding && view === "home" && Math.random() < dt * .25) { const sp = sprite(FLARE, new THREE.Color(.75, .92, 1), .035, .95); sp.renderOrder = 4; sp.position.copy(pk.pos); root.add(sp); tmp.set(pk.sx - sil.cx, -(pk.sy - sil.cy), 0).normalize(); ships.push({ s: sp, from: pk.pos.clone(), dir: tmp.clone(), len: pk.w * 3.2, t0: now, dur: 1200 }); } });
    jobs.forEach(j => { const tg = view === "stuck" ? (trouble(j) ? 2.1 : .09) : hovOrder ? (j.order === hovOrder ? 1.45 : .62) : bad(j) ? .45 : 1; j.glow = (j.glow == null ? 1 : j.glow) + (tg - (j.glow == null ? 1 : j.glow)) * Math.min(1, dt * 3.2); u.uJob.value[ji(j)].x = j.glow; });
    // one order's packets are joined by a thread, only between neighbours on the same stretch of ring
    const tp = tether.geometry.attributes.position, tc = tether.geometry.attributes.color, lim = (rings ? rings.R * 2.6 : 60) * 2.2; let ns2 = 0;
    for (let k = 1; k < seq.length && ns2 < 256; k++) { const a = seq[k - 1], b = seq[k]; if (a.order !== b.order || !a.g.visible || !b.g.visible || Math.hypot(a.sx - b.sx, a.sy - b.sy) > lim) continue; tmp.subVectors(b.pos, a.pos); const L = tmp.length(); if (L < 1e-6) continue; tmp.multiplyScalar(1 / L); tmp2.copy(a.pos).addScaledVector(tmp, a.g.scale.x * .95); tp.setXYZ(ns2 * 2, tmp2.x, tmp2.y, tmp2.z); tmp2.copy(b.pos).addScaledVector(tmp, -b.g.scale.x * .95); tp.setXYZ(ns2 * 2 + 1, tmp2.x, tmp2.y, tmp2.z); const c = a.order.color, m = homeAmt * (hovOrder === a.order ? 1 : hovOrder ? .3 : .7); tc.setXYZ(ns2 * 2, c.r * m, c.g * m, c.b * m); tc.setXYZ(ns2 * 2 + 1, c.r * m, c.g * m, c.b * m); ns2++; }
    tether.geometry.setDrawRange(0, ns2 * 2); tp.needsUpdate = true; tc.needsUpdate = true;
    for (let i = ships.length - 1; i >= 0; i--) { const p = ships[i], f = (now - p.t0) / p.dur; p.s.position.copy(p.from).addScaledVector(p.dir, p.len * Math.min(1, f)); p.s.material.opacity = .95 * (1 - f); p.s.visible = view === "home" && !rewinding; if (f >= 1) { root.remove(p.s); p.s.material.dispose(); ships.splice(i, 1); } }
    for (let i = flashes.length - 1; i >= 0; i--) { const fl = flashes[i], f = (now - fl.t0) / fl.dur; fl.s.material.opacity = Math.max(0, .95 * (1 - f)); fl.s.scale.setScalar(fl.size * (.6 + f)); if (f >= 1) { root.remove(fl.s); fl.s.material.dispose(); flashes.splice(i, 1); } }
    if (view === "stuck") stuckEls.forEach(([el, it]) => { const P = it.at().clone().project(camera), g = W.geo(), w = el.offsetWidth, x = Math.max(6, Math.min(g.w - w - 6, (P.x * .5 + .5) * g.w - w / 2)), y = Math.max(66, Math.min(W.rimAt(x + w / 2) - el.offsetHeight - 14, (-P.y * .5 + .5) * g.h + 16)); el.style.transform = `translate(${x}px, ${y}px)`; });
    if (now - lastPick > 120 && !W.scrubbing) { lastPick = now; const blocked = W.skyBlocked && W.skyBlocked(px, py), un = brush || (blocked || W.overUI ? null : pick(px, py)); hovered = un; if (un) { cardFor(un); card.hidden = false; } else card.hidden = true; } else if (W.scrubbing) { hovered = null; card.hidden = true; }
    if (!card.hidden) { const w = card.offsetWidth, h = card.offsetHeight, ax = brush ? brushAt.x : px, ay = brush ? brushAt.y : py; cardX = Math.max(30, Math.min(innerWidth - w - 34, ax + 22)); const headL = sil ? sil.box[0] : innerWidth / 2 - innerHeight * .165, headR = sil ? sil.box[2] : innerWidth / 2 + innerHeight * .165; if (!brush && ay < innerHeight * .52 && cardX < headR && cardX + w > headL) cardX = ax < innerWidth / 2 ? Math.max(30, ax - 22 - w) : Math.min(innerWidth - w - 8, headR + 8); cardY = brush ? Math.max(70, ay - h - 26) : Math.max(70, Math.min(innerHeight - h - 12, ay + 22)); card.style.left = cardX + "px"; card.style.top = cardY + "px"; }
    document.body.style.cursor = hovered && !W.overUI ? "pointer" : ""; }
  const pkFor = id => PK.get("j" + id) || null;
  return { update, ingest,
    get view() { return view; }, get rate() { return RATE; }, get stars() { return nStars; }, get hasWork() { return jobs.length > 0; },
    show, rescale() { relayout(true); if (view === "spend" || view === "kinds" || view === "time") { const r = layoutView(view); if (!r.none) { bB.needsUpdate = true; renderTags(r.tags); } } },
    replay() { let earliest = 1e6; for (let i = 0; i < nStars; i += 50) earliest = Math.min(earliest, aMeta[i * 4]); if (earliest < 1e5) { const s = nowMin(); tEff = earliest; replaySpeed = Math.max(1, (s - earliest) / 3.4); } },
    scrub(tm) { scrubT = tm; }, firstStart() { return jobs.length ? Math.min(...jobs.map(j => j.start)) : null; },
    sparkList() { const out = []; PK.forEach(pk => { if (pk.card) out.push({ id: pk.card.id, name: "Waiting for you", mesh: pk.g, size: pk.w, gold: true }); else if (pk.job.status === "running") out.push({ id: pk.job.id, name: pk.job.label, mesh: pk.g, size: pk.w, gold: false }); }); return out; },
    // where every packet is on screen now: for the labels, and for the browser test that checks none is behind the witness
    packets() { const out = []; PK.forEach(pk => { if (!pk.g.visible) return; const P = screenOf(pk); out.push({ id: pk.card ? pk.card.id : pk.job.id, kind: pk.card ? "gold" : pk.job.status, x: P.x, y: P.y, r: P.r, mark: pk.mark || "" }); }); return out; },
    overWitness(x, y, pad) { return inOutline(x, y, pad); },
    jobMesh(id) { const pk = pkFor(id); return pk ? pk.g : null; }, goldMesh(id) { const g = id ? golds.get(id) : golds.values().next().value; return g ? g.pk.g : null; },
    job: id => J.get(id) || null, brush(un, at) { brush = un; brushAt = at || null; lastPick = 0; }, jobU(id) { const j = J.get(id); return j ? { kind: bad(j) ? "failed" : "job", job: j } : null; },
    hoverInfo() { if (!hovered || card.hidden) return null; const mesh = hovered.kind === "action" ? pickMark : (pkOfUn(hovered) || {}).g; return mesh ? { mesh, x: cardX - 8, y: cardY + 10 } : null; },
  };
};
