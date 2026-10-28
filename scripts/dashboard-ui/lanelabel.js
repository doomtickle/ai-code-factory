// The lane tags both views draw (doomtickle/passdesk-ops#194): the floor's console glass and the lane
// modules and chips. The words themselves (desk placard, branch, the lane a reviewer is checking) are
// derived once, server-side, in scripts/dashboard_labels.py; this is the one place the browser turns a
// lane name into its "DEV 2" tag. Plain text in, plain text out: callers set it with textContent.
"use strict";
(function (root) {
  const clip = (s, n) => (s.length > n ? s.slice(0, Math.max(1, n - 1)) + "…" : s);
  const devTag = name => {
    name = String(name || "");
    const m = /^developer(?:-lane(\d+))?$/.exec(name);
    return m ? `DEV ${m[1] || 1}` : clip(name.toUpperCase(), 8);
  };
  // "▸ DEV 2" for a reviewer's reviewed lane, "" when it has none.
  const reviewTag = reviews => (typeof reviews === "string" && reviews ? `▸ ${devTag(reviews)}` : "");
  const api = { devTag, reviewTag };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FactoryLabels = api;
})(typeof window !== "undefined" ? window : globalThis);
