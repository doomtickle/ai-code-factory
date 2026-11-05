// The Thoughtfield's typed line (doomtickle/passdesk-ops#207), on the terminal origin, framed by the main page, which can't
// reach it and never sees what is typed. A sky question stays in the browser: the main page gets one fixed word. Anything
// else goes to the Coordinator exactly as if typed into Terminal 0, only when you press Enter. It never powers anything on,
// never answers terminal queries, and ignores everything the terminal prints (the visible terminal is its own frame).
"use strict";
(function () {
  const meta = document.querySelector('meta[name="term-key"]'), portMeta = document.querySelector('meta[name="main-port"]'), key = meta && meta.content, mainPort = portMeta ? Number(portMeta.content) : 0;
  if (meta) meta.remove();
  if (window.top === window || !key || !Number.isInteger(mainPort) || mainPort < 1 || mainPort > 65535) return;
  const mainOrigin = `${location.protocol}//${location.hostname}:${mainPort}`, tell = word => window.parent.postMessage("ln:" + word, mainOrigin);
  const form = document.getElementById("line"), input = document.getElementById("line-in"), encoder = new TextEncoder();
  let ws = null, running = false, retry = 0;
  function connect() { const sock = new WebSocket(`ws://${location.host}/term/ws`, [`factory-term.${key}`]); sock.binaryType = "arraybuffer"; ws = sock; sock.onopen = () => { retry = 0; };
    sock.onmessage = ev => { if (typeof ev.data !== "string") return; let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; } if (msg && msg.type === "state") running = !!msg.running; };
    sock.onclose = () => { if (ws !== sock) return; running = false; retry = Math.min(retry + 1, 8); setTimeout(connect, 500 * retry); }; }
  connect();
  // Typed text only: no control character reaches the PTY from this line.
  const clean = s => Array.from(s).filter(c => { const n = c.codePointAt(0); return n >= 0x20 && n !== 0x7f && !(n >= 0x80 && n < 0xa0); }).join("");
  input.addEventListener("input", () => form.classList.toggle("has", !!input.value.trim()));
  // The frame is sandboxed without allow-forms, so Enter and the arrow key send it directly; the form never submits.
  const submit = () => { const q = clean(input.value).trim(); if (!q) return;
    const view = window.FieldAsk.isQuestion(q) ? window.FieldAsk.view(q) : null; if (view) { input.value = ""; form.classList.remove("has"); tell(view); return; }
    if (!running || !ws || ws.readyState !== WebSocket.OPEN) { tell("off"); return; }   // keep the text: nothing was sent
    input.value = ""; form.classList.remove("has"); ws.send(encoder.encode(q)); setTimeout(() => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(encoder.encode("\r")); }, 120); tell("sent"); };
  form.addEventListener("submit", e => e.preventDefault());
  input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
  document.getElementById("send").addEventListener("click", e => { e.preventDefault(); submit(); });
})();
