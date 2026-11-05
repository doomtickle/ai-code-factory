// The Thoughtfield's lane monitor: one lane's transcript, its jobs' review packets and what each job
// changed. window.FieldMonitor = { open(laneName, jobId?), close(), isOpen(), current() } (current: the lane and job on screen, or null).
//
// Only GET /api/lane?name=&job= and GET /api/diff?job= on this origin. Every string from the server is
// agent-written and goes in with textContent. Classes come only from the fixed maps below, and a lane
// name or job id reaches a URL only after matching its pattern and through encodeURIComponent.
"use strict";

(() => {
  const POLL_MS = 2000, OPEN_LINES = 400;
  const LANE = /^[a-z][a-z0-9-]{1,40}$/;
  const JOB_ID = /^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$/;

  // Maps, not objects: a server string such as "constructor" must find nothing here.
  const STATES = new Map([
    ["running", ["Running", "fm-st-run"]], ["held", ["Held", "fm-st-warn"]], ["revise", ["Revise", "fm-st-warn"]],
    ["done", ["Done", "fm-st-ok"]], ["approved", ["Approved", "fm-st-ok"]], ["idle", ["Idle", "fm-st-idle"]],
    ["cancelled", ["Cancelled", "fm-st-bad"]], ["failed", ["Failed", "fm-st-bad"]], ["interrupted", ["Interrupted", "fm-st-bad"]],
  ]);
  const LINE_KINDS = new Map([
    ["start", "fm-k-mark"], ["end", "fm-k-mark"], ["task", "fm-k-task"], ["tool", "fm-k-tool"], ["result", "fm-k-result"],
    ["error", "fm-k-error"], ["branch", "fm-k-branch"], ["packet", "fm-k-branch"],
  ]);
  const DIFF_KINDS = new Map([[" ", "fm-d-ctx"], ["+", "fm-d-add"], ["-", "fm-d-del"], ["\\", "fm-d-note"]]);
  const FILE_STATUS = new Map([
    ["A", ["Added", "fm-s-add"]], ["D", ["Removed", "fm-s-del"]], ["M", ["Changed", "fm-s-mod"]],
    ["R", ["Renamed", "fm-s-mod"]], ["B", ["Binary", "fm-s-mod"]],
  ]);
  const FLAGS = new Map([
    ["bidi", "Text direction marks"], ["invisible", "Invisible characters"], ["control", "Control characters"],
    ["crlf", "Windows line endings"], ["longline", "Long line cut"], ["malformed", "Malformed"],
  ]);
  const TABS = [["transcript", "Transcript"], ["packet", "Packet"], ["diff", "Changes"]];

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  };
  const button = (cls, text) => { const b = el("button", cls, text); b.type = "button"; return b; };
  const str = v => (typeof v === "string" ? v : "");
  const list = v => (Array.isArray(v) ? v : []);
  const count = v => (Number.isFinite(v) ? v : 0);
  const usd = v => (Number.isFinite(v) ? `$${v.toFixed(2)}` : "not reported yet");
  const clock = n => {
    if (!Number.isFinite(n)) return "unknown";
    if (n < 3600) return `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, "0")}`;
    return `${Math.floor(n / 3600)}h ${String(Math.floor(n % 3600 / 60)).padStart(2, "0")}m`;
  };
  const stateOf = s => STATES.get(s) || ["Unknown", "fm-st-idle"];
  const message = text => el("p", "fm-msg", text);

  // ---------- state
  let ui = null;                // the DOM, built once on first open
  let lane = null;              // the open lane's name (matched LANE)
  let job = "";                 // the job asked for ("" = the lane's newest, followed)
  let tab = "transcript";
  let data = null;              // the latest /api/lane answer for this lane
  let failed = false;           // the latest /api/lane request failed
  let diff = null;              // { job, data } the latest /api/diff answer; null while there is none
  let diffFailed = "";          // the job whose /api/diff request failed
  let diffAsking = "";          // the job whose /api/diff request is in flight
  let stepping = false;         // moved to another job and its answer has not come yet
  let shown = { key: null, lines: [] };  // what the body draws now, so a poll changes only what changed
  let laneSeq = 0, diffSeq = 0; // a stale answer is dropped
  let timer = 0;
  let backTo = null;            // what had focus before the monitor opened

  // ---------- requests
  function schedulePoll() {
    clearTimeout(timer);
    timer = 0;
    if (lane && tab === "transcript" && data && data.state === "running") timer = setTimeout(loadLane, POLL_MS);
  }
  async function loadLane() {
    clearTimeout(timer);
    timer = 0;
    if (!lane || !LANE.test(lane) || (job && !JOB_ID.test(job))) return;
    const seq = ++laneSeq, asked = lane;
    const q = `name=${encodeURIComponent(asked)}${job ? `&job=${encodeURIComponent(job)}` : ""}`;
    let answer = null;
    try {
      const r = await fetch(`/api/lane?${q}`, { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) throw new Error(String(r.status));
      answer = await r.json();
      if (answer === null || typeof answer !== "object" || Array.isArray(answer)) throw new Error("shape");
    } catch (e) { answer = null; }
    if (seq !== laneSeq || lane !== asked) return;
    failed = !answer;
    if (answer) { data = answer; stepping = false; }
    render();
    schedulePoll();
  }
  async function loadDiff(id) {
    if (!JOB_ID.test(id)) return;
    const seq = ++diffSeq;
    diffFailed = "";
    diffAsking = id;
    let answer = null;
    try {
      const r = await fetch(`/api/diff?job=${encodeURIComponent(id)}`, { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) throw new Error(String(r.status));
      answer = await r.json();
      if (answer === null || typeof answer !== "object" || Array.isArray(answer)) throw new Error("shape");
    } catch (e) { answer = null; }
    if (seq !== diffSeq || !lane) return;
    diffAsking = "";
    if (answer) diff = { job: id, data: answer };
    else diffFailed = id;
    render();
  }

  // ---------- jobs
  const jobIds = () => list(data && data.jobs).filter(id => typeof id === "string" && JOB_ID.test(id));
  const shownJob = () => { const id = data && data.job; return typeof id === "string" && JOB_ID.test(id) ? id : ""; };
  function position() {
    const jobs = jobIds();
    return { jobs, i: jobs.indexOf(job || jobs[0] || "") };
  }
  function step(dir) {  // +1 older, -1 newer. The newest is followed, so a job that starts is shown.
    if (!lane || tab === "transcript") return;
    const { jobs, i } = position();
    const to = i < 0 ? -1 : i + dir;
    if (to < 0 || to >= jobs.length) return;
    job = to === 0 ? "" : jobs[to];
    diff = null; diffFailed = ""; diffAsking = ""; diffSeq++;
    stepping = true; failed = false;
    data = Object.assign({}, data, { pending: false, packet: null, job: jobs[to], sel: null });
    render();
    loadLane();
  }
  function setTab(next) {
    if (!lane) return;
    tab = next;
    // A finished job's changes don't change; "none yet" or a failure is asked again.
    if (tab === "diff" && !(diff && diff.data.state === "ok")) { diff = null; diffFailed = ""; }
    render();
    ui.body.scrollTop = tab === "transcript" ? ui.body.scrollHeight : 0;
    loadLane();  // a fresh look at the lane on every visit: only a running Transcript polls
  }

  // ---------- transcript
  function lineText(e) {
    const text = str(e.text);
    switch (e.kind) {
      case "start": return `Job ${text} started`;
      case "task": return `> ${text}`;
      case "end": return `Job ${text.replace(" ", " ended after ")}s`;
      case "branch": return `Branch ${text}`;
      case "packet": return `Packet ${text}`;
      case "error": return `Error: ${text}`;
      default: return text;
    }
  }
  const lineKey = e => `${str(e.ts)}\u0000${str(e.kind)}\u0000${str(e.text)}`;
  function lineNode(e) {
    const n = el("div", `fm-line ${LINE_KINDS.get(e.kind) || "fm-k-text"}`);
    n.append(el("span", "fm-ts", str(e.ts)), el("span", "fm-tx", lineText(e)));
    return n;
  }
  function renderTranscript() {
    const body = ui.body;
    if (!data) {
      show("t:wait", () => message(failed ? "Could not reach this lane. Press Transcript to try again." : "Loading…"));
      return;
    }
    const lines = list(data.tail).filter(e => e && typeof e === "object");
    const keys = lines.map(lineKey);
    const same = shown.key === "t";
    const old = same ? shown.lines : [];
    if (same && old.length === keys.length && (!keys.length || (old[0] === keys[0] && old[old.length - 1] === keys[keys.length - 1]))) return;
    const follow = !same || body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    if (!keys.length) {
      body.replaceChildren(message("Nothing in this lane's transcript yet."));
    } else {
      // A sliding window: old lines leave the front and new ones arrive at the end. Patching only the
      // ends keeps a text selection and the reading position across a poll.
      let drop = -1;
      for (let k = 0; k < old.length; k++) {
        const n = old.length - k;
        if (n <= keys.length && old[k] === keys[0] && old[old.length - 1] === keys[n - 1]) { drop = k; break; }
      }
      if (drop < 0 || body.children.length !== old.length) {
        body.replaceChildren(...lines.map(lineNode));
      } else {
        const before = body.scrollHeight;
        for (let k = 0; k < drop; k++) body.firstChild.remove();
        const removed = before - body.scrollHeight;
        body.append(...lines.slice(old.length - drop).map(lineNode));
        if (!follow && removed > 0) body.scrollTop = Math.max(0, body.scrollTop - removed);
      }
    }
    if (follow) body.scrollTop = body.scrollHeight;
    shown = { key: "t", lines: keys };
  }

  // ---------- packet
  function pendingView(sel, what) {
    const box = el("div", "fm-pending");
    box.append(el("p", "fm-pending-h", "This job is still running"),
      el("p", "fm-pending-n", `Its ${what} is written when it ends. Press the tab again to check.`));
    const rows = [["Task", str(sel.task) || "unknown"], ["Model", str(sel.model) || "unknown"], ["Elapsed", clock(sel.elapsed)],
      ["Cost so far", usd(sel.cost)], ["Started", str(sel.when) || "unknown"]];
    for (const [k, v] of rows) {
      const row = el("div", "fm-pending-row");
      row.append(el("span", "fm-pending-k", k), el("span", "fm-pending-v", v));
      box.append(row);
    }
    return box;
  }
  function show(key, build) {  // redraw the body only when what it shows has changed
    if (shown.key === key) return;
    const fresh = !shown.key || shown.key[0] !== key[0];
    ui.body.replaceChildren(build());
    if (fresh || key[0] !== "t") ui.body.scrollTop = 0;
    shown = { key, lines: [] };
  }
  function renderPacket() {
    if (!data) { show("p:wait", () => message(failed ? "Could not load this packet. Press Packet to try again." : "Loading…")); return; }
    if (stepping) { show(`p:step:${failed}`, () => message(failed ? "Could not load this job. Press Packet to try again." : "Loading…")); return; }
    if (data.pending && data.sel) {
      show(`p:pending:${JSON.stringify(data.sel)}`, () => pendingView(data.sel, "packet"));
      return;
    }
    const text = str(data.packet);
    show(`p:${shownJob()}\n${text}`, () => {
      if (!text) return message("No packet for this lane yet.");
      return el("pre", "fm-packet", text);
    });
  }

  // ---------- changes
  const stat = (adds, dels) => {
    const box = el("span", "fm-stat");
    box.append(el("span", "fm-plus", `+${count(adds).toLocaleString("en-US")}`), el("span", "fm-minus", `−${count(dels).toLocaleString("en-US")}`));
    return box;
  };
  function fileLines(file) {
    const box = el("div", "fm-file-body");
    const hunks = list(file.hunks);
    if (!hunks.length) {
      box.append(message(file.status === "B" ? "A binary file, so there is no text to show." : "No text changed: only its mode or name."));
      return box;
    }
    for (const h of hunks) {
      const header = str(h && h.header);
      const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(header);
      let oldN = m ? Number(m[1]) : 0, newN = m ? Number(m[2]) : 0;
      if (header) {
        const row = el("div", "fm-d fm-d-hunk");
        row.append(el("span", "fm-d-n"), el("span", "fm-d-m"), el("span", "fm-d-t", header));
        box.append(row);
      }
      for (const line of list(h && h.lines)) {
        if (!Array.isArray(line)) continue;
        const kind = line[0];
        let n = "";
        if (m && kind === "-") n = oldN++;
        else if (m && kind === "+") n = newN++;
        else if (m && kind === " ") { n = newN++; oldN++; }
        const row = el("div", `fm-d ${DIFF_KINDS.get(kind) || "fm-d-ctx"}`);
        row.append(el("span", "fm-d-n", n), el("span", "fm-d-m", kind === "+" ? "+" : kind === "-" ? "−" : ""), el("span", "fm-d-t", str(line[1])));
        box.append(row);
      }
    }
    return box;
  }
  function fileView(file, open) {
    const sec = el("section", "fm-file");
    const [label, cls] = FILE_STATUS.get(file.status) || ["Changed", "fm-s-mod"];
    const head = button("fm-file-h");
    const name = el("span", "fm-file-name");
    if (typeof file.old_path === "string") name.append(el("span", "fm-file-old", file.old_path), el("span", "fm-file-arrow", " to "));
    name.append(el("span", "fm-file-path", str(file.path) || "?"));
    head.append(el("span", `fm-file-s ${cls}`, label), name);
    for (const f of list(file.flags)) if (FLAGS.has(f)) head.append(el("span", "fm-flag", FLAGS.get(f)));
    head.append(stat(file.adds, file.dels));
    sec.append(head);
    let body = null;  // a diff of thousands of lines is not built until opened
    const set = on => {
      head.setAttribute("aria-expanded", String(on));
      if (on && !body) { body = fileLines(file); sec.append(body); }
      if (body) body.hidden = !on;
    };
    head.addEventListener("click", () => set(head.getAttribute("aria-expanded") !== "true"));
    set(open);
    return sec;
  }
  function diffView(d) {
    const root = el("div", "fm-diff");
    if (d.state !== "ok") {
      root.append(el("p", "fm-pending-h", d.state === "none" ? "No changes recorded for this job" : "These changes can't be shown"),
        el("p", "fm-pending-n", d.state === "none"
          ? "A job that brings back no branch has none: a review, or work that was not committed."
          : str(d.message) || "The file written next to this job's packet could not be used."));
      return root;
    }
    let budget = OPEN_LINES;
    for (const b of list(d.branches)) {
      if (!b || typeof b !== "object") continue;
      const files = list(b.files).filter(f => f && typeof f === "object");
      const br = el("section", "fm-branch");
      const head = el("div", "fm-branch-h");
      head.append(el("span", "fm-branch-ref", str(b.ref) || "?"));
      const bits = [];
      if (str(b.repo) && b.repo !== ".") bits.push(`in ${b.repo}`);
      if (str(b.tip)) bits.push(`at ${b.tip.slice(0, 7)}`);
      bits.push(`${files.length} file${files.length === 1 ? "" : "s"}`);
      head.append(el("span", "fm-branch-at", bits.join(" · ")), stat(b.adds, b.dels));
      br.append(head);
      if (str(b.error)) br.append(el("p", "fm-warn", `Could not make this diff: ${b.error}`));
      else if (!files.length) br.append(message(b.truncated ? "None of this diff fit under the size limit." : "No changes: the checked-out branch already has this one."));
      for (const f of files) {
        const lines = list(f.hunks).reduce((n, h) => n + list(h && h.lines).length + 1, 0);
        br.append(fileView(f, budget >= lines));
        budget -= lines;
      }
      if (b.truncated) br.append(el("p", "fm-warn", "Cut here: the rest of this diff was not kept. The packet's summary lists every file."));
      root.append(br);
    }
    if (!root.firstChild) root.append(message("No changes in this job."));
    return root;
  }
  function renderDiff() {
    if (!data) { show("d:wait", () => message(failed ? "Could not load this lane. Press Changes to try again." : "Loading…")); return; }
    if (stepping) { show(`d:step:${failed}`, () => message(failed ? "Could not load this job. Press Changes to try again." : "Loading…")); return; }
    if (data.pending && data.sel) {
      show(`d:pending:${JSON.stringify(data.sel)}`, () => pendingView(data.sel, "list of changes"));
      return;
    }
    const id = shownJob();
    if (!id) { show("d:nojob", () => message("No job on this lane yet.")); return; }
    if (diff && diff.job === id) { show(`d:${id}`, () => diffView(diff.data)); return; }
    if (diffFailed === id) { show(`d:failed:${id}`, () => message("Could not load these changes. Press Changes to try again.")); return; }
    show(`d:load:${id}`, () => message("Loading…"));
    if (diffAsking !== id) loadDiff(id);
  }

  // ---------- header and body
  function renderHead() {
    ui.title.textContent = lane;
    const [word, cls] = stateOf(data && data.state);
    ui.state.className = `fm-state ${data ? cls : "fm-st-idle"}`;
    ui.state.textContent = data ? word : "";
    for (const [key, node] of ui.tabs) node.setAttribute("aria-pressed", String(key === tab));
    const { jobs, i } = position();
    const nav = tab !== "transcript" && jobs.length > 0;
    ui.nav.hidden = !nav;
    if (nav) {
      ui.older.disabled = i < 0 || i >= jobs.length - 1;
      ui.newer.disabled = i <= 0;
      ui.pos.textContent = i < 0 ? `${jobs.length} jobs` : `Job ${i + 1} of ${jobs.length}`;
    }
    const sel = data && data.sel && typeof data.sel === "object" ? data.sel : null;
    const parts = [];
    if (failed && data) parts.push("Not updating: lost touch with the dashboard");
    if (nav && shownJob()) parts.push(shownJob());
    if (nav && sel) {
      if (STATES.has(sel.state)) parts.push(STATES.get(sel.state)[0]);
      if (Number.isFinite(sel.cost)) parts.push(usd(sel.cost));
      if (str(sel.when)) parts.push(sel.when);
    }
    ui.note.textContent = parts.join(" · ");
  }
  function render() {
    if (!lane) return;
    renderHead();
    if (tab === "transcript") renderTranscript();
    else if (tab === "packet") renderPacket();
    else renderDiff();
  }

  function build() {
    const root = document.getElementById("monitor-root");
    if (!root) return false;
    const scrim = el("div", "fm-scrim");
    scrim.hidden = true;
    const pool = el("div", "fm-pool");
    pool.setAttribute("role", "dialog");
    pool.setAttribute("aria-modal", "true");
    pool.setAttribute("aria-labelledby", "fm-title");
    const head = el("div", "fm-head");
    const title = el("h2", "fm-title");
    title.id = "fm-title";
    const state = el("span", "fm-state");
    const close_ = button("fm-key fm-close", "Close");
    close_.setAttribute("aria-label", "Close the monitor (Esc)");
    head.append(title, state, el("span", "fm-gap"), close_);
    const bar = el("div", "fm-bar");
    const tabs = new Map();
    for (const [key, word] of TABS) {
      const b = button("fm-tab", word);
      b.addEventListener("click", () => setTab(key));
      tabs.set(key, b);
      bar.append(b);
    }
    const nav = el("span", "fm-nav");
    const older = button("fm-key", "Older");
    older.setAttribute("aria-label", "Older job ([)");
    const pos = el("span", "fm-pos");
    const newer = button("fm-key", "Newer");
    newer.setAttribute("aria-label", "Newer job (])");
    nav.append(older, pos, newer);
    const note = el("span", "fm-note");
    bar.append(nav, note);
    const body = el("div", "fm-body");
    body.tabIndex = 0;
    body.setAttribute("aria-label", "Lane output");
    pool.append(head, bar, body);
    scrim.append(pool);
    root.append(scrim);
    older.addEventListener("click", () => step(1));
    newer.addEventListener("click", () => step(-1));
    close_.addEventListener("click", close);
    scrim.addEventListener("click", e => { if (e.target === scrim) close(); });
    ui = { scrim, pool, title, state, tabs, nav, older, pos, newer, note, body };
    return true;
  }

  // The monitor is modal: while it is up the page's own keys don't fire. Modified keys (a copy) pass.
  function onKey(e) {
    if (!lane || e.metaKey || e.ctrlKey || e.altKey) return;
    e.stopImmediatePropagation();
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "[") { e.preventDefault(); step(1); }
    else if (e.key === "]") { e.preventDefault(); step(-1); }
    else if (e.key === "Tab") {
      const items = [...ui.pool.querySelectorAll("button, [tabindex='0']")].filter(n => !n.disabled && n.offsetParent !== null);
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1], at = document.activeElement;
      const inside = ui.pool.contains(at);
      if (e.shiftKey && (at === first || !inside)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (at === last || !inside)) { e.preventDefault(); first.focus(); }
    }
  }

  function open(laneName, jobId) {
    if (typeof laneName !== "string" || !LANE.test(laneName)) return false;
    if (jobId != null && jobId !== "" && (typeof jobId !== "string" || !JOB_ID.test(jobId))) return false;
    if (!ui && !build()) return false;
    if (!lane) {
      backTo = document.activeElement;
      window.addEventListener("keydown", onKey, true);
    }
    lane = laneName;
    job = jobId || "";
    tab = job ? "packet" : "transcript";
    data = null; failed = false; stepping = false; diff = null; diffFailed = ""; diffAsking = "";
    shown = { key: null, lines: [] };
    laneSeq++; diffSeq++;
    ui.body.replaceChildren();
    ui.scrim.hidden = false;
    render();
    loadLane();
    ui.body.focus({ preventScroll: true });
    return true;
  }
  function close() {
    if (!lane) return;
    clearTimeout(timer);
    timer = 0;
    lane = null; job = ""; data = null; failed = false; stepping = false; diff = null; diffFailed = ""; diffAsking = "";
    shown = { key: null, lines: [] };
    laneSeq++; diffSeq++;
    window.removeEventListener("keydown", onKey, true);
    ui.scrim.hidden = true;
    ui.body.replaceChildren();
    const to = backTo;
    backTo = null;
    if (to && to.isConnected && typeof to.focus === "function") to.focus();
  }

  window.FieldMonitor = Object.freeze({ open, close, isOpen: () => lane !== null, current: () => lane === null ? null : { lane, job: shownJob() || job } });
})();
