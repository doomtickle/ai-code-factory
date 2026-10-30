// Terminal 0: the Coordinator's live terminal (xterm.js), on the dashboard's
// terminal origin. Binary WebSocket frames carry raw terminal bytes both ways;
// text frames carry small JSON control messages (resize, power, state).
// The terminal key arrives in a <meta> only this origin can read, goes out as
// the WebSocket subprotocol, and the <meta> is removed. No clipboard, link or
// image addons; OSC 8 links do nothing.
// Copying (doomtickle/passdesk-ops#142): an OSC 52 copy from the Coordinator
// never reaches the clipboard by itself, since this screen shows agent-written
// text. Our own OSC 52 handler is write-only (a query is refused and nothing
// here ever reads the clipboard), size-capped and decoded strictly; it raises a
// prompt with a text-only preview, and only a click on its COPY key writes the
// text. A selection (Shift-drag, or Option-drag on a Mac, past the Coordinator's
// mouse capture) is copied by the browser's own copy (Cmd+C, the menu) or by
// Ctrl+C / Ctrl+Shift+C / Ctrl+Insert elsewhere, with each line's trailing
// spaces trimmed; the selection is then cleared, so the next Ctrl+C interrupts.
// Attaching never starts the Coordinator: this page asks for it with a
// {"type":"power"} message, once on load (only if it is off and not open
// elsewhere) and whenever the power key is pressed. The RESUME key sends the
// same with "resume": true (the last session continued); nothing else does.
"use strict";

