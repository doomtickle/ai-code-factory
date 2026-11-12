// The Thoughtfield's "What do you need from me?" pop-up: the last block of text the Coordinator posted with its show tool
// (state.shown from /api/state, doomtickle/passdesk-ops, owner decision 6 Nov 2026). window.FieldShown = { update(state), open(), close(), isOpen() }.
//
// The main page only displays. This sends nothing anywhere: no request, no message to the Coordinator or the terminal. The text is
// agent-influenced, so it is drawn with textContent and copied only by a click on COPY (the rule term.js follows for OSC 52): the
// text on screen and nothing else, never within HOLD_MS of the text changing, with navigator.clipboard.writeText, else a selected
// textarea and execCommand("copy"). The page never reads the clipboard. A selection the owner makes in the text is copied by the browser's
// own copy; the page leaves it alone (render() rewrites the text only when it changed).
"use strict";

(() => {
  const HOLD_MS = 2500;       // COPY stays dark this long after the text changes (term.js and the control unit hold the same)
  const OLD_S = 600;          // after this the pop-up shrinks and dims; it never closes by itself
  const EMPTY = "Nothing posted yet. Ask me in the terminal.";
  const FLAGGED = /⟨U\+[0-9A-F]{4,6}⟩/;
  const $ = id => document.getElementById(id);

  let primed = false, lastId = null, block = null, open = false, holdUntil = 0, holdTimer = 0, doneTimer = 0, now = 0;

  const holding = () => Date.now() < holdUntil;
  function ago(s) {
    if (!(s >= 0)) return "";
    if (s < 60) return "just now";
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} min ago`;
    const h = Math.floor(m / 60);
    return h < 48 ? `${h} h ago` : `${Math.floor(h / 24)} d ago`;
  }

  function render() {
    const box = $("shown"), text = block ? block.text : EMPTY;
    const pre = $("shown-text");
    if (pre.textContent !== text) pre.textContent = text;   // rewriting identical text on every poll would drop the owner's selection
    pre.classList.toggle("none", !block);
    const age = block && block.t != null && now ? Math.max(0, now - block.t) : -1;
    $("shown-age").textContent = block ? ago(age) : "";
    box.classList.toggle("old", age >= OLD_S);
    $("shown-warn").hidden = !(block && FLAGGED.test(text));
    $("shown-copy").disabled = !block || holding();
    box.hidden = !open;
    $("shown-key").setAttribute("aria-expanded", String(open));
  }

  function holdCopy() {
    holdUntil = Date.now() + HOLD_MS;
    clearTimeout(holdTimer);
    holdTimer = setTimeout(render, HOLD_MS + 20);
  }

  function copied(ok) {
    const b = $("shown-copy");
    b.textContent = ok ? "Copied" : "Press Ctrl+C";
    clearTimeout(doneTimer);
    doneTimer = setTimeout(() => { b.textContent = "Copy"; }, 2000);
  }

  // The browser refused writeText: copy from a selected textarea in this click, and if even that fails leave the text selected
  // for the user's own copy.
  function fallback(text) {
    const area = $("shown-manual");
    area.value = text;
    area.hidden = false;
    area.focus();
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    if (ok) { area.hidden = true; $("shown-copy").focus(); }
    copied(ok);
  }

  function copy() {
    if (!block || holding()) return;
    const text = $("shown-text").textContent, clip = navigator.clipboard;
    if (!clip || typeof clip.writeText !== "function") { fallback(text); return; }
    let p;
    try { p = clip.writeText(text); } catch (e) { fallback(text); return; }
    p.then(() => copied(true), () => fallback(text));
  }

  function setOpen(on) { open = on; render(); }

  function update(state) {
    const s = state || {}, b = s.shown && typeof s.shown.id === "string" && typeof s.shown.text === "string" && s.shown.text ? s.shown : null;
    now = typeof s.now === "number" ? s.now : 0;
    const id = b ? b.id : null;
    block = b;
    if (!primed) { primed = true; lastId = id; render(); return; }   // a block already there at load is not news
    if (id !== lastId) {
      lastId = id;
      if (b) { holdCopy(); open = true; }
    }
    render();
  }

  function init() {
    $("shown-key").addEventListener("click", () => setOpen(!open));
    $("shown-close").addEventListener("click", () => setOpen(false));
    $("shown-copy").addEventListener("click", copy);
    $("shown").addEventListener("keydown", e => { if (e.key === "Escape") { setOpen(false); $("shown-key").focus(); } });
    render();
  }

  window.FieldShown = { update, open: () => setOpen(true), close: () => setOpen(false), isOpen: () => open };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
