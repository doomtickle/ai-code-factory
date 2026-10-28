// The dashboard's look (doomtickle/passdesk-ops#196): FACTORY_CONFIG/dashboard.toml, read once by the host at start and
// served as `look` in /api/state. The host has already validated it; this is the one place the browser checks it again
// before anything is drawn. Strings are only ever set with textContent, colours only as a validated #rrggbb through
// style.setProperty (the CSP has no inline style), never as a class name or HTML. Anything off reads as not set.
"use strict";
(function (root) {
  const HEX = /^#[0-9a-f]{6}$/, ROLE = /^[a-z][a-z0-9-]{1,30}$/, PRINTABLE = /^[\x20-\x7e]+$/;
  const text = (v, cap) => (typeof v === "string" && v.trim() && v.length <= cap && PRINTABLE.test(v) ? v : null);
  // {plant, shifts, crt, sound, colors} from state.look: plant a string or null, shifts three strings or null, crt a whole
  // number 0-10 or null, sound true/false or null, colors a Map of role name -> "#rrggbb".
  function read(raw) {
    const look = { plant: null, shifts: null, crt: null, sound: null, colors: new Map() };
    if (!raw || typeof raw !== "object") return look;
    look.plant = text(raw.plant, 24);
    if (Array.isArray(raw.shifts) && raw.shifts.length === 3) {
      const names = raw.shifts.map(s => text(s, 16));
      if (names.every(Boolean)) look.shifts = names;
    }
    if (Number.isInteger(raw.crt) && raw.crt >= 0 && raw.crt <= 10) look.crt = raw.crt;
    if (typeof raw.sound === "boolean") look.sound = raw.sound;
    if (raw.colors && typeof raw.colors === "object") {
      for (const role of Object.keys(raw.colors).slice(0, 32)) {
        const c = raw.colors[role];
        if (ROLE.test(role) && typeof c === "string" && HEX.test(c)) look.colors.set(role, c);
      }
    }
    return look;
  }
  // The soft halo the floor draws around a role's colour.
  const glow = hex => `rgba(${parseInt(hex.slice(1, 3), 16)}, ${parseInt(hex.slice(3, 5), 16)}, ${parseInt(hex.slice(5, 7), 16)}, .5)`;
  const api = { read, glow };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FactoryLook = api;
})(typeof window !== "undefined" ? window : globalThis);
