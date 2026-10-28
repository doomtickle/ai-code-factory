// The control unit (CU): the dashboard's only controls, on its isolated second
// origin (the terminal's), framed into the main page's "Write gate" module.
// The main page shows agent-written text, so it holds no action endpoint and no
// key: everything that acts lives here. This page's key arrives in a <meta> only
// this origin can read, goes out in the X-Factory-Control header of every POST,
// and the <meta> is removed. Server and agent strings go in with textContent,
// never as HTML and never as a URL.
// The only thing it tells the main page is one of three fixed strings over
// postMessage: "cu:expand" and "cu:collapse" (grow the frame to the whole window
// for the confirmation and log screens, and back) and "cu:reload" (after an
// update changed the dashboard's page). The one thing it hears back is
// {type: "cu:select", id}, from its parent at the main origin: which card to show.
// It is a display hint (see showCard) and decides nothing.
// INSPECT shows a held push's patch (/cu/api/patch): the server reads it from
// the broker's state, checks it against the card's digest, parses and sanitises
// it, so this page only draws lines with textContent; nothing from the patch
// goes into an attribute, a URL, a class name or a title.
// Starting a restart, an update or a diag takes two presses: the key asks the server
// what would happen (/cu/api/prepare, which runs nothing but `factory update
// --plan`), and only CONFIRM on that screen sends the single-use token back.
"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  };
  const str = v => (v == null ? "" : String(v));
  const pad = n => String(n).padStart(2, "0");
  const mmss = s => `${Math.floor(s / 60)}:${pad(s % 60)}`;
  const list = v => (Array.isArray(v) ? v : []);
  const setText = (n, t) => { t = str(t); if (n.textContent !== t) n.textContent = t; };
  const setClass = (n, c) => { if (n.className !== c) n.className = c; };

  const keyMeta = document.querySelector('meta[name="cu-key"]');
  const portMeta = document.querySelector('meta[name="cu-main-port"]');
  const key = keyMeta ? keyMeta.getAttribute("content") || "" : "";
  const mainPort = portMeta ? Number(portMeta.getAttribute("content")) : NaN;
  if (keyMeta) keyMeta.remove();
  if (portMeta) portMeta.remove();

  // Opened on its own (not framed by the dashboard), or served without its
  // settings: show why, and make no requests.
  if (window.top === window || !key || !Number.isInteger(mainPort) || mainPort < 1 || mainPort > 65535) {
    $("scr-alone").hidden = false;
    return;
  }
  const mainOrigin = `http://${location.hostname}:${mainPort}`;
  const tell = msg => window.parent.postMessage(msg, mainOrigin);

  const POLL_MS = 2000, LOG_MS = 500, LOG_CAP = 2 * 1024 * 1024;

  async function api(name, obj) {
    const r = await fetch(`/cu/api/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Factory-Control": key },
      body: JSON.stringify(obj || {}),
      credentials: "same-origin",
      cache: "no-store",
    });
    let data = null;
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!data || typeof data !== "object" || Array.isArray(data)) data = {};
    return { status: r.status, ok: r.ok, data };
  }
  const errText = res => (typeof res.data.error === "string" && res.data.error ? res.data.error : `refused (HTTP ${res.status})`);

  // ---------- screens
  // Each screen is a node and whether it needs the frame expanded to the whole
  // window. Switching to one that does tells the main page "cu:expand", and back
  // "cu:collapse". The setup screen shows the effective configuration read-only;
  // settings that move the security boundary (broker policy and hosts,
  // credentials) are never edited from a browser.
  const SCREENS = {
    deck: { node: $("scr-deck"), expanded: false },      // gate deck and service row (compact, in the rail)
    confirm: { node: $("scr-console"), expanded: true },  // what RESTART / UPDATE would do, and CONFIRM
    log: { node: $("scr-console"), expanded: true },      // the running action's output
    inspect: { node: $("scr-inspect"), expanded: true },  // a held push's patch, and the gate's keys
    setup: { node: $("scr-console"), expanded: true },    // the effective configuration (read-only)
  };
  let screen = null, expanded = false;
  function go(name) {
    const s = SCREENS[name];
    for (const k in SCREENS) if (SCREENS[k].node !== s.node) SCREENS[k].node.hidden = true;
    s.node.hidden = false;
    screen = name;
    if (s.expanded !== expanded) {
      expanded = s.expanded;
      document.documentElement.classList.toggle("expanded", expanded);
      tell(expanded ? "cu:expand" : "cu:collapse");
    }
  }

  // ---------- state
  let st = null, polledAt = 0, polling = false, fault = null;
  let selId = null, selFp = null, expectGone = null, busy = false;
  let shownKey = null, flashTimer = 0, offeredLog = false;
  // holdUntil: the decide keys stay dark until then after the card on the glass
  // changed under the user (a poll replaced it, or it left and the next one moved
  // in), so a click aimed at the old card can't land on one never seen.
  const HOLD_MS = 2500;
  let holdUntil = 0, holdTimer = 0;
  const holding = () => Date.now() < holdUntil;
  function holdKeys() {
    holdUntil = Date.now() + HOLD_MS;
    clearTimeout(holdTimer);
    holdTimer = setTimeout(updateKeys, HOLD_MS + 20);
  }
  const cards = () => (st ? list(st.cards).filter(c => c && typeof c === "object") : []);
  const current = () => cards().find(c => c.id === selId) || null;
  const act = () => (st && st.action && typeof st.action === "object" ? st.action : { name: null, phase: "idle" });
  const running = () => act().phase === "running";
  const secondsLeft = c => (c.seconds_left == null ? null : Math.max(0, Math.round(Number(c.seconds_left) - (Date.now() - polledAt) / 1000)));
  const expired = c => { const s = secondsLeft(c); return s != null && s <= 0; };
  // A decided card stays listed until the gateway acts on it (within seconds);
  // one decision per card, so its keys stay dark meanwhile.
  const VERDICTS = { approve: "approved", decline: "declined" };
  const decidedOf = c => (c && VERDICTS[c.decided] ? c.decided : null);
  const closed = c => !!c && (!!decidedOf(c) || expired(c));
  const clock = ts => {
    const n = Number(ts);
    if (ts == null || !Number.isFinite(n)) return "?";
    const d = new Date(n * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  // "Approved by sam at 14:02; the gateway makes the write within seconds" (d: the card, or a 409's body).
  // A card that isn't staged (an older gateway's) has nothing for the gateway to make;
  // a 409's body doesn't say, and every card the gateway makes now is staged.
  const waitText = d => (d.queued === false ? "not staged, so nothing is made from it" : d.decided === "approve" ? "the gateway makes the write within seconds" : "the gateway files it; nothing is written");
  const decidedText = d => `${VERDICTS[d.decided] === "approved" ? "Approved" : "Declined"} by ${str(d.decided_by) || "?"} at ${clock(d.decided_at)}; ${waitText(d)}`;
  // What the keys' strip says when they are dark for a reason other than a new card.
  function shutText(c) {
    const v = decidedOf(c);
    if (v === "approve") return c.queued ? "Approved · the gateway writes it" : "Approved · not staged, nothing to write";
    if (v === "decline") return "Declined";
    return expired(c) ? "Expired" : "";
  }

  async function poll() {
    if (polling || document.hidden) return;
    polling = true;
    try {
      const res = await api("state", {});
      if (res.ok) {
        st = res.data; polledAt = Date.now(); fault = null;
      } else {
        fault = res.status === 403
          ? ["REFUSED", "The dashboard refused this control unit (it may have restarted). Reload the dashboard page."]
          : ["FAULT", errText(res)];
      }
    } catch (e) {
      fault = ["NO LINK", "Lost the dashboard. Retrying…"];
    } finally {
      polling = false;
    }
    renderDeck();
  }

  // ---------- scroll cues: a fade and "▼ more" while a screen has more below
  function cue(scroller, node) {
    node.hidden = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 2;
  }
  function moreCue() {
    cue($("deck-body"), $("deck-more"));
    cue($("con-body"), $("con-more"));
  }
  $("deck-body").addEventListener("scroll", moreCue, { passive: true });
  $("con-body").addEventListener("scroll", moreCue, { passive: true });
  window.addEventListener("resize", moreCue);

  // ---------- the gate deck
  function flash(text, kind) {
    const f = $("flash");
    clearTimeout(flashTimer);
    f.className = `flash${kind === "err" ? " err" : ""}`;
    f.textContent = text;
    f.hidden = false;
    flashTimer = setTimeout(() => { f.hidden = true; }, kind === "err" ? 8000 : 5000);
  }
  function lanesLine(c) {
    const lanes = list(c.lanes).map(str);
    const role = str(c.role);
    if (lanes.length === 1) return `lane ${lanes[0]}`;
    if (lanes.length > 1) return `one of ${lanes.join(", ")} (the broker names the role, not the lane)`;
    return role ? `no running lane for ${role}` : "no running lane";
  }
  // Each line is one ellipsized row on the small glass; its full text is the tooltip.
  const line = (cls, text) => { const n = el("div", cls, text); n.title = n.textContent; return n; };
  function cardLines(c) {
    // Most telling first: the card's state if it can't be decided any more, what
    // and who, a PR's title, then where it comes from and goes.
    const out = [];
    const v = decidedOf(c);
    if (v) {
      out.push(line(`ln big ${v === "approve" ? "st-ok" : "st-no"}`, v === "approve" ? (c.queued ? "APPROVED, the gateway writes it within seconds" : "APPROVED, but not staged: nothing to write") : "DECLINED"));
      out.push(line("ln dim", `by ${str(c.decided_by) || "?"} at ${clock(c.decided_at)}`));
    } else if (expired(c)) {
      out.push(line("ln big st-no", "EXPIRED"), line("ln dim", "nothing is written; the agent's next call stages a fresh card"));
    }
    out.push(line("ln big", `${str(c.tool) || "write"} · ${str(c.role) || "?"}`));
    if (c.queued && !v && !expired(c)) out.push(line("ln warn", "STAGED: the agent has moved on; the gateway writes it when approved"));
    if (c.title) out.push(line("ln title", `“${str(c.title)}”${c.draft ? " (draft)" : ""}`));
    out.push(line("ln lane", lanesLine(c)));
    if (c.repo) out.push(line("ln", `→ ${str(c.repo)}`));
    if (c.branch || c.base) out.push(line("ln", c.base ? `${str(c.branch)} → ${str(c.base)}` : str(c.branch)));
    if (c.commits != null || c.file_count != null) {
      const n = v => (v == null ? "?" : str(v));
      out.push(line("ln", `${n(c.commits)} commit${c.commits === 1 ? "" : "s"} · ${n(c.file_count)} file${c.file_count === 1 ? "" : "s"}`));
    }
    const files = list(c.files).map(str);
    for (const f of files) out.push(line("ln files", f));
    if (Number(c.file_count) > files.length && files.length) out.push(line("ln files", `… and ${Number(c.file_count) - files.length} more`));
    out.push(line("ln dim", `id ${str(c.id)}`));
    return out;
  }

  // The gateway's recent deferred writes (display only; text from the state
  // files, already one clean line each, inserted as text). Failures first, loud.
  const WRITE_LABEL = { failed: "FAILED", running: "APPROVED · WRITING", made: "DONE" };
  const writes = () => (st ? list(st.writes).filter(w => w && typeof w === "object" && WRITE_LABEL[w.state]) : []);
  function writeLines() {
    const ws = writes();
    if (!ws.length) return [];
    const out = [line("ln dim", "the gateway's writes")];
    for (const w of ws) {
      const what = `${str(w.tool)}${w.target ? ` ${str(w.target)}` : ""}${w.repo ? ` → ${str(w.repo)}` : ""}`;
      out.push(line(`ln ${w.state === "failed" ? "big st-no" : w.state === "made" ? "st-ok" : ""}`, `${WRITE_LABEL[w.state]} ${what}`));
      if (w.state !== "running" && w.summary) out.push(line("ln files", str(w.summary)));
    }
    return out;
  }

  function renderDeck() {
    renderDeckOnly();
    if (screen === "inspect") syncInspect();
    if (screen === "setup") renderSetup();
  }
  function renderDeckOnly() {
    const body = $("deck-body");
    if (fault) {
      const k = `fault\u0000${fault.join("\u0000")}`;
      if (shownKey !== k) {
        shownKey = k;
        body.replaceChildren(el("div", "ln big fault", fault[0]), el("div", "ln", fault[1]));
      }
      setClass($("cu-led"), "led r blink");
      $("sel").hidden = true;
      updateKeys();
      return;
    }
    if (!st) return;
    const cs = cards();
    let c = current();
    if (selId != null && (!c || c.fp !== selFp)) {
      // The selected write left (or a new card took its id): never carry a typed
      // reason over to a different write.
      // (A write this unit just decided is expected to go: no warning for that.)
      if (selId === expectGone) expectGone = null;
      else flash(c ? "Changed: a new write replaced that card. Check it, then decide." : "That write is no longer held.", "err");
      $("reason").value = "";
    }
    if (!c && cs.length) c = cs[0];
    // Any card on the glass that the user didn't pick with ◀ ▶ (a replacement, the
    // next one after a decision, the first one to arrive): hold the keys a moment.
    if (c && (c.id !== selId || c.fp !== selFp)) {
      $("reason").value = "";
      holdKeys();
    }
    selId = c ? c.id : null;
    selFp = c ? c.fp : null;
    if (asking("deck") && (!c || c.id !== ask.id || c.fp !== ask.fp)) cancelAsk();

    // Blinking while a held write waits for a person; steady while all of them wait for the agent.
    setClass($("cu-led"), cs.some(x => !closed(x)) ? "led a blink" : cs.length ? "led a" : "led g");
    $("sel").hidden = cs.length < 2;
    if (cs.length > 1) setText($("sel-n"), `${cs.indexOf(c) + 1}/${cs.length}`);

    const k = JSON.stringify([c ? Object.assign({}, c, { seconds_left: c.seconds_left == null, expired: expired(c) }) : "empty", writes()]);
    if (k !== shownKey) {
      shownKey = k;
      body.replaceChildren(...(c ? cardLines(c) : [el("div", "ln idle", "No writes staged")]), ...writeLines());
      body.scrollTop = 0;
    }
    moreCue();
    if (running() && !offeredLog && screen === "deck") {
      offeredLog = true;
      flash(`${str(act().name).toUpperCase() || "AN ACTION"} IS RUNNING · press LOG to watch it`, "ok");
    }
    tick();
  }

  function updateKeys() {
    const c = fault ? null : current();
    const reason = $("reason");
    const held = !!c && holding();
    const shut = closed(c);
    const live = !!c && !busy && !held && !shut;
    reason.disabled = !c || shut;
    const strip = shut ? shutText(c) : held ? "Check the new card" : "";
    $("keys-hold").hidden = !strip;
    setText($("keys-hold"), strip);
    const deckAsk = asking("deck");
    // DECLINE takes a press without a reason too, to say that one is needed;
    // its lamp lights only when the press would decline.
    const canDecline = deckAsk ? !busy : live;
    const declineLit = deckAsk ? !busy : live && reason.value.trim() !== "";
    const canApprove = live && (!deckAsk || armed());
    setText($("lbl-decline"), deckAsk ? "Cancel" : "Decline");
    setText($("lbl-approve"), deckAsk ? "Confirm approve" : "Approve");
    $("btn-decline").disabled = !canDecline;
    $("btn-approve").disabled = !canApprove;
    setClass($("led-decline"), `led${declineLit ? " r" : ""}`);
    setClass($("led-approve"), `led${canApprove ? " g" : ""}`);
    if (declineLit || !live) reason.classList.remove("need");
    const inspectable = !fault && !!c && c.patch === true;
    $("insp-row").hidden = !inspectable;
    $("btn-inspect").disabled = !inspectable;
    setClass($("led-inspect"), `led${inspectable ? " g" : ""}`);
    const why = !c ? "" : decidedOf(c) ? decidedText(c) : expired(c) ? "This hold has expired" : "";
    $("btn-approve").title = why;
    $("btn-decline").title = why || (live && !declineLit && !deckAsk ? "Type a reason to decline" : "");

    const a = act(), run = !fault && running();
    const bound = !!(st && st.bound);
    $("svc-restart").disabled = !!fault || !st || run || !bound;
    $("svc-restart").title = st && !bound ? "No project is bound to the factory: start it once with factory start" : "";
    $("svc-update").disabled = !!fault || !st || run;
    $("svc-doctor").disabled = !!fault || !st || run;
    $("svc-setup").disabled = !!fault || !st;
    setClass($("led-doctor"), `led${run ? " a blink" : ""}`);
    const due = st && st.service;  // the header's SERVICE DUE lamp: light the key that applies it
    const first = !!(st && st.first_shift);  // FIRST SHIFT (#181): the first /login or RESTART is still to do
    setClass($("led-restart"), `led${run || first ? " a blink" : due === "restart" ? " a" : ""}`);
    setClass($("led-update"), `led${run ? " a blink" : due === "update" ? " a" : ""}`);
    const showLog = !fault && !!a.name && a.phase !== "idle";
    $("svc-log").hidden = !showLog;
    setClass($("led-log"), `led${run ? " a blink" : a.phase === "done" ? " g" : a.phase === "failed" ? " r" : ""}`);
    updateInspKeys();
  }

  // Once a second: the countdown, and the keys go dark when the hold expires
  // (the glass is redrawn once then, to say EXPIRED).
  let shownExpired = null;
  function tick() {
    const c = fault ? null : current();
    const s = c ? secondsLeft(c) : null;
    const exp = c ? `${c.id}\u0000${expired(c)}` : null;
    if (exp !== shownExpired) {
      shownExpired = exp;
      if (c && expired(c) && screen === "deck") { renderDeckOnly(); return; }
    }
    const box = $("cd-box");
    setText($("cd"), !c ? "--:--" : s == null ? "--:--" : s <= 0 ? "EXP" : mmss(s));
    setClass(box, `lcd cd-box${!c || s == null ? " off" : s < 60 ? " low" : ""}`);
    box.title = !c ? "No write held" : s == null ? "Expiry unknown" : s <= 0 ? "Expired" : `Expires in ${mmss(s)}`;
    updateKeys();
    if (screen === "confirm") tickConfirm();
  }

  function step(d) {
    const cs = cards();
    if (cs.length < 2) return;
    const i = Math.max(0, cs.findIndex(c => c.id === selId));
    const c = cs[(i + d + cs.length) % cs.length];
    $("reason").value = "";
    cancelAsk();
    selId = c.id; selFp = c.fp;
    renderDeck();
  }

  // The main page may ask which card to show: {type: "cu:select", id}, from the frame's parent
  // at the main origin and nothing else, with exactly those two fields and an id shaped like a
  // card id. It is a display hint. It acts on the deck only (not over a screen, an open question
  // or a decision in flight), picks among the cards this unit already holds, never touches a
  // decision key or focus, and treats the new card like any card that arrives unasked: the typed
  // reason is cleared and the keys go dark for HOLD_MS. At most one in SELECT_GAP_MS, so a page
  // can't keep the keys dark. The main page shows agent text, so it gets nothing else from here.
  const CARD_ID = /^[A-Za-z0-9_-]{1,64}$/;
  const SELECT_GAP_MS = 400;
  let lastSelectAt = 0;
  function showCard(id) {
    if (screen !== "deck" || fault || busy || asking("deck") || !st) return;
    const now = Date.now();
    if (now - lastSelectAt < SELECT_GAP_MS) return;
    lastSelectAt = now;
    const c = cards().find(x => x.id === id);
    if (!c) { flash("That write is no longer held.", "err"); return; }
    if (c.id === selId && c.fp === selFp) return;
    $("reason").value = "";
    selId = c.id; selFp = c.fp;
    holdKeys();
    renderDeck();
  }
  window.addEventListener("message", e => {
    if (e.source !== window.parent || e.origin !== mainOrigin) return;
    const d = e.data;
    if (!d || typeof d !== "object" || Array.isArray(d)) return;
    const names = Object.keys(d);
    if (names.length !== 2 || !names.includes("type") || !names.includes("id")) return;
    if (d.type !== "cu:select" || typeof d.id !== "string" || !CARD_ID.test(d.id)) return;
    showCard(d.id);
  });

  const WHY = {
    gone: "Gone: that write is no longer held (decided elsewhere, or the agent gave up).",
    changed: "Changed: a new write replaced that card. Check it, then decide again.",
    expired: "Expired: the hold ran out. The agent can push again.",
    ambiguous: "Ambiguous: two held writes claim this id. Nothing was decided; use factory approvals in a terminal.",
    reason: "DECLINE needs a reason for the agent: type one, then press DECLINE.",
  };
  // Every refusal as text: the server's reasons by name, anything else as it says.
  function refusalText(res) {
    const e = errText(res);
    if (res.status === 409 && e === "decided") return `Already decided: ${decidedText(res.data)}.`;
    if (res.status === 409 && WHY[e]) return WHY[e];
    return `Refused (${res.status}): ${e}`;
  }
  // APPROVE on a push whose patch was truncated (the card says so, or the viewer
  // got a truncated patch) asks once more, on the deck or in the viewer:
  // "Approve without seeing N files?", with CANCEL and CONFIRM APPROVE, and
  // CONFIRM stays dark for ARM_MS so a double press can't confirm. Only CONFIRM
  // sends the decision; the card changing or going cancels the question.
  const ARM_MS = 1000;
  let ask = null, askTimer = 0;  // { where: "deck" | "viewer", id, fp, armAt }
  const asking = where => !!ask && ask.where === where;
  const armed = () => !!ask && Date.now() >= ask.armAt;
  const needsAsk = (fromViewer, c) => c.patch_truncated === true
    || (fromViewer && !!insp && !!insp.data && insp.data.truncated === true);
  function askText(fromViewer) {
    if (fromViewer && insp && insp.data) {
      const n = list(insp.data.files_missing).length + Math.max(0, Number(insp.data.files_missing_more) || 0);
      if (n > 0) return `Approve without seeing ${n} file${n === 1 ? "" : "s"}?`;
    }
    return "Approve without seeing the rest of the push?";
  }
  function showAsk() {
    for (const [where, node] of [["deck", $("ask")], ["viewer", $("pv-ask")]]) {
      node.hidden = !asking(where);
      node.textContent = asking(where) ? `${askText(where === "viewer")} The patch was cut: CONFIRM APPROVE approves what you haven't seen too.` : "";
    }
  }
  function askApprove(fromViewer, c) {
    ask = { where: fromViewer ? "viewer" : "deck", id: c.id, fp: fromViewer ? insp.dataFp : c.fp, armAt: Date.now() + ARM_MS };
    clearTimeout(askTimer);
    askTimer = setTimeout(updateKeys, ARM_MS + 20);
    showAsk();
    updateKeys();
  }
  function cancelAsk() {
    if (!ask) return;
    ask = null;
    clearTimeout(askTimer);
    showAsk();
    updateKeys();
  }

  // From the deck (the card on the glass), or with fromViewer from INSPECT: then
  // the card the viewer shows, with the fingerprint of the patch it drew, so a
  // decision never lands on a card whose patch wasn't the one on the screen.
  async function decide(verdict, fromViewer) {
    const c = fromViewer ? inspCard() : current();
    if (!c || busy) return;
    if (fromViewer ? !inspLive() : holding()) return;
    const where = fromViewer ? "viewer" : "deck";
    if (verdict === "decline" && asking(where)) { cancelAsk(); return; }  // the key reads CANCEL
    if (verdict === "approve" && needsAsk(fromViewer, c)) {
      if (!asking(where)) { askApprove(fromViewer, c); return; }
      if (!armed() || ask.id !== c.id || ask.fp !== (fromViewer ? insp.dataFp : c.fp)) return;
      ask = null;
      clearTimeout(askTimer);
      showAsk();
    }
    const reasonEl = fromViewer ? $("pv-reason") : $("reason");
    const say = fromViewer ? inspSay : flash;
    const reason = reasonEl.value.trim();
    if (decidedOf(c)) { say(`Already decided: ${decidedText(c)}.`, "err"); return; }
    if (expired(c)) { say(WHY.expired, "err"); return; }
    if (verdict === "decline" && !reason) {
      reasonEl.classList.add("need");
      reasonEl.focus();
      say(WHY.reason, "err");
      return;
    }
    busy = true;
    updateKeys();
    try {
      const res = await api("decide", { id: c.id, fp: fromViewer ? insp.dataFp : c.fp, verdict, reason });
      if (res.ok) {
        expectGone = c.id;
        reasonEl.value = "";
        if (fromViewer) closeInspect();
        flash(`${verdict === "approve" ? "APPROVED" : "DECLINED"} · ${str(res.data.text)} · ${verdict === "approve" ? "the gateway makes the write within seconds" : "nothing is written"}`, "ok");
      } else {
        say(refusalText(res), "err");
      }
    } catch (e) {
      say("No link to the dashboard: nothing was decided.", "err");
    } finally {
      busy = false;
      await poll();
      updateKeys();
    }
  }

  $("sel-prev").addEventListener("click", () => step(-1));
  $("sel-next").addEventListener("click", () => step(1));
  $("deck-lcd").addEventListener("keydown", e => {
    if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); $("deck-body").scrollBy(0, e.key === "ArrowDown" ? 18 : -18); }
  });
  $("reason").addEventListener("input", updateKeys);
  $("reason").addEventListener("keydown", e => {
    // Enter in the tape is DECLINE (the reason is for declining); APPROVE is always a key press.
    if (e.key === "Enter") { e.preventDefault(); decide("decline"); }
  });
  $("btn-decline").addEventListener("click", () => decide("decline"));
  $("btn-approve").addEventListener("click", () => decide("approve"));

  // ---------- the service monitor (confirm and log screens)
  const conBody = $("con-body"), goKey = $("con-go"), cancelKey = $("con-cancel");
  let svc = null, svcSeq = 0, lastKey = null;
  const P = (cls, text) => el("p", cls, text);
  const H = (text, tag) => { const h = el("h5", null, text); if (tag) h.append(el("span", "tag", tag)); return h; };
  const RUNS = { restart: "factory restart", update: "factory update --yes", doctor: "factory doctor" };
  const TITLES = { restart: "Restart the factory", update: "Update the factory", doctor: "Diagnose the factory" };
  const NAMES = { restart: "restart", update: "update", doctor: "diag" };  // what the keys say
  const keyName = action => NAMES[action] || str(action);

  function head(action, tag, tagCls, led, note) {
    setText($("con-title"), `SERVICE · ${keyName(action).toUpperCase()}`);
    const t = $("con-state");
    setClass(t, `state-tag st-${tagCls}`);
    setText(t, tag);
    setClass($("con-led"), `led ${led}`);
    setText($("con-project"), st ? str(st.project) : "");
    setText($("con-note"), note || "");
  }
  function keys(cancelText, goText, goOn) {
    cancelKey.textContent = cancelText;
    goKey.hidden = !goText;
    if (goText) goKey.textContent = goText;
    goKey.disabled = !goOn;
  }

  function openService(action) {
    if (running() || (action === "restart" && !(st && st.bound))) return;
    lastKey = $(`svc-${action}`);
    const seq = ++svcSeq;
    svc = { action, seq, token: null, deadline: 0, state: "checking", onGo: null };
    go("confirm");
    head(action, "Check", "check", "a blink");
    keys("Cancel", `Confirm ${keyName(action)}`, false);
    conBody.replaceChildren(
      P("banner", TITLES[action]),
      P("cursor", action === "update"
        ? "▶ Fetching the factory's upstream and comparing the sandboxes with it (this can take up to a minute and a half)…"
        : action === "doctor" ? "▶ Getting ready…" : "▶ Checking what a restart would stop…"));
    conBody.scrollTop = 0;
    moreCue();
    cancelKey.focus();
    api("prepare", { action }).then(res => {
      if (!svc || svc.seq !== seq || screen !== "confirm") return;
      if (res.ok && typeof res.data.confirm === "string" && res.data.confirm) renderConfirm(res.data);
      else refused(errText(res), res.status === 409);
    }, () => {
      if (svc && svc.seq === seq && screen === "confirm") refused("Lost the dashboard: nothing was checked or run.", false);
    });
  }

  // ---------- the setup screen: the effective configuration, read-only (st.config)
  // Built on the host from the factory's own parsers (dashboard_setup.py); nothing here
  // can change a setting. Every string goes in with textContent.
  const APPLIES = { START: "factory start", UPDATE: "factory update" };
  const SOURCES = { REPO: "REPO", LOCAL: "LOCAL" };
  const num = n => (Number.isFinite(Number(n)) ? Number(n).toLocaleString("en-US") : "?");
  const span = (cls, text) => el("span", cls, text);
  const applyTag = a => span("k-cmd", `  → ${APPLIES[a] ? a : "?"}`);
  let setupKey = null;

  function row(label, value, ...tail) {
    const p = el("p", "ind");
    p.append(span(null, `  ${str(label).padEnd(19)} `), value === "" || value == null ? span("k-dim", "(empty)") : span(null, str(value)), ...tail);
    return p;
  }

  function roleBlock(r) {
    const out = [];
    const tags = [r.on ? "on" : "off", r.optional ? "optional" : null, r.override ? "local override" : null].filter(Boolean).join(" · ");
    out.push(H(str(r.name), tags));
    if (!r.on) out.push(P("k-dim", "  Switched off in enabled-roles: no sandbox, so none of this is applied."));
    for (const k of list(r.keys).filter(x => x && typeof x === "object")) {
      const local = k.source === "LOCAL";
      const tail = [span(local ? "k-warn" : "k-dim", `  ${SOURCES[k.source] || "?"}`)];
      if (local && str(k.repo) !== str(k.value)) tail.push(span("k-dim", ` (repo: ${str(k.repo) || "empty"})`));
      tail.push(applyTag(k.applies));
      out.push(row(k.key, k.value, ...tail));
    }
    for (const k of list(r.fixed).filter(x => x && typeof x === "object")) {
      const tail = [span("k-dim", "  FIXED"), applyTag(k.applies)];
      if (k.note) tail.push(span("k-dim", ` (${str(k.note)})`));
      out.push(row(k.key, k.value, ...tail));
    }
    for (const name of list(r.ignored)) out.push(P("ind k-err", `  ✕ ${str(name)} in the override isn't allowed: factory start refuses it`));
    if (r.instructions_chars != null) {
      out.push(P(`ind ${r.over ? "k-err" : "k-dim"}`, `  instructions + notes  ${num(r.instructions_chars)} of ${num(c().notes && c().notes.limit)} characters${r.over ? " · OVER: the bridge refuses this role's tasks" : ""}`));
    }
    return out;
  }
  const c = () => (st && st.config && typeof st.config === "object" ? st.config : null);

  function setupBlocks() {
    const cfg = c();
    if (!cfg) return [P("k-dim", "No configuration to show: this dashboard was started without the factory's config folder.")];
    const out = [P("k-dim", "Read-only. REPO is the committed roles/<name>/role.conf, LOCAL a value your override in the config folder puts on top of it, FIXED a value no override can change. → START is factory start, → UPDATE is factory update.")];
    for (const r of list(cfg.roles).filter(x => x && typeof x === "object")) out.push(...roleBlock(r));

    const en = cfg.enabled || {};
    out.push(H("Optional roles", "enabled-roles"));
    const optional = list(cfg.roles).filter(r => r && r.optional);
    if (!optional.length) out.push(P("k-dim", "  No role is optional."));
    for (const r of optional) out.push(row(r.name, r.on ? "ON" : "OFF", span("k-dim", `  ${en.file ? "enabled-roles file" : "no enabled-roles file"}`), applyTag(en.applies)));
    for (const n of list(en.unknown)) out.push(P("ind k-err", `  ✕ ${str(n)} is named in enabled-roles but is not a role: factory start refuses it`));

    const ap = cfg.approvals || {}, rp = cfg.repos || {}, gl = cfg.gitlab || {}, co = cfg.coordinator || {};
    out.push(H("Broker policy", cfg.local_policy ? "local policy.toml" : "repo policy.toml only"));
    const src = o => span(o.source === "LOCAL" ? "k-warn" : "k-dim", `  ${SOURCES[o.source] || "NOT SET"}`);
    const tools = list(ap.tools);
    out.push(row("held tools", tools.join(", "), src(ap), applyTag(ap.applies)));
    if (!tools.length) out.push(P("ind k-dim", "                      (nothing is held: the gateway makes every write at once)"));
    out.push(row("hold expires after", ap.expire_seconds ? `${str(ap.expire_seconds)} s` : "", span("k-dim", ap.expire_seconds ? `  ${SOURCES[ap.expire_source] || "?"}` : "  default one day"), applyTag(ap.applies)));
    const repos = list(rp.list);
    out.push(row("allowed repos", repos.join(", "), src(rp), applyTag(rp.applies)));
    if (!repos.length) out.push(P("ind k-dim", "                      (git tools are off until [git].repos names a repo)"));
    out.push(row("GitLab url", gl.url, src(gl), applyTag(gl.applies)));
    out.push(row("Coordinator tools", list(co.broker_tools).join(" "), span("k-dim", "  kits/coordinator/broker.conf"), applyTag(co.applies)));

    const nt = cfg.notes || {};
    out.push(H("Project notes", str(nt.project) || "no project"));
    const files = list(nt.files).filter(x => x && typeof x === "object");
    if (!files.length) out.push(P("k-dim", `  No notes file for ${str(nt.project) || "this project"} in the config folder's projects/.`));
    for (const f of files) {
      const tail = [span(f.over ? "k-err" : "k-dim", `  ${num(f.chars)} of ${num(nt.limit)} characters${f.over ? " · OVER" : ""}`)];
      if (f.read === false) tail.push(span("k-dim", " · role off, not read"));
      tail.push(applyTag(nt.applies));
      out.push(row(f.name, "", ...tail));
    }

    const lk = cfg.look && typeof cfg.look === "object" ? cfg.look : {};
    out.push(H("Dashboard look", "dashboard.toml"));
    if (lk.problem) out.push(P("ind k-err", `  ✕ ignored, the defaults show: ${str(lk.problem)}`), P("ind k-dim", "  Fix the file, then restart factory dashboard."));
    else out.push(P("k-dim", lk.file ? "  Read when factory dashboard started: the nameplate, shift names, defaults and role colours." : "  No dashboard.toml in the config folder: the defaults show."));
    return out;
  }

  function renderSetup() {
    const k = JSON.stringify(st ? st.config : null);
    if (k === setupKey) return;
    setupKey = k;
    const keep = conBody.scrollTop;
    conBody.replaceChildren(P("banner", "Setup · effective configuration"), ...setupBlocks());
    conBody.scrollTop = keep;
    moreCue();
  }

  function openSetup() {
    if (fault || !st) return;
    lastKey = $("svc-setup");
    svcSeq++;
    svc = null;
    go("setup");
    setText($("con-title"), "SETUP · CONFIGURATION");
    const t = $("con-state");
    setClass(t, "state-tag st-check");
    setText(t, "Read-only");
    setClass($("con-led"), "led g");
    setText($("con-project"), str(st.project));
    setText($("con-note"), "Edit the files, then run the command shown.");
    keys("Close", null, false);
    setupKey = null;
    renderSetup();
    conBody.scrollTop = 0;
    cancelKey.focus();
  }

  function renderConfirm(d) {
    const action = svc.action;
    svc.token = d.confirm;
    svc.deadline = Date.now() + Math.max(0, Number(d.expires_in) || 0) * 1000;
    svc.state = "ready";
    svc.onGo = runAction;
    const out = [P("banner", TITLES[action])];
    if (action === "doctor") {
      out.push(P(null, "▶ Diag runs the factory's doctor: it checks this machine, its tools, Docker, the sandboxes and the gateway, and prints the fix for each problem. It changes nothing."));
      if (d.project) out.push(P("ind k-dim", `  project: ${str(d.project)}`));
      out.push(P("ind k-cmd", `  runs exactly: ${RUNS[action]}`));
      out.push(P("ind k-dim", "  Terminal 0 stays attached. Running jobs and held writes carry on."));
      out.push(el("div", "gap"), P("k-dim", "Nothing has run yet. CONFIRM DIAG starts it; CANCEL or Esc backs out."));
      conBody.replaceChildren(...out);
      conBody.scrollTop = 0;
      moreCue();
      keys("Cancel", "Confirm diag", true);
      tickConfirm();
      return;
    }
    if (action === "update") {
      out.push(P(null, "▶ Update pulls the factory's code, rebuilds what changed, recreates the sandboxes listed below, then starts the factory again."));
    } else {
      out.push(P(null, "▶ Restart stops the agents, the broker and the dispatch bridge, then starts them again."));
    }
    if (d.project) out.push(P("ind k-dim", `  project: ${str(d.project)}`));
    out.push(P("ind k-cmd", `  runs exactly: ${RUNS[action]}`));
    out.push(P("ind k-dim", "  Terminal 0 hangs up while it runs."));

    const jobs = list(d.jobs);
    out.push(H("Running jobs"));
    if (jobs.length) {
      out.push(P("k-err", `✕ ${jobs.length} running job${jobs.length > 1 ? "s" : ""} will be cancelled:`));
      for (const j of jobs) out.push(P("ind k-err", `  ${str(j && j.lane)} · ${str(j && j.task) || "(no task text)"}`));
    } else {
      out.push(P("k-ok", "✓ No jobs are running."));
    }
    const held = list(d.held);
    out.push(H("Write gate"));
    if (held.length) {
      out.push(P("k-err", `✕ ${held.length} held write${held.length > 1 ? "s are" : " is"} dropped; the agent can try again afterwards:`));
      for (const h of held) out.push(P("ind k-err", `  ${str(h && h.text) || str(h && h.id)}`));
    } else {
      out.push(P("k-ok", "✓ No writes are held."));
    }
    const queued = list(d.queued);
    if (queued.length) {
      out.push(P("k-ok", `✓ ${queued.length} staged write${queued.length > 1 ? "s are" : " is"} kept; the gateway makes ${queued.length > 1 ? "each" : "it"} once approved:`));
      for (const h of queued) out.push(P("ind k-dim", `  ${str(h && h.text) || str(h && h.id)}`));
    }

    if (action === "update") {
      if (d.plan_error) out.push(H("Update check"), P("k-err", `✕ ${str(d.plan_error)}`));
      const commits = list(d.commits);
      out.push(H("Incoming commits"));
      if (commits.length) for (const c of commits) out.push(P("ind k-cmd", `  ${str(c && c.sha).slice(0, 9)} ${str(c && c.subject)}`));
      else out.push(P("k-dim", "  No new commits upstream."));
      const rec = list(d.recreate);
      out.push(H("Sandboxes recreated"));
      if (rec.length) for (const r of rec) out.push(P("ind k-warn", `✕ ${str(r && r.sandbox)}: loses ${str(r && r.loses)}`));
      else out.push(P("k-ok", "✓ No sandbox is out of date."));
      const maybe = list(d.maybe);
      if (maybe.length) {
        out.push(H("Maybe recreated", "heuristic"));
        out.push(P("k-dim", "  A guess from the files the incoming commits touch; the update decides for real."));
        for (const m of maybe) out.push(P("ind k-warn", `? ${str(m && m.sandbox)}: ${str(m && m.why)}`));
      }
      const notes = list(d.notes);
      if (notes.length) {
        out.push(H("Notes"));
        for (const n of notes) out.push(P("ind k-warn", `• ${str(n)}`));
      }
    }
    out.push(el("div", "gap"), P("k-dim", `Nothing has run yet. CONFIRM ${action.toUpperCase()} starts it; CANCEL or Esc backs out.`));
    conBody.replaceChildren(...out);
    conBody.scrollTop = 0;
    moreCue();
    keys("Cancel", `Confirm ${action}`, true);
    tickConfirm();
  }

  function tickConfirm() {
    if (!svc || svc.state !== "ready") return;
    const left = Math.max(0, Math.ceil((svc.deadline - Date.now()) / 1000));
    if (left > 0) {
      head(svc.action, "Confirm", "confirm", "a blink", `Confirm within ${mmss(left)}`);
      return;
    }
    svc.state = "expired";
    svc.token = null;
    head(svc.action, "Expired", "expired", "r", "The confirmation expired. Cancel and press the key again to start over.");
    keys("Cancel", "Expired · start over", false);
  }

  function refused(text, conflict) {
    svc.state = "refused";
    svc.token = null;
    head(svc.action, "Refused", "refused", "r");
    conBody.replaceChildren(P("banner", "Not started"), P("k-err", `✕ ${text}`));
    if (conflict && running()) {
      conBody.append(P("k-dim", "Another action is running: open its log to watch it."));
      svc.onGo = () => openLog(false);
      keys("Close", "Open log", true);
    } else {
      keys("Close", null, false);
    }
    cancelKey.focus();
  }

  async function runAction() {
    if (!svc || svc.state !== "ready" || !svc.token) return;
    const { action, token, seq } = svc;
    svc.state = "starting";
    svc.token = null;  // single use, whatever the answer
    keys("Cancel", `Confirm ${keyName(action)}`, false);
    let res;
    try {
      res = await api("run", { action, confirm: token });
    } catch (e) {
      if (svc && svc.seq === seq) refused("Lost the dashboard before it answered. Check the log key on the control unit before trying again.", false);
      return;
    }
    if (!svc || svc.seq !== seq) return;
    if (res.ok) {
      offeredLog = true;
      openLog(true, action);
      poll();
    } else if (res.status === 403) {
      refused("The confirmation is no longer valid (expired, already used, or replaced by a newer check). Cancel and start over.", false);
    } else {
      refused(errText(res), res.status === 409);
    }
  }

  // ---------- the log screen
  let logOffset = 0, logTimer = 0, logBusy = false, logText = null, logPre = null, logStatus = null, logName = null, logFor = null;
  function openLog(fresh, name) {
    const a = act();
    const forRun = fresh ? `new:${Date.now()}` : str(a.started);
    svc = { action: name || a.name || "action", seq: ++svcSeq, state: "log", onGo: null };
    logName = svc.action;
    go("log");
    if (forRun !== logFor || !logPre) {
      // A fresh log view: read the action's log from the start.
      logFor = forRun;
      logOffset = 0;
      logText = document.createTextNode("");
      logPre = el("pre", "log");
      logPre.append(logText);
      logStatus = el("div", "exit");
      conBody.replaceChildren(P("banner", `${str(logName)} · log`), logPre, logStatus);
    } else {
      conBody.replaceChildren(P("banner", `${str(logName)} · log`), logPre, logStatus);
    }
    renderLogHead(fresh ? "running" : a.phase, a.exit, a.reload);
    conBody.scrollTop = conBody.scrollHeight;
    moreCue();
    cancelKey.focus();
    clearTimeout(logTimer); logTimer = 0;
    pollLog();
  }

  function renderLogHead(phase, exit, reload) {
    const name = str(logName);
    if (phase === "done" || phase === "failed") {
      const ok = phase === "done";
      // The doctor exits 1 when it finds problems: that's its answer, not a failure.
      const found = name === "doctor" && exit === 1;
      const word = ok ? (name === "doctor" ? "NO PROBLEMS FOUND" : "FINISHED") : found ? "PROBLEMS FOUND" : "FAILED";
      head(name, ok ? "Done" : found ? "Problems" : "Failed", ok ? "done" : "failed", ok ? "g" : "r");
      logStatus.replaceChildren(P(ok ? "k-ok" : "k-err",
        `${ok ? "✓" : "✕"} ${keyName(name).toUpperCase()} ${word} · exit ${exit == null ? "unknown" : str(exit)}`));
      if (found) logStatus.append(P("k-dim", "Each problem has its fix indented under it. Fix them, then press DIAG again."));
      if (reload === "page") {
        logStatus.append(P("k-warn", "The dashboard's page changed. Press RELOAD to load the new one."));
        svc.onGo = () => tell("cu:reload");
        keys("Close", "Reload", true);
      } else {
        if (reload === "server") logStatus.append(P("k-warn", "The dashboard's own code changed: press Ctrl-C where it runs and start `factory dashboard` again."));
        keys("Close", null, false);
      }
      return;
    }
    head(name, "Running", "running", "g blink", "Closing this doesn't stop it: the LOG key brings it back.");
    logStatus.replaceChildren(P("k-dim cursor", ""));
    keys("Close", null, false);
  }

  async function pollLog() {
    if (screen !== "log" || logBusy) return;
    logBusy = true;
    clearTimeout(logTimer); logTimer = 0;
    let again = LOG_MS;
    try {
      const res = await api("log", { offset: logOffset });
      if (screen !== "log") return;
      if (!res.ok) {
        setText($("con-note"), `Log: ${errText(res)}. Retrying…`);
        again = 2000;
      } else {
        const d = res.data;
        const prev = logOffset;
        const next = Number.isFinite(d.offset) ? d.offset : prev;
        // The server answers in byte offsets, up to 64 KiB a time: a big step means
        // there is more to read straight away.
        const more = next - prev >= 32 * 1024;
        const text = next < prev ? "" : str(d.text);
        if (next < prev) {
          // A different (newer, or shorter) log than the one shown, e.g. offset 0
          // because ours is past its end: start the view over and read it from 0.
          logText.data = "";
          logOffset = 0;
          logTimer = setTimeout(pollLog, 0);
          return;
        }
        if (text) {
          const follow = conBody.scrollHeight - conBody.scrollTop - conBody.clientHeight < 60;
          logText.appendData(text);
          if (logText.length > LOG_CAP) logText.deleteData(0, logText.length - Math.floor(LOG_CAP * 0.75));
          if (follow) conBody.scrollTop = conBody.scrollHeight;
        }
        logOffset = next;
        const phase = str(d.phase);
        if (phase === "done" || phase === "failed") {
          renderLogHead(phase, d.exit, str(d.reload));
          again = more ? 0 : null;  // finished: read to the end, then stop
        } else {
          renderLogHead("running");
          if (more) again = 0;
        }
        moreCue();
      }
    } catch (e) {
      if (screen === "log") setText($("con-note"), "Lost the dashboard. Retrying… (the action keeps running on the host)");
      again = 2000;
    } finally {
      logBusy = false;
    }
    if (again != null && screen === "log") logTimer = setTimeout(pollLog, again);
  }

  // ---------- the inspector: a held push's patch on the green screen
  const pvList = $("pv-list"), pvMain = $("pv-main"), pvReason = $("pv-reason");
  // Fixed strings only: a flag or status the page doesn't know is not shown,
  // and nothing the server sends becomes a class name.
  const FLAGS = {
    bidi: ["BIDI", "bidirectional control characters (they can make code read differently than it runs), shown as ⟨U+…⟩"],
    invisible: ["INVIS", "invisible characters (zero-width, BOM, tags), shown as ⟨U+…⟩"],
    control: ["CTRL", "control characters, shown as ⟨U+…⟩"],
    longline: ["LONG", "lines over 2000 characters, cut"],
    crlf: ["CRLF", "Windows (CRLF) line ends"],
    malformed: ["BAD", "a part of this diff could not be read and is not shown"],
  };
  const STATUS = { M: "modified", A: "added", D: "deleted", R: "renamed", B: "binary" };
  const KIND = { "+": "pv-ln add", "-": "pv-ln del", " ": "pv-ln ctx", "\\": "pv-ln note", "✂": "pv-ln cut" };
  let insp = null, inspSeq = 0;
  // insp: { id, fp (the card's, as last polled), dataFp (the one the drawn patch was
  // read for), seq, data, files: [{c, f}], sel, hunk, hunkEls, rows, holdUntil }

  const inspCard = () => (insp ? cards().find(c => c.id === insp.id) || null : null);
  const inspHolding = () => !!insp && Date.now() < insp.holdUntil;
  function inspLive() {
    const c = inspCard();
    return !!c && !fault && !busy && !!insp.data && insp.dataFp === c.fp && !inspHolding() && !closed(c);
  }
  function inspSay(text, kind) {
    const n = $("pv-note");
    n.textContent = str(text);
    n.classList.toggle("err", kind === "err");
  }
  function inspHead(tag, tagCls, led) {
    const t = $("pv-state");
    setClass(t, `state-tag st-${tagCls}`);
    setText(t, tag);
    setClass($("pv-led"), `led ${led}`);
    const c = inspCard();
    setText($("pv-where"), c ? `${str(c.repo)} · ${str(c.branch)}` : "");
  }
  function updateInspKeys() {
    if (screen !== "inspect" || !insp) return;
    const c = inspCard();
    const live = inspLive();
    const held = !!c && inspHolding();
    const shut = closed(c);
    const strip = shut ? shutText(c) : held ? "Check the new card" : "";
    $("pv-hold").hidden = !strip;
    setText($("pv-hold"), strip);
    pvReason.disabled = !c || shut;
    const vAsk = asking("viewer");
    const canDecline = vAsk ? !busy : live;
    const declineLit = vAsk ? !busy : live && pvReason.value.trim() !== "";
    const canApprove = live && (!vAsk || armed());
    setText($("pv-lbl-decline"), vAsk ? "Cancel" : "Decline");
    setText($("pv-lbl-approve"), vAsk ? "Confirm approve" : "Approve");
    $("pv-decline").disabled = !canDecline;
    $("pv-approve").disabled = !canApprove;
    setClass($("pv-led-decline"), `led${declineLit ? " r" : ""}`);
    setClass($("pv-led-approve"), `led${canApprove ? " g" : ""}`);
    if (declineLit || !live) pvReason.classList.remove("need");
  }

  function openInspect() {
    const c = current();
    if (!c || c.patch !== true || fault) return;
    cancelAsk();
    lastKey = $("btn-inspect");
    insp = { id: c.id, fp: c.fp, dataFp: null, seq: ++inspSeq, data: null, files: [], sel: -1, hunk: 0,
             hunkEls: [], rows: [], holdUntil: 0 };
    pvReason.value = "";
    go("inspect");
    loadPatch();
    pvList.focus();
  }

  function loadPatch() {
    const seq = ++inspSeq, fp = insp.fp;
    insp.seq = seq;
    insp.data = null;
    inspHead("Reading", "check", "a blink");
    $("pv-trunc").hidden = true;
    $("pv-missing").hidden = true;
    pvList.replaceChildren();
    pvMain.replaceChildren(el("p", "pv-msg cursor", "▶ Reading the patch the gateway stored for this push…"));
    updateInspKeys();
    api("patch", { id: insp.id, fp }).then(res => {
      if (!insp || insp.seq !== seq) return;
      if (res.ok && Array.isArray(res.data.commits)) {
        insp.data = res.data;
        insp.dataFp = fp;
        renderPatch();
      } else if (res.status === 409 && errText(res) === "gone") {
        closeInspect(WHY.gone);
      } else {
        inspHead("Error", "failed", "r");
        pvMain.replaceChildren(el("p", "pv-msg k-err", `✕ ${refusalText(res)}`),
          el("p", "pv-msg k-dim", "Nothing is shown, so nothing can be decided from here. Close, and decide from the deck if you must."));
      }
      updateInspKeys();
    }, () => {
      if (!insp || insp.seq !== seq) return;
      inspHead("No link", "failed", "r");
      pvMain.replaceChildren(el("p", "pv-msg k-err", "✕ Lost the dashboard: the patch was not read."));
    });
  }

  function badges(flags, into) {
    for (const f of list(flags)) {
      const b = FLAGS[str(f)];
      if (b) into.append(el("span", "pv-badge", b[0]));
    }
  }

  function renderPatch() {
    const d = insp.data;
    const total = Number.isInteger(d.files_total) ? d.files_total : null;
    const shown = Number.isInteger(d.files_shown) ? d.files_shown : 0;
    const tr = $("pv-trunc");
    tr.hidden = d.truncated !== true;
    tr.textContent = `TRUNCATED · showing ${shown} of ${total == null ? "?" : total} files`;
    // What the cut hides, by name (the card's own file list), so it can't be missed.
    const missing = list(d.files_missing).map(str);
    const more = Math.max(0, Number(d.files_missing_more) || 0);
    const ms = $("pv-missing");
    ms.hidden = d.truncated !== true;
    ms.textContent = d.truncated !== true ? "" : missing.length || more
      ? `NOT SHOWN: ${missing.join(", ")}${missing.length && more ? " … " : ""}${more ? `and ${more} more file${more === 1 ? "" : "s"}` : ""}`
      : "NOT SHOWN: the rest of the push (the cut fell inside a file shown here)";
    inspHead(d.truncated === true ? "Truncated" : "Held", d.truncated === true ? "confirm" : "running", "a blink");
    insp.files = [];
    insp.rows = [];
    const out = [];
    list(d.commits).forEach(c => {
      if (!c || typeof c !== "object") return;
      const first = str(c.message).split("\n")[0];
      const h = el("div", "pv-commit", `${str(c.sha).slice(0, 9) || "(no commit)"}${c.merge === true ? " · merge" : ""}`);
      h.append(el("span", "pv-subj", ` ${first}`));
      badges(c.flags, h);
      out.push(h);
      for (const f of list(c.files)) {
        if (!f || typeof f !== "object") continue;
        const k = insp.files.length;
        const row = el("div", "pv-file");
        row.setAttribute("role", "button");
        row.append(el("span", "pv-st", STATUS[str(f.status)] ? str(f.status) : "?"),
          el("span", "pv-path", str(f.path)),
          el("span", "pv-add", `+${Number(f.adds) || 0}`), el("span", "pv-del", `−${Number(f.dels) || 0}`));
        badges(f.flags, row);
        row.addEventListener("click", () => selectFile(k, 0));
        insp.files.push({ c, f });
        insp.rows.push(row);
        out.push(row);
      }
    });
    if (!insp.files.length) out.push(el("p", "pv-msg k-dim", "No files in this patch."));
    if (d.truncated === true) out.push(el("div", "pv-cut", "✂ CUT HERE · the rest of this push is not shown"));
    pvList.replaceChildren(...out);
    pvList.scrollTop = 0;
    if (insp.files.length) selectFile(0, 0);
    else pvMain.replaceChildren(...commitBlock(list(d.commits)[0]));
  }

  function commitBlock(c) {
    if (!c || typeof c !== "object") return [el("p", "pv-msg k-dim", "Nothing to show.")];
    const out = [el("p", "pv-meta", `commit ${str(c.sha)}${c.merge === true ? " (merge)" : ""}`)];
    if (c.author) out.push(el("p", "pv-meta", `Author: ${str(c.author)}`));
    if (c.date) out.push(el("p", "pv-meta", `Date:   ${str(c.date)}`));
    out.push(el("pre", "pv-msgtext", str(c.message)));
    return out;
  }

  function selectFile(k, hunk) {
    if (!insp || k < 0 || k >= insp.files.length) return;
    const { c, f } = insp.files[k];
    if (insp.sel >= 0 && insp.rows[insp.sel]) insp.rows[insp.sel].classList.remove("sel");
    insp.sel = k;
    insp.rows[k].classList.add("sel");
    insp.rows[k].scrollIntoView({ block: "nearest" });
    const out = commitBlock(c);
    const st = STATUS[str(f.status)] || "changed";
    const title = el("p", "pv-title", `${st.toUpperCase()} ${str(f.path)}`);
    out.push(title);
    if (f.old_path != null) out.push(el("p", "pv-meta", `from ${str(f.old_path)}`));
    if (c.merge === true) out.push(el("p", "pv-warn", "merge: only changes against every parent are shown"));
    out.push(el("p", "pv-meta", `+${Number(f.adds) || 0} −${Number(f.dels) || 0}`));
    for (const fl of list(f.flags)) {
      const b = FLAGS[str(fl)];
      if (b) {
        const p = el("p", "pv-warn");
        p.append(el("span", "pv-badge", b[0]), document.createTextNode(` ${b[1]}`));
        out.push(p);
      }
    }
    const hunks = list(f.hunks);
    insp.hunkEls = [];
    if (str(f.status) === "B") out.push(el("p", "pv-msg k-dim", "Binary file: its contents are not shown."));
    else if (!hunks.length) out.push(el("p", "pv-msg k-dim", "No content changes shown (a rename or mode change, an empty file, or cut by the cap)."));
    for (const h of hunks) {
      if (!h || typeof h !== "object") continue;
      const box = el("div", "pv-hunk");
      if (h.header) box.append(el("div", "pv-hh", str(h.header)));
      for (const ln of list(h.lines)) {
        if (!Array.isArray(ln)) continue;
        const kind = str(ln[0]);
        const cls = KIND[kind] || "pv-ln ctx";
        const prefix = typeof ln[2] === "string" ? ln[2] : kind === "\\" ? "\\ " : kind === "✂" ? "✂ " : kind;
        box.append(el("div", cls, `${prefix}${str(ln[1])}`));
      }
      insp.hunkEls.push(box);
      out.push(box);
    }
    pvMain.replaceChildren(...out);
    insp.hunk = Math.max(0, Math.min(hunk === -1 ? insp.hunkEls.length - 1 : hunk, insp.hunkEls.length - 1));
    if (hunk === 0 || !insp.hunkEls.length) pvMain.scrollTop = 0;
    else showHunk();
  }
  function showHunk() {
    const h = insp.hunkEls[insp.hunk];
    if (h) pvMain.scrollTop = Math.max(0, h.offsetTop - 8);
  }
  function stepFile(d) {
    if (!insp || !insp.files.length) return;
    selectFile(Math.max(0, Math.min(insp.files.length - 1, insp.sel + d)), 0);
  }
  function stepHunk(d) {
    if (!insp || !insp.files.length) return;
    const next = insp.hunk + d;
    if (insp.hunkEls.length && next >= 0 && next < insp.hunkEls.length) {
      insp.hunk = next;
      showHunk();
      return;
    }
    // Past the file's first or last hunk: on to the next file that has hunks.
    for (let k = insp.sel + d; k >= 0 && k < insp.files.length; k += d) {
      if (list(insp.files[k].f.hunks).length) { selectFile(k, d > 0 ? 0 : -1); if (d > 0) showHunk(); return; }
    }
  }

  // Each poll: the card the viewer shows may have gone (decided elsewhere, given up:
  // close, with a note) or been replaced under the same id (a new fingerprint: read
  // the new card's patch and hold the keys, as the deck does).
  function syncInspect() {
    if (!insp || fault) { updateInspKeys(); return; }
    const c = inspCard();
    if (!c) {
      const quiet = expectGone === insp.id;
      closeInspect(quiet ? null : "That write is no longer held: the viewer closed.");
      return;
    }
    if (c.fp !== insp.fp) {
      if (asking("viewer")) cancelAsk();
      insp.fp = c.fp;
      insp.holdUntil = Date.now() + HOLD_MS;
      setTimeout(updateInspKeys, HOLD_MS + 20);
      pvReason.value = "";
      loadPatch();
      inspSay(WHY.changed, "err");
    }
    updateInspKeys();
  }

  function closeInspect(note) {
    if (screen !== "inspect") return;
    if (asking("viewer")) cancelAsk();
    inspSeq++;   // a pending patch answer is dropped
    insp = null;
    pvList.replaceChildren();
    pvMain.replaceChildren();
    inspSay("", null);
    go("deck");
    renderDeckOnly();
    if (note) flash(note, "err");
    $("deck-lcd").focus();  // never onto APPROVE
  }
  $("btn-inspect").addEventListener("click", openInspect);
  $("pv-close").addEventListener("click", () => closeInspect());
  $("pv-decline").addEventListener("click", () => decide("decline", true));
  $("pv-approve").addEventListener("click", () => decide("approve", true));
  pvReason.addEventListener("input", updateInspKeys);
  pvReason.addEventListener("keydown", e => {
    // As on the deck: Enter in the tape is DECLINE, when DECLINE could be pressed.
    if (e.key === "Enter") { e.preventDefault(); if (!$("pv-decline").disabled) decide("decline", true); }
  });

  // ---------- leaving the monitor
  function closeConsole() {
    if (screen !== "confirm" && screen !== "log" && screen !== "setup") return;
    svcSeq++;           // a pending prepare or run answer is dropped
    svc = null;
    clearTimeout(logTimer); logTimer = 0;
    go("deck");
    renderDeck();
    setupKey = null;
    const back = lastKey && !lastKey.disabled && !lastKey.hidden ? lastKey : $("btn-approve").disabled ? $("deck-lcd") : $("btn-approve");
    back.focus();
  }
  cancelKey.addEventListener("click", closeConsole);
  goKey.addEventListener("click", () => { if (svc && svc.onGo && !goKey.disabled) svc.onGo(); });
  $("svc-restart").addEventListener("click", () => openService("restart"));
  $("svc-update").addEventListener("click", () => openService("update"));
  $("svc-doctor").addEventListener("click", () => openService("doctor"));
  $("svc-setup").addEventListener("click", openSetup);
  $("svc-log").addEventListener("click", () => { lastKey = $("svc-log"); openLog(false); });

  // The monitor and the inspector are modal: Esc backs out, Tab stays inside.
  // In the inspector, j/k or ↓/↑ move between files and n/p between hunks
  // (not while typing a reason).
  document.addEventListener("keydown", e => {
    if (screen === "inspect") {
      if (e.key === "Escape") { e.preventDefault(); closeInspect(); return; }
      if (e.key === "Tab") {
        const items = [pvList, pvMain, pvReason, $("pv-close"), $("pv-decline"), $("pv-approve")].filter(n => !n.hidden && !n.disabled);
        const first = items[0], last = items[items.length - 1];
        const inside = $("scr-inspect").contains(document.activeElement);
        if (e.shiftKey && (document.activeElement === first || !inside)) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && (document.activeElement === last || !inside)) { e.preventDefault(); first.focus(); }
        return;
      }
      if (e.target === pvReason || e.ctrlKey || e.metaKey || e.altKey) return;
      const move = { j: () => stepFile(1), ArrowDown: () => stepFile(1), k: () => stepFile(-1), ArrowUp: () => stepFile(-1),
                     n: () => stepHunk(1), p: () => stepHunk(-1) }[e.key];
      if (move) { e.preventDefault(); move(); }
      return;
    }
    if (screen !== "confirm" && screen !== "log" && screen !== "setup") return;
    if (e.key === "Escape") { e.preventDefault(); closeConsole(); return; }
    if (e.key !== "Tab") return;
    const items = [conBody, cancelKey, goKey].filter(n => !n.hidden && !n.disabled);
    const first = items[0], last = items[items.length - 1];
    const inside = $("scr-console").contains(document.activeElement);
    if (e.shiftKey && (document.activeElement === first || !inside)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (document.activeElement === last || !inside)) { e.preventDefault(); first.focus(); }
  });

  // ---------- start
  go("deck");
  updateKeys();
  poll();
  setInterval(poll, POLL_MS);
  setInterval(tick, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
})();
