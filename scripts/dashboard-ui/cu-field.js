// The gold light on the Thoughtfield's decision row (doomtickle/passdesk-ops#207). It adds no request and sees no key: a full
// 2.5 s hold clicks cu.js's own APPROVE key, and only while that key is enabled, so every rule of the unit still decides
// (the hold after a new card, a decided or expired card, the "without seeing N files" question). Everything else here is
// presentation. Runs only inside the dashboard: cu.js has already refused to run top-level.
"use strict";
(function () {
  if (window.top === window) return;
  const $ = id => document.getElementById(id), sun = $("sun"), ring = $("ring"), hint = $("hint"), approve = $("btn-approve"), decline = $("btn-decline"), root = document.documentElement, HOLD_MS = 2500;
  let t0 = null, raf = 0, base = "Hold to approve", note = "";
  const setHold = f => { ring.style.strokeDashoffset = 1 - f; sun.style.setProperty("--f", f); };
  function paint() { const label = $("lbl-approve").textContent || "", dark = approve.disabled; base = /confirm/i.test(label) ? "Hold again to approve anyway" : "Hold to approve"; sun.classList.toggle("dark", dark); sun.setAttribute("aria-disabled", String(dark)); if (t0 === null && !note) hint.textContent = dark && !$("keys-hold").hidden ? $("keys-hold").textContent : base; }
  function stopHold(msg) { t0 = null; cancelAnimationFrame(raf); setHold(0); note = msg || ""; if (note) hint.textContent = note; else paint(); }
  function start(e) { if (approve.disabled || t0 !== null) return; e.preventDefault(); note = ""; t0 = performance.now(); hint.textContent = "Keep holding…"; const step = () => { if (t0 === null) return; if (approve.disabled) { stopHold(); return; } const f = Math.min(1, (performance.now() - t0) / HOLD_MS); setHold(f); if (f < 1) raf = requestAnimationFrame(step); else { stopHold(); approve.click(); } }; step(); }
  const end = () => { if (t0 !== null) stopHold("Let go early. Nothing was sent."); };
  sun.addEventListener("pointerdown", start); sun.addEventListener("pointerup", end); sun.addEventListener("pointerleave", end); sun.addEventListener("pointercancel", end);
  sun.addEventListener("keydown", e => { if (e.key === " " || e.key === "Enter") { if (!e.repeat) start(e); else e.preventDefault(); } }); sun.addEventListener("keyup", e => { if (e.key === " " || e.key === "Enter") end(); });
  // Decline opens the reason in place of the card's details; cu.js still decides whether the press declines.
  decline.addEventListener("click", () => { if (!$("reason").value.trim()) { root.classList.add("why"); $("reason").focus(); } }, true);
  $("reason").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); decline.click(); } if (e.key === "Escape") { root.classList.remove("why"); $("reason").value = ""; } });
  const svc = open => { root.classList.toggle("svc", open); $("svc-more").setAttribute("aria-expanded", String(open)); $("svc-back").hidden = !open; };
  $("svc-more").addEventListener("click", () => svc(true)); $("svc-back").addEventListener("click", () => svc(false));
  new MutationObserver(paint).observe(approve, { attributes: true, attributeFilter: ["disabled"] });
  new MutationObserver(paint).observe($("lbl-approve"), { childList: true, characterData: true, subtree: true });
  new MutationObserver(paint).observe($("keys-hold"), { attributes: true, childList: true });
  let lastCards = root.dataset.cards;   // cu.js sets this on every render: act only when the count really changes
  new MutationObserver(() => { if (root.dataset.cards === lastCards) return; lastCards = root.dataset.cards; root.classList.remove("why"); note = ""; if (lastCards === "0") svc(false); paint(); }).observe(root, { attributes: true, attributeFilter: ["data-cards"] });
  paint();
})();
