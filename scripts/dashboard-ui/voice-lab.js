// Tune LOIS's voice (doomtickle/passdesk-ops#207). Display and this browser's localStorage only: LiveVoice talks to the optional local service.
"use strict";
(function () {
  const $ = id => document.getElementById(id), V = window.LiveVoice;
  const FIELDS = [["speed", "Speed", .7, 1.8, .01], ["rate", "Pitch", .8, 1.2, .01], ["wet", "Reverb", 0, 1.5, .01], ["duck", "Clear words", 0, 1, .01], ["dry", "Direct voice", 0, 1.2, .01], ["decay", "Hall length (s)", 1, 8, .1]];
  const box = $("vl-sliders"), inputs = {};
  FIELDS.forEach(([k, name, min, max, step]) => { const label = document.createElement("label"), s = document.createElement("span"), i = document.createElement("input"), o = document.createElement("output"); s.textContent = name; i.type = "range"; i.min = min; i.max = max; i.step = step; const show = () => { o.textContent = Number(V.settings[k]).toFixed(2); i.value = V.settings[k]; }; i.addEventListener("input", () => { V.unlock(); V.set({ [k]: Number(i.value) }); show(); }); inputs[k] = show; show(); label.append(s, i, o); box.append(label); });
  const refresh = async () => { await V.check(); $("vl-status").textContent = V.available() ? "The local voice is running." : "The local voice isn't running. Start it with: factory voice start"; };
  refresh(); setInterval(refresh, 8000);
  $("vl-say").addEventListener("click", () => { V.unlock(); if (V.available()) V.say($("vl-text").value.slice(0, 1200)); });
  $("vl-stop").addEventListener("click", () => V.stop());
  $("vl-reset").addEventListener("click", () => { V.set(Object.assign({}, V.DEF)); Object.values(inputs).forEach(f => f()); });
})();
