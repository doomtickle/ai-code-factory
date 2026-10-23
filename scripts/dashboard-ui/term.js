// Terminal 0: the Coordinator's live terminal (xterm.js), on the dashboard's
// terminal origin. Binary WebSocket frames carry raw terminal bytes both ways;
// text frames carry small JSON control messages (resize, power, state).
// The terminal key arrives in a <meta> only this origin can read, goes out as
// the WebSocket subprotocol, and the <meta> is removed. No clipboard, link or
// image addons; OSC 8 links do nothing.
// Attaching never starts the Coordinator: this page asks for it with a
// {"type":"power"} message, once on load (only if it is off and not open
// elsewhere) and whenever the power key is pressed.
"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const meta = document.querySelector('meta[name="term-key"]');
  const key = meta ? meta.getAttribute("content") : "";
  if (meta) meta.remove();

  const CHUNK = 16 * 1024;  // the server refuses messages over 64 KiB: split big pastes
  const REASONS = {
    elsewhere: ["TERMINAL 0 · OPEN ELSEWHERE",
      "The Coordinator is open elsewhere (`factory chat`). Close it there, then power on here."],
    "no-sandbox": ["TERMINAL 0 · NO COORDINATOR", "The Coordinator sandbox doesn't exist yet: run `factory start` first, then power on."],
    "no-sbx": ["TERMINAL 0 · NO SBX", "`sbx` could not run on this machine. Run `factory doctor`, then power on."],
    error: ["TERMINAL 0 · FAULT", "The Coordinator could not be started. Power on to try again."],
    exited: ["TERMINAL 0 · POWERED OFF", "The Coordinator session ended. Power on to start it again."],
    lost: ["TERMINAL 0 · NO SIGNAL", "Lost the dashboard. Reconnecting…"],
    framed: ["TERMINAL 0 · NOT IN THE DASHBOARD", "Open the LOIS dashboard; this page only works inside it."],
    service: ["TERMINAL 0 · SERVICE", "The factory is restarting or updating. The Coordinator comes back when it's done."],
  };

  // Opened on its own (not framed by the dashboard): never connect.
  if (window.top === window) {
    const show = () => {
      $("off-title").textContent = REASONS.framed[0];
      $("off-text").textContent = REASONS.framed[1];
      $("power").hidden = true;
      $("off").hidden = false;
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", show); else show();
    return;
  }

  // VT323's cell is about 0.4em wide: this keeps 65 or more columns on the
  // narrow Coordinator screen and grows the type on a wide one.
  const fontSize = () => Math.max(15, Math.min(26, Math.floor(window.innerWidth / 30)));
  const term = new Terminal({
    scrollback: 5000,
    fontFamily: '"VT323", ui-monospace, Menlo, Consolas, monospace',
    fontSize: fontSize(),
    lineHeight: 1.0,
    fontWeight: "normal",
    fontWeightBold: "normal",  // a pixel font has no bold: bold shows as the bright color
    cursorBlink: true,
    allowProposedApi: false,
    allowTransparency: true,
    macOptionIsMeta: true,
    linkHandler: { activate() {}, allowNonHttpProtocols: false },
    theme: {
      background: "rgba(0, 0, 0, 0)", foreground: "#00ff41", cursor: "#b8ffca", cursorAccent: "#000500",
      selectionBackground: "rgba(0, 255, 65, 0.28)", selectionInactiveBackground: "rgba(0, 255, 65, 0.16)",
    },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);

  let ws = null, running = false, wanted = false, retry = 0, opened = false;
  // autoTried: the one automatic power-on per page load has been decided.
  // replayNext: the next binary frame is the server's replay of past output.
  // muted: that replay is being parsed. xterm.js answers the old terminal
  // queries in it (DA, DSR, DECRQSS, OSC 10/11); those answers are dropped, not
  // typed into the Coordinator.
  let autoTried = false, replayNext = false, muted = false;
  const enc = new TextEncoder();

  function show(reason) {
    const off = $("off");
    if (!reason) { off.hidden = true; return; }
    const [title, text] = REASONS[reason] || REASONS.exited;
    $("off-title").textContent = title;
    $("off-text").textContent = text;
    // Lost: it reconnects by itself. Service: the server holds power-on until the
    // restart or update ends, and brings the Coordinator back itself.
    $("power").hidden = reason === "lost" || reason === "service";
    $("power").disabled = false;
    off.classList.toggle("service", reason === "service");
    off.hidden = false;
  }
  function send(data) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (typeof data === "string") { ws.send(data); return; }
    for (let i = 0; i < data.length; i += CHUNK) ws.send(data.subarray(i, i + CHUNK));
  }
  // Every tab shares one PTY, so the last size sent wins: only a visible tab
  // sends its size, and a tab claims the size again when it is shown or focused.
  function sendSize() {
    if (!document.hidden) send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }
  function refit() {
    if (!opened) return;
    const size = fontSize();
    if (term.options.fontSize !== size) term.options.fontSize = size;
    try { fit.fit(); } catch (e) { /* hidden: no size yet */ }
  }

  function connect() {
    const sock = new WebSocket(`ws://${location.host}/term/ws`, [`factory-term.${key}`]);
    sock.binaryType = "arraybuffer";
    ws = sock;
    sock.onopen = () => {
      retry = 0;
      sendSize();
      if (wanted) { wanted = false; send(JSON.stringify({ type: "power", on: true })); }
    };
    sock.onmessage = ev => {
      if (typeof ev.data !== "string") {
        const bytes = new Uint8Array(ev.data);
        if (replayNext) {
          replayNext = false;
          muted = true;
          term.write(bytes, () => { muted = false; });
        } else {
          term.write(bytes);
        }
        return;
      }
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg) return;
      if (msg.type === "replay") { replayNext = true; return; }
      if (msg.type !== "state") return;
      if (msg.running && !running) { term.reset(); sendSize(); }
      running = !!msg.running;
      const elsewhere = msg.reason === "elsewhere" || msg.elsewhere === true;
      // During a restart or update the server decides whether the Coordinator
      // comes back (only if it was attached and the action succeeded): no
      // automatic power-on from this page then, or after.
      if (msg.reason === "service") autoTried = true;
      if (!autoTried) {
        // Opening the dashboard brings the Coordinator up, once per page load,
        // and only if it is off and not open in `factory chat`.
        autoTried = true;
        if (!running && !elsewhere) {
          send(JSON.stringify({ type: "power", on: true }));
          return;  // the server answers with the new state
        }
      }
      show(running ? null : (msg.reason || (elsewhere ? "elsewhere" : "exited")));
      if (running) term.focus();
    };
    sock.onclose = () => {
      if (ws !== sock) return;
      ws = null; running = false; replayNext = false; muted = false;
      show("lost");
      retry = Math.min(retry + 1, 10);
      setTimeout(connect, 500 * retry);
    };
  }

  // A hidden tab can't be typing: anything it would send is an automatic
  // reply to a query, which the visible tab already answers.
  const quiet = () => muted || document.hidden;
  term.onData(d => { if (!quiet()) send(enc.encode(d)); });
  term.onBinary(d => { if (quiet()) return; const b = new Uint8Array(d.length); for (let i = 0; i < d.length; i++) b[i] = d.charCodeAt(i) & 0xff; send(b); });
  term.onResize(() => sendSize());

  $("power").addEventListener("click", () => {
    $("power").disabled = true;
    if (ws && ws.readyState === WebSocket.OPEN) send(JSON.stringify({ type: "power", on: true }));
    else wanted = true;
  });
  // The dashboard focuses this frame when its screen is clicked: hand focus to the terminal.
  window.addEventListener("focus", () => { sendSize(); if ($("off").hidden) term.focus(); else $("power").focus(); });
  document.addEventListener("visibilitychange", sendSize);

  function start() {
    term.open($("term"));
    opened = true;
    refit();
    new ResizeObserver(refit).observe($("term"));
    connect();
  }
  // Measure the cell only once VT323 has loaded, or the grid is sized for the fallback font.
  const ready = document.fonts && document.fonts.load ? document.fonts.load(`${fontSize()}px "VT323"`) : Promise.resolve();
  ready.then(start, start);
})();
