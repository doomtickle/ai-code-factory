// Which shape of the sky a typed line asks for, or null. One copy for the main page's line (demo, no Terminal 0) and the
// terminal origin's line. Deliberately narrow: only a short plain question about the whole day, one sentence with no
// aside. Anything else (an order, a question about one job) is for the Coordinator, so nothing it asked for is ever lost
// to the sky. Plain text in, a fixed word out; nothing typed goes further than this function.
"use strict";
window.FieldAsk = {
  isQuestion: q => window.FieldAsk.view(q) !== null,
  view(q) { const t = String(q).trim().toLowerCase().replace(/[?]+$/, "").trim();
    if (/^(back|home|reset|go back|back to the galaxy|show the galaxy)$/.test(t)) return "home";
    if (/[,.;:!?]/.test(t) || t.split(/\s+/).length > 7) return null;
    if (!/^(what|what's|whats|which|where|how|when|show)\b/.test(t) || /\b(in|on|for|about|with|from|to)\b/.test(t)) return null;
    if (/\b(costs?|spend|spent|expensive|money|budget)\b/.test(t)) return "spend";
    if (/\b(stuck|wrong|failed|failing|broken|problems?|trouble|attention)\b/.test(t)) return "stuck";
    if (/\b(busiest|busy|quiet|timeline)\b/.test(t)) return "time";
    if (/\b(doing|actions|blocked|reading)\b|\bkind of\b/.test(t)) return "kinds";
    return null; },
};
