// The gold light on the Thoughtfield's decision row and in Inspect (doomtickle/passdesk-ops#207, #208). It adds no request and
// sees no key: a full 2.5 s hold clicks cu.js's own APPROVE key, and only while that key is enabled, so every rule of the unit
// still decides (the hold after a new card, a decided or expired card, the "without seeing N files" question). A hold belongs to
// one card and one real press: the primary button or Space/Enter on the light itself. It ends the moment the card changes, the
// pointer leaves, the key is released, focus goes or the page is hidden. Runs only inside the dashboard: cu.js has already
// refused to run top-level.
"use strict";
(function () {
  if (window.top === window) return;
  const $ = id => document.getElementById(id), root = document.documentElement, HOLD_MS = 2500;
  const CHANGED = "The card changed. Nothing was sent.";
  const baseWords = label => (/confirm/i.test(label.textContent || "") ? "Hold again to approve anyway" : "Hold to approve");

  // o.ready: anything beyond APPROVE's own state that keeps this light dark. o.card: what the hold belongs to, if the page can tell. o.words: the idle hint.
  function light(o) {
    const { sun, ring, hint, approve, ready, card, words } = o;
    let t0 = null, raf = 0, note = "", heldCard = "", heldKey = "", pointerId = null;
    const setHold = f => { ring.style.strokeDashoffset = 1 - f; sun.style.setProperty("--f", f); };
    const canHold = () => !approve.disabled && ready();
    function paint() {
      const dark = !canHold();
      sun.classList.toggle("dark", dark);
      sun.setAttribute("aria-disabled", String(dark));
      if (t0 === null && !note) hint.textContent = words(dark);
    }
    function stopHold(msg) {
      const was = t0 !== null;
      t0 = null;
      cancelAnimationFrame(raf);
      setHold(0);
      if (pointerId !== null) { try { sun.releasePointerCapture(pointerId); } catch (e) {} pointerId = null; }
      heldKey = "";
      if (was && msg) { note = msg; hint.textContent = msg; }
      paint();
    }
    function start(e) {
      if (!canHold() || t0 !== null) return;
      e.preventDefault();
      note = "";
      heldCard = card();
      t0 = performance.now();
      hint.textContent = "Keep holding…";
      const step = () => {
        if (t0 === null) return;
        if (!canHold() || card() !== heldCard) { stopHold(CHANGED); return; }
        const f = Math.min(1, (performance.now() - t0) / HOLD_MS);
        setHold(f);
        if (f < 1) raf = requestAnimationFrame(step);
        else { stopHold(); approve.click(); }
      };
      step();
    }
    const early = () => stopHold("Let go early. Nothing was sent.");
    sun.addEventListener("pointerdown", e => {
      if (e.button !== 0 || !e.isPrimary) return;
      start(e);
      if (t0 !== null) { try { sun.setPointerCapture(e.pointerId); pointerId = e.pointerId; } catch (err) {} }
    });
    sun.addEventListener("contextmenu", e => e.preventDefault());
    sun.addEventListener("pointerup", early);
    sun.addEventListener("pointercancel", early);
    sun.addEventListener("lostpointercapture", () => { if (t0 !== null && !heldKey) early(); });
    sun.addEventListener("pointermove", e => {
      if (t0 === null || heldKey) return;
      const r = sun.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) early();
    });
    sun.addEventListener("keydown", e => {
      if (e.key !== " " && e.key !== "Enter") return;
      e.preventDefault();
      if (!e.repeat && t0 === null) { start(e); if (t0 !== null) heldKey = e.key; }
    });
    document.addEventListener("keyup", e => { if (t0 !== null && e.key === heldKey) early(); });
    sun.addEventListener("blur", () => { if (t0 !== null && heldKey) early(); });
    window.addEventListener("blur", () => { if (t0 !== null) early(); });
    document.addEventListener("visibilitychange", () => { if (document.hidden && t0 !== null) early(); });
    return {
      paint,
      // The card under this light changed: a hold ends, an old "let go early" goes.
      cardChanged() { if (t0 !== null) stopHold(CHANGED); else note = ""; paint(); },
    };
  }

  const watch = (node, opts, fn) => new MutationObserver(fn).observe(node, opts);
  const STATE = { attributes: true, attributeFilter: ["disabled"] }, TEXT = { childList: true, characterData: true, subtree: true };

  // ---------- the decision row
  const decline = $("btn-decline"), flash = $("flash");
  const deck = light({
    sun: $("sun"), ring: $("ring"), hint: $("hint"), approve: $("btn-approve"),
    // Dark unless the card on the glass is the one a hold would approve: never with the reason box up or a flash covering it.
    ready: () => !$("scr-deck").hidden && !root.classList.contains("why") && flash.hidden,
    card: () => root.dataset.sel || "",
    words: () => (!$("keys-hold").hidden ? $("keys-hold").textContent : !flash.hidden ? "One moment…" : root.classList.contains("why") ? "Say why, then decline" : baseWords($("lbl-approve"))),
  });
  // Decline opens the reason in place of the card's details; cu.js still decides whether the press declines. Not during its own question.
  const closeWhy = () => { root.classList.remove("why"); deck.paint(); };
  decline.addEventListener("click", () => {
    if (/^decline$/i.test(($("lbl-decline").textContent || "").trim()) && !$("reason").value.trim()) { root.classList.add("why"); deck.cardChanged(); $("reason").focus(); }
  }, true);
  $("reason").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); decline.click(); }
    if (e.key === "Escape") { $("reason").value = ""; closeWhy(); }
  });
  const svc = open => { root.classList.toggle("svc", open); $("svc-more").setAttribute("aria-expanded", String(open)); $("svc-back").hidden = !open; };
  $("svc-more").addEventListener("click", () => svc(true));
  $("svc-back").addEventListener("click", () => svc(false));
  watch($("btn-approve"), STATE, deck.paint);
  watch($("lbl-approve"), TEXT, deck.paint);
  watch($("keys-hold"), { attributes: true, childList: true }, deck.paint);
  watch(flash, { attributes: true, attributeFilter: ["hidden"] }, deck.paint);
  // cu.js sets these on every render: act only when they really change. A different card always drops a half-typed reason and a hold.
  let lastSel = root.dataset.sel, lastCards = root.dataset.cards;
  watch(root, { attributes: true, attributeFilter: ["data-cards", "data-sel"] }, () => {
    if (root.dataset.sel === lastSel && root.dataset.cards === lastCards) return;
    lastSel = root.dataset.sel;
    lastCards = root.dataset.cards;
    root.classList.remove("why");
    if (lastCards === "0") svc(false);
    deck.cardChanged();
  });
  deck.paint();

  // ---------- Inspect: the same light on the card and patch on the screen. cu.js puts nothing about either in an attribute here, but it
  // darkens APPROVE the moment a new card or patch arrives, before it draws them: so APPROVE going dark ends the hold, and the next hold starts clean.
  const viewer = light({
    sun: $("pv-sun"), ring: $("pv-ring"), hint: $("pv-hint"), approve: $("pv-approve"),
    ready: () => !$("scr-inspect").hidden,
    card: () => "",
    words: dark => (!$("pv-hold").hidden ? $("pv-hold").textContent : !dark ? baseWords($("pv-lbl-approve")) : /confirm/i.test($("pv-lbl-approve").textContent || "") ? "One moment…" : "Can't approve yet"),
  });
  watch($("pv-approve"), STATE, () => { if ($("pv-approve").disabled) viewer.cardChanged(); else viewer.paint(); });
  watch($("pv-lbl-approve"), TEXT, viewer.paint);
  watch($("pv-hold"), { attributes: true, childList: true }, viewer.paint);
  viewer.paint();
})();