(() => {
  // ---------- copying: pure helpers (also loaded under node by test_dashboard_term.py)
  const COPY_MAX = 64 * 1024;                     // bytes of UTF-8 an OSC 52 copy may carry
  const B64_MAX = Math.ceil(COPY_MAX / 3) * 4;    // the same, as base64
  const PREVIEW_MAX = 240;                            // characters the prompt shows
  const B64 = /^[A-Za-z0-9+/]*={0,2}$/;
  // C0 and C1 controls other than tab and newline: a copy carrying one is dropped.
  // eslint-disable-next-line no-control-regex
  const CONTROL = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/;
  // Invisible and bidi characters: kept, but shown in the preview as ⟨U+XXXX⟩.
  const HIDDEN_CHARS = "\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb";
  const HIDDEN = new RegExp(`[${HIDDEN_CHARS}]`, "g"), HIDDEN_ONE = new RegExp(`[${HIDDEN_CHARS}]`);

  // The text an OSC 52 sequence asks to copy, or null to drop it. data is what
  // follows "52;": a selection target (ignored), ";", then base64 or "?". A query
  // ("?"), an empty payload (clear), invalid base64, invalid UTF-8, a control
  // character or more than COPY_MAX bytes gives null.
  function osc52Text(data) {
    if (typeof data !== "string") return null;
    const semi = data.indexOf(";");
    if (semi < 0) return null;
    let b64 = data.slice(semi + 1);
    if (!b64 || b64 === "?" || b64.length > B64_MAX || !B64.test(b64)) return null;
    if (b64.length % 4 === 1) return null;
    while (b64.length % 4) b64 += "=";
    let text;
    try {
      const bin = atob(b64);
      if (bin.length > COPY_MAX) return null;
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
    } catch (e) { return null; }
    text = text.replace(/\r\n?/g, "\n");
    if (!text || CONTROL.test(text)) return null;
    return text;
  }
  // An OSC 52 copy osc52Text drops only for its size: the bytes its base64 would
  // decode to (counted, not decoded), or 0 if it fits or is malformed anyway.
  function osc52Oversize(data) {
    if (typeof data !== "string") return 0;
    const b64 = data.slice(data.indexOf(";") + 1);
    if (data.indexOf(";") < 0 || b64.length % 4 === 1 || !B64.test(b64)) return 0;
    const pad = b64.endsWith("==") ? 2 : b64.endsWith("=") ? 1 : 0;
    const bytes = Math.floor((b64.length - pad) * 3 / 4);
    return bytes > COPY_MAX ? bytes : 0;
  }
  // What the prompt shows of it: hidden characters made visible, newlines as ⏎,
  // cut at PREVIEW_MAX. Drawn with textContent only.
  function copyPreview(text) {
    const s = String(text).replace(HIDDEN, c => `⟨U+${c.charCodeAt(0).toString(16).toUpperCase().padStart(4, "0")}⟩`)
      .replace(/\n/g, "⏎ ").replace(/\t/g, "  ");
    const chars = Array.from(s);
    return chars.length > PREVIEW_MAX ? chars.slice(0, PREVIEW_MAX - 1).join("") + "…" : s;
  }
  const hasHidden = text => HIDDEN_ONE.test(String(text));
  // A grid selection: the Coordinator pads its lines with spaces, so trim each line's end.
  const trimSelection = text => String(text).replace(/[ \u00a0]+$/gm, "");
  // The copy shortcut for a selection: Cmd+C on a Mac (also the browser's own
  // copy; Ctrl+C there is always the Coordinator's), Ctrl+C, Ctrl+Shift+C or
  // Ctrl+Insert elsewhere. Only asked while there is a selection: without one,
  // Ctrl+C goes to the Coordinator as an interrupt.
  function isCopyKey(ev, mac) {
    if (ev.type !== "keydown" || ev.altKey) return false;
    const c = ev.code === "KeyC" || ev.key === "c" || ev.key === "C";
    if (mac) return ev.metaKey && !ev.ctrlKey && !ev.shiftKey && c;
    if (ev.metaKey || !ev.ctrlKey) return false;
    return c || (!ev.shiftKey && (ev.key === "Insert" || ev.code === "Insert"));
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { osc52Text, osc52Oversize, copyPreview, hasHidden, trimSelection, isCopyKey, COPY_MAX, PREVIEW_MAX };
    return;
  }

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
    exited: ["TERMINAL 0 · POWERED OFF", "The Coordinator session ended. Power on starts a fresh session; Resume continues the last one."],
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
      $("resume").hidden = true;
      $("off").hidden = false;
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", show); else show();
    return;
  }

  // VT323's cell is about 0.4em wide: this keeps 65 or more columns on the
  // narrow Coordinator screen and grows the type on a wide one, up to 22px.
  const fontSize = () => Math.max(15, Math.min(22, Math.floor(window.innerWidth / 30)));
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
    // The Coordinator captures the mouse, so a plain drag selects nothing. Shift-drag
    // forces a selection on Windows and Linux; on a Mac it takes Option-drag and this.
    // (It only changes mouse handling: Option still types as Meta.)
    macOptionClickForcesSelection: true,
    linkHandler: { activate() {}, allowNonHttpProtocols: false },
    theme: {
      background: "rgba(0, 0, 0, 0)", foreground: "#00ff41", cursor: "#b8ffca", cursorAccent: "#000500",
      selectionBackground: "rgba(0, 255, 65, 0.28)", selectionInactiveBackground: "rgba(0, 255, 65, 0.16)",
    },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);

  let ws = null, running = false, wanted = null, retry = 0, opened = false;
  // autoTried: the one automatic power-on per page load has been decided.
  // replayNext: the next binary frame is the server's replay of past output.
  // muted: that replay is being parsed. xterm.js answers the old terminal
  // queries in it (DA, DSR, DECRQSS, OSC 10/11); those answers are dropped, not
  // typed into the Coordinator.
  let autoTried = false, replayNext = false, muted = false;
  const enc = new TextEncoder();

  // ---------- copying
  // One pending OSC 52 copy at a time: a newer one replaces it, DISMISS drops it,
  // and only a click on COPY writes it (navigator.clipboard.writeText, needing the
  // frame's allow="clipboard-write"; else the selected-textarea fallback). Each new
  // text holds COPY dark for HOLD_MS (as the control unit's keys), so a copy sent
  // just before a click can't be the one the click writes unread.
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
  const HOLD_MS = 2500;
  let pending = null, copyTimer = 0, holdUntil = 0, holdTimer = 0;
  const holding = () => Date.now() < holdUntil;
  function copyShow(state, text) {
    clearTimeout(copyTimer);
    const box = $("copy"), manual = $("copy-manual");
    if (!manual.hidden && document.activeElement === manual) term.focus();
    manual.hidden = true;
    manual.value = "";
    if (state === "none") { pending = null; box.hidden = true; return; }
    box.classList.toggle("done", state === "done");
    box.classList.toggle("large", state === "large");
    box.classList.toggle("fail", state === "fail");
    $("copy-go").hidden = state === "done" || state === "large";
    $("copy-x").hidden = state === "done";
    if (state === "offer") {
      pending = text;
      holdUntil = Date.now() + HOLD_MS;
      $("copy-go").disabled = true;
      clearTimeout(holdTimer);
      holdTimer = setTimeout(() => { $("copy-go").disabled = holding(); }, HOLD_MS + 20);
      const n = text.length;
      $("copy-title").textContent = `COORDINATOR COPIED ${n} CHARACTER${n === 1 ? "" : "S"} · PRESS TO COPY`;
      $("copy-preview").textContent = copyPreview(text);
      $("copy-note").textContent = hasHidden(text) ? "⚠ CONTAINS HIDDEN CHARACTERS (SHOWN AS ⟨U+…⟩)" : "";
      $("copy-note").hidden = !hasHidden(text);
    } else if (state === "done") {
      pending = null;
      $("copy-title").textContent = "COPIED TO THE CLIPBOARD";
      $("copy-note").hidden = true;
      copyTimer = setTimeout(() => copyShow("none"), 1500);
    } else if (state === "large") {
      // text: the bytes it would decode to. Nothing to copy, so no COPY key.
      pending = null;
      $("copy-preview").textContent = "";
      $("copy-title").textContent = `COORDINATOR COPIED ABOUT ${Math.ceil(text / 1024)} KiB · TOO LARGE TO COPY (LIMIT ${COPY_MAX / 1024} KiB)`;
      $("copy-note").hidden = true;
    }
    box.hidden = false;
  }
  // The browser refused writeText: copy from a selected textarea in this click,
  // and if even that fails leave it selected for the user's own Ctrl+C / Cmd+C.
  function copyFallback(text) {
    const area = $("copy-manual");
    area.value = text;
    area.hidden = false;
    area.focus();
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    if (ok) { copyShow("done"); term.focus(); return; }
    $("copy-title").textContent = `THE BROWSER BLOCKED THE COPY · PRESS ${mac ? "⌘C" : "CTRL+C"} NOW`;
    $("copy-note").hidden = true;
    $("copy").classList.add("fail");
  }
  $("copy-go").addEventListener("click", () => {
    const text = pending;
    if (text === null || holding()) return;
    const clip = navigator.clipboard;
    if (!clip || typeof clip.writeText !== "function") { copyFallback(text); return; }
    clip.writeText(text).then(() => { copyShow("done"); term.focus(); }, () => copyFallback(text));
  });
  $("copy-x").addEventListener("click", () => { copyShow("none"); term.focus(); });
  // Write-only, never during the muted replay (old output raises no prompt), and
  // always "handled" so no other handler sees it.
  term.parser.registerOscHandler(52, data => {
    if (muted) return true;
    const text = osc52Text(data);
    if (text !== null) { copyShow("offer", text); return true; }
    const size = osc52Oversize(data);
    if (size) copyShow("large", size);
    return true;
  });
  // A grid selection, by any copy the user starts (keys, the browser's menu):
  // trimmed, put on the copy event's own clipboard data. Capture phase, so it runs
  // before xterm.js's own untrimmed copy handler. The selection is then cleared, so
  // the next Ctrl+C reaches the Coordinator.
  document.addEventListener("copy", ev => {
    if (!term.hasSelection() || !ev.clipboardData || !$("term").contains(ev.target)) return;
    ev.clipboardData.setData("text/plain", trimSelection(term.getSelection()));
    ev.preventDefault();
    ev.stopImmediatePropagation();
    term.clearSelection();
  }, true);
  term.attachCustomKeyEventHandler(ev => {
    if (!term.hasSelection() || !isCopyKey(ev, mac)) return true;
    if (mac) return false;  // Cmd+C: xterm.js stays out; the browser's copy event runs the handler above
    ev.preventDefault();
    const text = trimSelection(term.getSelection());
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    if (!ok && navigator.clipboard && typeof navigator.clipboard.writeText === "function") navigator.clipboard.writeText(text).catch(() => {});
    term.clearSelection();
    return false;
  });

  function show(reason) {
    const off = $("off");
    if (!reason) { off.hidden = true; return; }
    const [title, text] = REASONS[reason] || REASONS.exited;
    $("off-title").textContent = title;
    $("off-text").textContent = text;
    // Lost: it reconnects by itself. Service: the server holds power-on until the
    // restart or update ends, and brings the Coordinator back itself.
    const noKeys = reason === "lost" || reason === "service";
    for (const id of ["power", "resume"]) { $(id).hidden = noKeys; $(id).disabled = false; }
    off.classList.toggle("service", reason === "service");
    off.hidden = false;
  }
  const powerMsg = resume => JSON.stringify(resume ? { type: "power", on: true, resume: true } : { type: "power", on: true });
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
      if (wanted) { send(powerMsg(wanted === "resume")); wanted = null; }
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
      if (msg.running && !running) { term.reset(); sendSize(); copyShow("none"); }
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

  function powerKey(id, resume) {
    $(id).addEventListener("click", () => {
      $("power").disabled = true;
      $("resume").disabled = true;
      if (ws && ws.readyState === WebSocket.OPEN) send(powerMsg(resume));
      else wanted = resume ? "resume" : "fresh";
    });
  }
  powerKey("power", false);
  powerKey("resume", true);
  // The dashboard focuses this frame when its screen is clicked: hand focus to the terminal.
  window.addEventListener("focus", () => { sendSize(); if ($("off").hidden) term.focus(); else if (!$("power").hidden) $("power").focus(); });
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
