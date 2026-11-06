// LOIS's bust and the renderer (doomtickle/passdesk-ops#207): three.js, the Blender model, lip sync and bloom. Display only.
"use strict";
(function () {
  // ---------- the bust: three.js r147 (22 morphs need its morph texture), the Blender model, bloom
  const cfg = { pres: .75 };
  const W = { ready: false, failed: false, s: { pres: .75, flash: 0, yaw: 0, pitch: 0, gx: 0, gy: 0 }, m: {}, speaking: false, thinking: false, talkKick: 0, mood: "calm",
    blinkAt: performance.now() + 2000, blinking: 0, lookAway: 0, nextLook: performance.now() + 6000, lookUntil: 0, lookTo: [0, 0] };
  const VISEMES = ["PP", "FF", "TH", "DD", "kk", "CH", "SS", "nn", "RR", "aa", "E", "ih", "oh", "ou"].map(v => "viseme_" + v);
  const MORPHS = ["blink", "lids_wide", "lids_narrow", "brow_up", "brow_down", "jaw_open", "mouth_round", "smile", ...VISEMES];
  MORPHS.forEach(k => W.m[k] = 0);
  const canvas = document.getElementById("being");
  let renderer, scene, camera, composer, bloom, head, eyeL, eyeR, corneaL, corneaR, faceMeshes = [], skinMats = [], eyeMats = [], halo, stars;
  const U = { uTime: { value: 0 }, uPres: { value: .75 }, uFlash: { value: 0 }, uTalk: { value: 0 } };
  const LF = window.LF = { cfg, W, U };
  try {
    if (!window.THREE || !THREE.GLTFLoader || !THREE.EffectComposer || !THREE.UnrealBloomPass) throw new Error("three.js or its examples did not load");
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
    renderer.setPixelRatio(Math.min(1.5, devicePixelRatio || 1)); renderer.setSize(innerWidth, innerHeight, false);
    scene = new THREE.Scene(); scene.background = new THREE.Color(0x020409);
    camera = new THREE.PerspectiveCamera(28, innerWidth / innerHeight, .01, 40);
    camera.position.set(0, .2, 1.2);
    // lights: a cool key from upper left, a hard cyan rim from behind, a faint uplight
    scene.add(new THREE.HemisphereLight(0x3a6aa8, 0x02040a, .35));
    const key = new THREE.DirectionalLight(0xcfe4ff, .7); key.position.set(-.6, .9, 1.1); scene.add(key);
    const rimR = new THREE.DirectionalLight(0x5fd6ff, 1.6); rimR.position.set(1.1, .5, -1.0); scene.add(rimR);
    const rimL = new THREE.DirectionalLight(0x3b7dff, 1.0); rimL.position.set(-1.2, .3, -.8); scene.add(rimL);
    const up = new THREE.DirectionalLight(0x2a8cff, .35); up.position.set(0, -1, .6); scene.add(up);
    // stars far behind
    const sp = new Float32Array(900 * 3); for (let i = 0; i < 900; i++) { sp[i * 3] = (Math.random() - .5) * 9; sp[i * 3 + 1] = (Math.random() - .3) * 6; sp[i * 3 + 2] = -3 - Math.random() * 4; }
    const sg = new THREE.BufferGeometry(); sg.setAttribute("position", new THREE.BufferAttribute(sp, 3));
    stars = new THREE.Points(sg, new THREE.PointsMaterial({ color: 0x9fc8ff, size: .012, transparent: true, opacity: .7, depthWrite: false })); scene.add(stars);
    // a soft aura behind the head
    const c = document.createElement("canvas"); c.width = c.height = 256; const x = c.getContext("2d"); const g = x.createRadialGradient(128, 128, 0, 128, 128, 128);
    g.addColorStop(0, "rgba(95,214,255,.55)"); g.addColorStop(.4, "rgba(40,120,255,.18)"); g.addColorStop(1, "rgba(0,0,0,0)"); x.fillStyle = g; x.fillRect(0, 0, 256, 256);
    halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: .5 }));
    halo.scale.set(.9, .9, 1); scene.add(halo);
    // post: bloom, then gamma (the composer's targets are linear)
    const bufType = renderer.capabilities.isWebGL2 && renderer.extensions.has("EXT_color_buffer_float") ? THREE.HalfFloatType : THREE.UnsignedByteType; composer = new THREE.EffectComposer(renderer, new THREE.WebGLRenderTarget(innerWidth, innerHeight, { samples: 4, type: bufType })); composer.setPixelRatio(Math.min(1.5, devicePixelRatio || 1)); composer.setSize(innerWidth, innerHeight);
    composer.addPass(new THREE.RenderPass(scene, camera));
    composer.addPass(new THREE.ShaderPass({ uniforms: { tDiffuse: { value: null } }, vertexShader: THREE.CopyShader.vertexShader, fragmentShader: "uniform sampler2D tDiffuse; varying vec2 vUv; void main(){ gl_FragColor = min(texture2D(tDiffuse, vUv), vec4(1.)); }" }));
    bloom = new THREE.UnrealBloomPass(new THREE.Vector2(innerWidth, innerHeight), .55, .42, .32); [bloom.renderTargetBright, ...bloom.renderTargetsHorizontal, ...bloom.renderTargetsVertical].forEach(t => { t.texture.type = bufType; }); composer.addPass(bloom);
    composer.addPass(new THREE.ShaderPass({ uniforms: { tDiffuse: { value: null } }, vertexShader: THREE.CopyShader.vertexShader, fragmentShader: "uniform sampler2D tDiffuse; varying vec2 vUv; void main(){ gl_FragColor = LinearTosRGB(texture2D(tDiffuse, vUv)); gl_FragColor.rgb += (fract(sin(dot(gl_FragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453) - .5) / 255.; }" }));
    addEventListener("resize", () => { renderer.setSize(innerWidth, innerHeight, false); composer.setSize(innerWidth, innerHeight); camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); });

    // textures: all seven are linear data on one atlas, baked for glTF UVs (no flip). Head, Torso and Mouth_Interior share it.
    const tl = new THREE.TextureLoader();
    const tex = (u) => { const t = tl.load("/witness/" + u); t.flipY = false; t.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy()); return t; };
    const veins = tex("veins_mask.png"), seam = tex("seam_mask.png"), ao = tex("ao.png"), detail = tex("normal_detail.png"), rough = tex("roughness.png"), thick = tex("thickness.png"), curv = tex("curvature.png");

    function skin() {
      const m = new THREE.MeshStandardMaterial({ color: new THREE.Color(.025, .085, .24), roughness: 1, roughnessMap: rough, metalness: .05, normalMap: detail, normalScale: new THREE.Vector2(1, 1), emissive: new THREE.Color(1, 1, 1), emissiveMap: veins, aoMap: ao, aoMapIntensity: 1, side: THREE.DoubleSide });
      m.onBeforeCompile = sh => {
        Object.assign(sh.uniforms, U, { uSeam: { value: seam }, uThick: { value: thick }, uCurv: { value: curv } });
        sh.fragmentShader = "uniform float uTime; uniform float uPres; uniform float uFlash; uniform float uTalk; uniform sampler2D uSeam; uniform sampler2D uThick; uniform sampler2D uCurv;\n" + sh.fragmentShader.replace("#include <color_fragment>", `#include <color_fragment>
          diffuseColor.rgb *= clamp(1.0 + (texture2D(uCurv, vUv).r - .5) * 3.0, .4, 1.5);   // creases darker, ridges lighter
        `).replace("#include <aomap_fragment>", `#include <aomap_fragment>
          float occD = texture2D(aoMap, vUv2).r;                // no shadow maps here, so the AO also shades the direct light
          reflectedLight.directDiffuse *= mix(1.0, occD, .7);
          vec3 spec = reflectedLight.directSpecular * occD;     // soft ceiling: grazing rim light would otherwise blow out the side of the nose
          reflectedLight.directSpecular = spec / (1.0 + spec * 2.2);
        `).replace("#include <emissivemap_fragment>", `
          float vein = texture2D(emissiveMap, vUv).r;
          float sm = texture2D(uSeam, vUv).r;
          float occ = texture2D(aoMap, vUv2).r;
          float inside = smoothstep(.42, .82, occ);            // the mouth and the deep creases stay dark
          float flow = .5 + .5 * sin(uTime * 1.25 + vUv.y * 34.0 + vUv.x * 9.0 + vein * 5.0);
          float surge = .5 + .5 * sin(uTime * .37 + vUv.x * 3.0);
          vec3 glow = vec3(.25, .72, 1.0);
          vec3 vdir = normalize(vViewPosition);
          float fres = pow(1.0 - clamp(abs(dot(normal, vdir)), 0.0, 1.0), 2.4);
          totalEmissiveRadiance = glow * vein * (.15 + 1.05 * flow * surge) * (.4 + .6 * uPres);
          totalEmissiveRadiance += vec3(.82, .95, 1.0) * sm * (1.6 + .6 * sin(uTime * 1.3)) * (.55 + .6 * uPres);
          totalEmissiveRadiance += glow * (.035 + .32 * fres) * (.5 + .5 * uPres);
          float thin = 1.0 - texture2D(uThick, vUv).r;         // light through the ears, lids, nostrils and lip edges
          totalEmissiveRadiance += vec3(.2, .62, 1.0) * thin * thin * (.05 + .14 * uPres);
          totalEmissiveRadiance *= inside;
          totalEmissiveRadiance += vec3(uFlash) * inside;
        `);
      };
      skinMats.push(m); return m;
    }
    function eye() {
      const m = new THREE.ShaderMaterial({ uniforms: Object.assign({ uCol: { value: new THREE.Color(.55, .9, 1.0) } }, U),
        vertexShader: "varying vec3 vN; void main(){ vN = normal; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }",
        fragmentShader: `uniform vec3 uCol; uniform float uTime; uniform float uPres; uniform float uFlash; varying vec3 vN;
          void main(){ float a = dot(normalize(vN), vec3(0.0, 0.0, 1.0));
            float core = smoothstep(.55, .985, a); float hot = smoothstep(.93, 1.0, a);
            vec3 c = uCol * (.35 + 1.5 * core) * (.75 + .5 * uPres) * (1.0 + .08 * sin(uTime * 1.7)) + vec3(1.0) * hot * .9 + vec3(uFlash);
            gl_FragColor = vec4(c, 1.0); }` });
      eyeMats.push(m); return m;
    }
    // clear shell over the eye: adds only its reflections, so it never dims the glow behind it
    function cornea() { return new THREE.MeshStandardMaterial({ color: 0x000000, roughness: .06, metalness: 0, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }); }
    // cavity, teeth and tongue, coloured by COLOR_0; AO keeps it dark even with the jaw open
    function mouth() {
      const m = new THREE.MeshStandardMaterial({ color: new THREE.Color(.32, .44, .62), vertexColors: true, roughness: 1, roughnessMap: rough, metalness: 0, normalMap: detail, aoMap: ao, aoMapIntensity: 1, side: THREE.DoubleSide });
      m.onBeforeCompile = sh => { sh.fragmentShader = sh.fragmentShader.replace("#include <aomap_fragment>", `#include <aomap_fragment>
          float occD = texture2D(aoMap, vUv2).r; reflectedLight.directDiffuse *= occD * occD; reflectedLight.directSpecular *= occD * occD;
        `); };
      return m;
    }
    const onModel = gltf => {
      const root = gltf.scene; scene.add(root);
      root.traverse(o => {
        if (!o.isMesh) return;
        if (o.geometry.attributes.uv && !o.geometry.attributes.uv2) o.geometry.setAttribute("uv2", o.geometry.attributes.uv);
        o.material = /^Eye_/.test(o.name) ? eye() : /^Cornea_/.test(o.name) ? cornea() : o.name === "Mouth_Interior" ? mouth() : skin();
        if (o.morphTargetDictionary && "blink" in o.morphTargetDictionary) faceMeshes.push(o);   // Head and Mouth_Interior share every weight
      });
      head = root.getObjectByName("Head"); eyeL = root.getObjectByName("Eye_L"); eyeR = root.getObjectByName("Eye_R"); corneaL = root.getObjectByName("Cornea_L"); corneaR = root.getObjectByName("Cornea_R");
      if (!head || !eyeL || !eyeR || !corneaL || !corneaR || faceMeshes.length !== 2) throw new Error("the model is missing Head, Eye_L, Eye_R, its corneas or its morph targets");
      // frame it: crown near the top, chin near the middle, so the words sit over the chest
      root.updateMatrixWorld(true);
      const box = new THREE.Box3().setFromObject(head); const crown = box.max.y, chin = crown - .25, span = (crown - chin) / .41;
      const cy = crown + .05 * span - span / 2; const dist = (span / 2) / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
      const eyeW = new THREE.Vector3(); eyeL.getWorldPosition(eyeW);
      camera.position.set(0, cy + .02, eyeW.z + dist); camera.lookAt(0, cy, eyeW.z);
      halo.position.set(0, eyeW.y + .02, box.min.z - .05);
      W.ready = true; W.eyeY = eyeW.y; W.eyeZ = eyeW.z;
      LF.sky = window.makeSky(scene, camera, new THREE.Vector3(0, eyeW.y, eyeW.z - .08), U, W); LF.camera = camera; LF.onReady && LF.onReady();
    };
    const onFail = err => { W.failed = true; console.error("glb:", err); (LF.failMsg = "The bust did not load."), LF.fail && LF.fail(LF.failMsg); };
    const loader = new THREE.GLTFLoader();
    loader.load("/witness/witness.glb", g => { try { onModel(g); } catch (e) { onFail(e); } }, undefined, onFail);
  } catch (e) { W.failed = true; console.error(e); LF.failMsg = "This view needs WebGL."; LF.fail && LF.fail(LF.failMsg); }

  const tmpF = typeof THREE !== "undefined" ? new THREE.Vector3() : null;
  const ease = (a, b, k) => a + (b - a) * k;
  const tmp = typeof THREE !== "undefined" ? new THREE.Vector3() : null;
  function drawBeing(b, now, px, py) {
    if (!renderer) return;
    U.uTime.value = now / 1000;
    if (!b.ready) { composer.render(); return; }
    if (LF.sky) LF.sky.update(now, px, py);
    const fo = b.hoverFocus || b.focus; if (fo) { fo.getWorldPosition(tmpF); tmpF.project(camera); px = (tmpF.x * .5 + .5) * innerWidth; py = (-tmpF.y * .5 + .5) * innerHeight; }
    const s = b.s;
    // where is the face on screen? gaze is measured from there
    tmp.set(0, b.eyeY, b.eyeZ).project(camera); const fx = (tmp.x * .5 + .5) * innerWidth, fy = (-tmp.y * .5 + .5) * innerHeight;
    let gx = (px - fx) / innerHeight * 1.6, gy = -(py - fy) / innerHeight * 1.6; const L = Math.hypot(gx, gy); if (L > 1) { gx /= L; gy /= L; }
    // now and then, when silent, it looks at something you cannot see
    if (!b.speaking && !fo && now > b.nextLook) { if (Math.random() < .45) { b.lookUntil = now + 1400 + Math.random() * 1800; b.lookTo = [(Math.random() - .5) * 1.4, .2 + Math.random() * .4]; } b.nextLook = now + 5000 + Math.random() * 5000; }
    if (now < b.lookUntil) { gx = b.lookTo[0]; gy = b.lookTo[1]; }
    // thinking about what you just said: it looks up and away, and its gaze wanders slowly
    const thinking = b.thinking && !b.speaking; if (thinking) { gx = .42 + Math.sin(now / 2300) * .24; gy = .5 + Math.sin(now / 1700) * .1; }
    // blink, a little slower than a person
    if (now > b.blinkAt) { b.blinking = now; b.blinkAt = now + 3000 + Math.random() * 6000; }
    const bl = b.blinking ? Math.sin(Math.min(Math.PI, (now - b.blinking) / 230 * Math.PI)) : 0; if (b.blinking && now - b.blinking > 230) b.blinking = 0;
    // speech envelope, kicked by word boundaries
    b.talkKick *= .8;
    const live = window.LiveVoice ? LiveVoice.mouth() : { open: 0, round: 0 }; const env = b.speaking ? live.open : 0;
    const vis = b.speaking && window.LiveVoice ? LiveVoice.visemes() : null;   // phoneme-timed lips; without timing data, the jaw follows loudness as before
    const stern = b.mood === "stern", lit = b.mood === "lit";
    const T = { blink: bl, lids_wide: lit ? .35 : 0, lids_narrow: stern ? .7 : thinking ? .38 : .22, brow_up: lit ? .55 : thinking ? .24 : (b.speaking ? .18 * Math.abs(Math.sin(now / 1100)) : 0), brow_down: stern ? .65 : 0,
      jaw_open: Math.min(1, env) * (vis ? .22 * (1 - vis.viseme_PP - vis.viseme_FF) : .9), mouth_round: vis ? 0 : Math.min(1, env) * Math.min(1, Math.max(0, (live.round - .55) * 3.2)) * .7, smile: lit ? .35 : .1 };
    VISEMES.forEach(k => { T[k] = vis ? vis[k] : 0; });
    MORPHS.forEach(k => { b.m[k] = ease(b.m[k], T[k], k === "blink" ? .55 : k === "jaw_open" || k === "mouth_round" ? .4 : k.startsWith("viseme") ? .7 : .07); faceMeshes.forEach(f => { f.morphTargetInfluences[f.morphTargetDictionary[k]] = b.m[k]; }); });
    s.gx = ease(s.gx, gx, .12); s.gy = ease(s.gy, gy, .12);
    s.yaw = ease(s.yaw, gx * .32, .035); s.pitch = ease(s.pitch, -gy * .14, .035);
    head.rotation.y = s.yaw; head.rotation.x = s.pitch + Math.sin(now / 2900) * .012; head.rotation.z = Math.sin(now / 4100) * .01;
    [eyeL, eyeR, corneaL, corneaR].forEach(e => { e.rotation.y = (s.gx * .55) - s.yaw * .5; e.rotation.x = -s.gy * .38 - s.pitch * .5; });
    s.pres = ease(s.pres, b.speaking ? 1 : thinking ? .9 + Math.sin(now / 900) * .07 : cfg.pres, .05); s.flash *= .9;
    U.uPres.value = s.pres; U.uFlash.value = s.flash; U.uTalk.value = env;
    halo.material.opacity = .14 + .16 * s.pres + s.flash * .5;
    bloom.strength = .42 + .22 * s.pres + s.flash;
    stars.rotation.y = now / 400000;
    composer.render();
  }
  LF.drawBeing = drawBeing; let px = innerWidth / 2, py = innerHeight * .4; addEventListener("pointermove", e => { px = e.clientX; py = e.clientY; }); LF.pointer = () => [px, py];
  // motes: light drifting off the skin
  const mc = document.getElementById("motes"), mx = mc.getContext("2d"); const motes = Array.from({ length: 140 }, () => ({ x: Math.random(), y: Math.random(), s: .3 + Math.random() * 1.4, v: .0002 + Math.random() * .0006, p: Math.random() * 7 }));
  // ---------- voice and chime
  let on = true, ac = null;
  function audio() { if (ac) return; ac = new (window.AudioContext || window.webkitAudioContext)(); }
  function chime() { if (!ac || !on) return; const t = ac.currentTime; [523.25, 783.99, 1046.5].forEach((f, i) => { const o = ac.createOscillator(); o.type = "sine"; o.frequency.value = f; const g = ac.createGain(); g.gain.setValueAtTime(.12 / (i + 1), t + i * .05); g.gain.exponentialRampToValueAtTime(.0001, t + 4); o.connect(g); g.connect(ac.destination); o.start(t + i * .05); o.stop(t + 4); }); }
  LF.audio = audio; LF.chime = chime; LF.muted = () => !on; LF.setOn = v => { on = v; };
  LF.drawMotes = (now) => { const w = innerWidth, h = innerHeight; if (mc.width !== w || mc.height !== h) { mc.width = w; mc.height = h; } mx.clearRect(0, 0, w, h); motes.forEach(m => { m.y -= m.v; if (m.y < -.02) { m.y = 1.02; m.x = .3 + Math.random() * .4; } const X = (m.x + Math.sin(now / 3000 + m.p) * .015) * w, Y = m.y * h; const a = .25 + .5 * Math.sin(now / 700 + m.p) ** 2; mx.fillStyle = `rgba(140,220,255,${a * (W.s ? W.s.pres : 1)})`; mx.beginPath(); mx.arc(X, Y, m.s, 0, 7); mx.fill(); }); };
})();
