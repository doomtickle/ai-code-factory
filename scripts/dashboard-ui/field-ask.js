// Which shape of the sky a typed question asks for, or null. One copy for the main page's line (demo, no Terminal 0) and the
// terminal origin's line. Plain text in, a fixed word out; nothing typed goes further than this function.
"use strict";
window.FieldAsk = {
  isQuestion: q => /\?\s*$/.test(q) || /^(what|when|which|where|why|how|who|show|back)\b/i.test(q),
  view(q) { const t = String(q).toLowerCase();
    if (/\b(back|galaxy|home|reset)\b/.test(t)) return "home"; if (/cost|spen[dt]|expens|money|budget|price/.test(t)) return "spend"; if (/stuck|wrong|fail|problem|broken|trouble|attention/.test(t)) return "stuck";
    if (/\bwhen\b|busiest|busy|quiet|timeline|\bhours?\b/.test(t)) return "time"; if (/doing|kind of|actions|blocked|reading|\bwork(ing|ed)?\b/.test(t)) return "kinds"; return null; },
};
