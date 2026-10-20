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
// update changed the dashboard's page). The main page never answers.
// Starting a restart or an update takes two presses: the key asks the server
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
  // "cu:collapse". The settings screen registers here later; settings that move
  // the security boundary (broker policy and hosts, credentials) are shown there
  // read-only, never edited from a browser.
  const SCREENS = {
    deck: { node: $("scr-deck"), expanded: false },      // gate deck and service row (compact, in the rail)
    confirm: { node: $("scr-console"), expanded: true },  // what RESTART / UPDATE would do, and CONFIRM
    log: { node: $("scr-console"), expanded: true },      // the running action's output
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
    // Most telling first: what and who, a PR's title, then where it comes from and goes.
    const out = [line("ln big", `${str(c.tool) || "write"} · ${str(c.role) || "?"}`)];
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

  function renderDeck() {
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

    setClass($("cu-led"), cs.length ? "led a blink" : "led g");
    $("sel").hidden = cs.length < 2;
    if (cs.length > 1) setText($("sel-n"), `${cs.indexOf(c) + 1}/${cs.length}`);

    const k = c ? JSON.stringify(Object.assign({}, c, { seconds_left: c.seconds_left == null })) : "empty";
    if (k !== shownKey) {
      shownKey = k;
      body.replaceChildren(...(c ? cardLines(c) : [el("div", "ln idle", "No writes held")]));
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
    const live = !!c && !busy && !held;
    reason.disabled = !c;
    $("keys-hold").hidden = !held;
    const canDecline = live && reason.value.trim() !== "";
    const canApprove = live && !expired(c);
    $("btn-decline").disabled = !canDecline;
    $("btn-approve").disabled = !canApprove;
    setClass($("led-decline"), `led${canDecline ? " r" : ""}`);
    setClass($("led-approve"), `led${canApprove ? " g" : ""}`);
    if (canDecline) reason.classList.remove("need");
    $("btn-approve").title = c && expired(c) ? "This hold has expired" : "";
    $("btn-decline").title = c && !canDecline && !busy && !held ? "Type a reason to decline" : "";

    const a = act(), run = !fault && running();
    const bound = !!(st && st.bound);
    $("svc-restart").disabled = !!fault || !st || run || !bound;
    $("svc-restart").title = st && !bound ? "No project is bound to the factory: start it once with factory start" : "";
    $("svc-update").disabled = !!fault || !st || run;
    setClass($("led-restart"), `led${run ? " a blink" : ""}`);
    setClass($("led-update"), `led${run ? " a blink" : ""}`);
    const showLog = !fault && !!a.name && a.phase !== "idle";
    $("svc-log").hidden = !showLog;
    setClass($("led-log"), `led${run ? " a blink" : a.phase === "done" ? " g" : a.phase === "failed" ? " r" : ""}`);
  }

  // Once a second: the countdown, and APPROVE goes dark when the hold expires.
  function tick() {
    const c = fault ? null : current();
    const s = c ? secondsLeft(c) : null;
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
    selId = c.id; selFp = c.fp;
    renderDeck();
  }

  const WHY = {
    gone: "Gone: that write is no longer held (decided elsewhere, or the agent gave up).",
    changed: "Changed: a new write replaced that card. Check it, then decide again.",
    expired: "Expired: the hold ran out. The agent can push again.",
  };
  async function decide(verdict) {
    const c = current();
    if (!c || busy) return;
    const reason = $("reason").value.trim();
    if (verdict === "decline" && !reason) {
      $("reason").classList.add("need");
      $("reason").focus();
      return;
    }
    if (verdict === "approve" && expired(c)) return;
    busy = true;
    updateKeys();
    try {
      const res = await api("decide", { id: c.id, fp: c.fp, verdict, reason });
      if (res.ok) {
        expectGone = c.id;
        $("reason").value = "";
        flash(`${verdict === "approve" ? "APPROVED" : "DECLINED"} · ${str(res.data.text)}`, "ok");
      } else if (res.status === 409) {
        const e = errText(res).toLowerCase();
        flash(e.includes("gone") ? WHY.gone : e.includes("changed") ? WHY.changed : e.includes("expired") ? WHY.expired : errText(res), "err");
      } else {
        flash(errText(res), "err");
      }
    } catch (e) {
      flash("No link to the dashboard: nothing was decided.", "err");
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
  const RUNS = { restart: "factory restart", update: "factory update --yes" };

  function head(action, tag, tagCls, led, note) {
    setText($("con-title"), `SERVICE · ${action.toUpperCase()}`);
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
    keys("Cancel", `Confirm ${action}`, false);
    conBody.replaceChildren(
      P("banner", action === "update" ? "Update the factory" : "Restart the factory"),
      P("cursor", action === "update"
        ? "▶ Fetching the factory's upstream and comparing the sandboxes with it (this can take up to a minute and a half)…"
        : "▶ Checking what a restart would stop…"));
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

  function renderConfirm(d) {
    const action = svc.action;
    svc.token = d.confirm;
    svc.deadline = Date.now() + Math.max(0, Number(d.expires_in) || 0) * 1000;
    svc.state = "ready";
    svc.onGo = runAction;
    const out = [P("banner", action === "update" ? "Update the factory" : "Restart the factory")];
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
    keys("Cancel", `Confirm ${action}`, false);
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
      head(name, ok ? "Done" : "Failed", ok ? "done" : "failed", ok ? "g" : "r");
      logStatus.replaceChildren(P(ok ? "k-ok" : "k-err",
        `${ok ? "✓" : "✕"} ${name.toUpperCase()} ${ok ? "FINISHED" : "FAILED"} · exit ${exit == null ? "unknown" : str(exit)}`));
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

  // ---------- leaving the monitor
  function closeConsole() {
    if (screen !== "confirm" && screen !== "log") return;
    svcSeq++;           // a pending prepare or run answer is dropped
    svc = null;
    clearTimeout(logTimer); logTimer = 0;
    go("deck");
    renderDeck();
    const back = lastKey && !lastKey.disabled && !lastKey.hidden ? lastKey : $("btn-approve").disabled ? $("deck-lcd") : $("btn-approve");
    back.focus();
  }
  cancelKey.addEventListener("click", closeConsole);
  goKey.addEventListener("click", () => { if (svc && svc.onGo && !goKey.disabled) svc.onGo(); });
  $("svc-restart").addEventListener("click", () => openService("restart"));
  $("svc-update").addEventListener("click", () => openService("update"));
  $("svc-log").addEventListener("click", () => { lastKey = $("svc-log"); openLog(false); });

  // The monitor is modal: Esc backs out, Tab stays inside it.
  document.addEventListener("keydown", e => {
    if (screen !== "confirm" && screen !== "log") return;
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
