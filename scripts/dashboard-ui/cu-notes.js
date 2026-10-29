// The NOTES editor on the control unit's SETUP screen (doomtickle/passdesk-ops#186).
// Loaded before cu.js; cu.js calls CuNotes.create() with the few things it lends
// (its request function, which holds the page's key, and the console's nodes), so
// the key never leaves cu.js. The editor writes only the bound project's notes
// files, through three POSTs: config-read (load a file), config-prepare (the host
// returns a single-use token and the diff of what it would write) and config-run
// (the token back, after CONFIRM, which stays dark for HOLD_MS). Nothing is written
// by a single press, and nothing the server says becomes HTML, an attribute, a URL
// or a class name: strings go in with textContent, classes come from the fixed
// maps below.
"use strict";

(() => {
  const ROLE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/;
  const DEFAULT_LIMIT = 20000;     // characters a role's instructions plus the notes may total
  const TEXT_MAX = 20000;          // the most one notes file may hold (the host refuses more)
  const BAR = 24, DIFF_MAX = 2000;
  const GAUGE = { ok: "k-ok", warn: "k-warn", over: "k-err" };
  const DIFF = { "+": ["nd nd-add", "+"], "-": ["nd nd-del", "-"], " ": ["nd nd-ctx", " "] };
  const DEMO = "DEMO · CHANGES DISABLED";
  const SAVED = "SAVED · SERVICE DUE · RESTART TO APPLY";
  const APPLIES = { RESTART: "restart" };
  // Control, invisible and bidi characters would hide what a diff really writes: shown as tokens.
  const HIDDEN = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u00ad\u061c\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]/g;

  const cp = s => Array.from(s).length;                    // the host counts characters, not UTF-16 units
  const norm = s => s.replace(/\r\n?/g, "\n");             // what a textarea holds
  const part = (header, n) => (n > 0 ? cp(header) + n : 0); // an empty notes file adds no heading
  const sharedHeader = project => `\n# This project (${project})\n`;
  const roleHeader = (project, role) => `\n# This project (${project}), for the ${role}\n`;
  const level = (total, limit) => (total > limit ? "over" : total * 10 >= limit * 9 ? "warn" : "ok");
  const mmss = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const seen = s => {
    let n = 0;
    const text = String(s).replace(HIDDEN, ch => { n++; return `⟨U+${ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0")}⟩`; });
    return { text, n };
  };

  // A role's total (its instructions plus the notes the bridge adds) with the box's text in place of
  // the saved file's: the shared file counts for every role, a role's file for that role only.
  function projected(base, project, tab, role, savedChars, newChars) {
    if (tab === "") return base + part(sharedHeader(project), newChars) - part(sharedHeader(project), savedChars);
    if (tab === role) return base + part(roleHeader(project, role), newChars) - part(roleHeader(project, role), savedChars);
    return base;
  }

  function create(d) {
    const { el, str, list, num, conBody, cancelKey, goKey } = d;
    const P = (cls, text) => el("p", cls, text);
    const span = (cls, text) => el("span", cls, text);
    const H = (text, tag) => { const h = el("h5", null, text); if (tag) h.append(el("span", "tag", tag)); return h; };
    const button = (cls, text, onClick) => {
      const b = el("button", cls, text);
      b.type = "button";
      b.addEventListener("click", onClick);
      return b;
    };

    let on = false, tab = null, phase = "idle";
    let loaded = "", draft = "", savedChars = 0, exists = false;
    let prep = null, ask = null, status = null, override = null;
    let seq = 0, holdUntil = 0, tickTimer = 0, holdTimer = 0;
    let refs = {};

    const cfg = () => { const s = d.state(); return s && s.config && typeof s.config === "object" ? s.config : null; };
    const block = () => { const c = cfg(); return c && c.notes && typeof c.notes === "object" ? c.notes : {}; };
    const project = () => str(block().project);
    const usable = () => { const p = project(); return !!p && p !== "(no project)" && !p.includes("/") && !p.startsWith("."); };
    const limit = () => { const n = Number(block().limit); return Number.isFinite(n) && n > 0 ? n : DEFAULT_LIMIT; };
    const roles = () => { const c = cfg(); return c ? list(c.roles).filter(r => r && typeof r === "object" && ROLE.test(str(r.name))) : []; };
    const label = t => (t === "" ? "SHARED" : t.toUpperCase());
    const fileName = t => `${project()}${t === "" ? "" : `.${t}`}.md`;
    const dirty = () => tab !== null && draft !== loaded;

    // ---------- the gauges
    function baseOf(r) {
      const v = override && override.totals[r.name] != null ? override.totals[r.name] : r.instructions_chars;
      return v == null || !Number.isFinite(Number(v)) ? null : Number(v);
    }
    const gauged = () => roles().filter(r => baseOf(r) !== null);
    function totalOf(r) {
      return projected(baseOf(r), project(), tab, r.name, savedChars, cp(draft));
    }
    const affected = r => tab === "" || tab === r.name;
    const stillOver = () => gauged().filter(r => affected(r) && totalOf(r) > limit());
    // The host lets a change through that lowers (or keeps) a total already over the limit: it is the way back under.
    const overRoles = () => stillOver().filter(r => totalOf(r) > baseOf(r));
    const canSave = () => phase === "edit" && dirty() && cp(draft) <= TEXT_MAX && overRoles().length === 0;

    function paintGauge(g) {
      const total = totalOf(g.role), lim = limit(), lv = level(total, lim);
      const filled = lv === "over" ? BAR : Math.min(BAR, Math.round(total / lim * BAR));
      g.row.className = `ngauge ${GAUGE[lv]}`;
      const bar = "█".repeat(filled) + "░".repeat(BAR - filled);
      if (g.bar.textContent !== bar) g.bar.textContent = bar;
      const count = `  ${num(total)} / ${num(lim)}  ${Math.round(total / lim * 100)}%`;
      if (g.count.textContent !== count) g.count.textContent = count;
      const flag = lv === "over" ? `  OVER by ${num(total - lim)}` : lv === "warn" ? "  NEAR THE LIMIT" : "";
      if (g.flag.textContent !== flag) g.flag.textContent = flag;
    }
    function gaugeRow(r) {
      const row = el("p", "ngauge");
      const g = { role: r, row, bar: span("nbar", ""), count: span(null, ""), flag: span(null, "") };
      row.append(span(null, `  ${str(r.name).padEnd(14)} `), g.bar, g.count, g.flag);
      paintGauge(g);
      return g;
    }

    // ---------- the keys and the head
    function setKeys(cancelText, goText, goOn) { d.keys(cancelText, goText, goOn); }
    function editKeys() { setKeys("Back", "Save", canSave()); }

    // ---------- rendering
    function tabsRow() {
      const row = el("div", "ntabs");
      row.setAttribute("role", "tablist");
      refs.tabs = {};
      for (const t of ["", ...roles().map(r => str(r.name))]) {
        const b = button(`ntab${t === tab ? " on" : ""}`, label(t), () => switchTab(t));
        b.setAttribute("role", "tab");
        b.setAttribute("aria-selected", t === tab ? "true" : "false");
        refs.tabs[t] = b;
        row.append(b);
      }
      return row;
    }
    function paintTabs() {
      for (const t in refs.tabs) {
        const text = label(t) + (t === tab && dirty() ? " *" : "");
        if (refs.tabs[t].textContent !== text) refs.tabs[t].textContent = text;
      }
    }
    function done(focus) {
      conBody.scrollTop = 0;
      d.more();
      if (focus) focus.focus();
    }
    function head(tag, cls, led, note) { d.head(tag, cls, led, note); }

    function renderBusy(text, tagText) {
      refs = {};
      head(tagText, "check", "a blink", "");
      conBody.replaceChildren(P("banner", `Notes · ${label(tab || "")}`), tabsRow(), P("cursor", text));
      setKeys("Back", "Save", false);
      done(null);
    }

    function renderBroken(text) {
      phase = "broken";
      refs = {};
      head("Refused", "refused", "r", "");
      conBody.replaceChildren(P("banner", `Notes · ${label(tab || "")}`), tabsRow(), P("k-err", `✕ ${text}`));
      setKeys("Back", null, false);
      done(cancelKey);
    }

    function renderEdit() {
      phase = "edit";
      head("Editing", "check", "a", "Notes are read when the roles start: a restart applies a saved change.");
      const ta = el("textarea", "nedit");
      ta.value = draft;
      ta.spellcheck = false;
      ta.setAttribute("aria-label", `Notes: ${fileName(tab)}`);
      ta.addEventListener("input", () => {
        // maxLength counts UTF-16 units; the host counts characters, so the cap is enforced here.
        if (cp(ta.value) > TEXT_MAX) {
          const at = ta.selectionStart;
          ta.value = Array.from(ta.value).slice(0, TEXT_MAX).join("");
          if (typeof at === "number" && ta.setSelectionRange) ta.setSelectionRange(Math.min(at, ta.value.length), Math.min(at, ta.value.length));
        }
        draft = ta.value; status = null; live();
      });
      const g = gauged().map(gaugeRow);
      refs = { ta, tabs: {}, gauges: g, count: P("k-dim", ""), status: P("nstatus", ""), ask: el("div", "nask"), hint: null };
      refs.ask.setAttribute("role", "alert");
      refs.ask.hidden = true;
      const off = tab !== "" && !roles().some(r => r.name === tab && baseOf(r) !== null);
      conBody.replaceChildren(
        P("banner", `Notes · ${project()}`),
        tabsRow(),
        P("k-dim", `  ${fileName(tab)}${exists ? "" : "  (new: the file is created when you save)"}${off ? "  · this role is off, so nothing reads it yet" : ""}`),
        ta, refs.count, refs.ask,
        H("Characters per role", `instructions + shared + role notes, limit ${num(limit())}`),
        ...g.map(x => x.row), refs.status);
      live();
      if (ask) showAsk();
      done(ta);
    }

    // Everything that follows the box's text: the count, the gauges, the keys and the status line.
    function live() {
      if (phase !== "edit" || !refs.ta) return;
      const n = cp(draft);
      setText(refs.count, `  ${num(n)} character${n === 1 ? "" : "s"} in this file`);
      for (const g of refs.gauges) paintGauge(g);
      paintTabs();
      const over = overRoles();
      let text, cls;
      if (status) { text = status.text; cls = status.kind === "err" ? "k-err" : status.kind === "ok" ? "k-ok" : "k-dim"; }
      else if (over.length) {
        const worst = Math.max(...over.map(r => totalOf(r) - limit()));
        text = `✕ ${over.map(r => str(r.name)).join(", ")} would be over ${num(limit())} characters: cut ${num(worst)} to save`;
        cls = "k-err";
      } else if (!dirty()) { text = "  No changes yet."; cls = "k-dim"; }
      else if (stillOver().length) { text = `  ${stillOver().map(r => str(r.name)).join(", ")} still over ${num(limit())}, but this change does not add to it: SAVE is allowed.`; cls = "k-warn"; }
      else { text = "  Unsaved changes. SAVE shows what would be written and asks you to confirm."; cls = "k-dim"; }
      refs.status.className = `nstatus ${cls}`;
      setText(refs.status, text);
      editKeys();
    }
    const setText = (n, t) => { if (n.textContent !== t) n.textContent = t; };

    function showAsk() {
      if (!refs.ask) return;
      const a = refs.ask;
      a.replaceChildren();
      a.hidden = !ask;
      if (!ask) return;
      const what = ask.leave ? "Leave the notes" : `Switch to ${label(ask.to)}`;
      a.append(
        span(null, `Unsaved changes in ${label(tab)}. ${what} and they are lost?  `),
        button("svckey", "Discard", () => { const to = ask && ask.to, leave = ask && ask.leave; ask = null; if (leave) exit(); else load(to); }),
        span(null, " "),
        button("svckey", "Keep editing", () => { ask = null; showAsk(); refs.ta.focus(); }));
      d.more();
    }

    function drawDiff(rows, out) {
      let hidden = 0, plus = 0, minus = 0;
      const lines = list(rows).filter(x => x && typeof x === "object");
      for (const r of lines.slice(0, DIFF_MAX)) {
        const op = DIFF[r.op] ? r.op : "?";
        const s = seen(str(r.text));
        hidden += s.n;
        if (op === "+") plus++; else if (op === "-") minus++;
        const [cls, mark] = DIFF[op] || ["nd nd-ctx", "?"];
        out.push(P(cls, `${mark} ${s.text}`));
      }
      for (const r of lines.slice(DIFF_MAX)) if (r.op === "+") plus++; else if (r.op === "-") minus++;
      if (lines.length > DIFF_MAX) out.push(P("k-warn", `… ${num(lines.length - DIFF_MAX)} more lines not shown`));
      return { hidden, plus, minus };
    }

    function renderConfirm() {
      phase = "confirm";
      refs = {};
      const data = prep.data, out = [P("banner", `Save notes · ${label(prep.tab)}`)];
      out.push(P("ind k-cmd", `  file: ${str(data.path) || fileName(prep.tab)}`));
      out.push(P("ind k-dim", `  Applies on ${APPLIES[data.applies] || "?"}: the roles read their notes when they start.`));
      const diffLines = [];
      const sum = drawDiff(data.diff, diffLines);
      out.push(H("What would be written", `+${sum.plus} −${sum.minus}`));
      if (sum.hidden) out.push(P("k-warn", `  ${num(sum.hidden)} hidden or control character${sum.hidden === 1 ? "" : "s"} shown as ⟨U+XXXX⟩`));
      out.push(...diffLines);
      const totals = data.totals && typeof data.totals === "object" ? data.totals : {};
      const lim = Number.isFinite(Number(data.limit)) && Number(data.limit) > 0 ? Number(data.limit) : limit();
      const names = roles().map(r => str(r.name)).filter(n => Number.isFinite(Number(totals[n])));
      out.push(H("Totals after saving", `limit ${num(lim)}`));
      if (!names.length) out.push(P("k-dim", "  No running role reads this file."));
      for (const n of names) {
        const t = Number(totals[n]), lv = level(t, lim);
        out.push(P(`ind ${GAUGE[lv]}`, `  ${n.padEnd(14)} ${num(t)} of ${num(lim)}${lv === "over" ? " · OVER" : ""}`));
      }
      out.push(el("div", "gap"), P("k-dim", "Nothing has been written yet. CONFIRM SAVE writes it; CANCEL or Esc backs out."));
      conBody.replaceChildren(...out);
      setKeys("Cancel", "Confirm save", false);
      tickConfirm();
      done(cancelKey);
    }

    function tickConfirm() {
      if (phase !== "confirm" || !prep) return;
      const left = Math.max(0, Math.ceil((prep.deadline - Date.now()) / 1000));
      if (left <= 0) {
        phase = "expired";
        prep.token = null;
        stopTimers();
        head("Expired", "expired", "r", "The confirmation expired. Cancel to go back to your text.");
        setKeys("Cancel", "Expired · start over", false);
        return;
      }
      head("Confirm", "confirm", "a blink", `Confirm within ${mmss(left)}`);
      setKeys("Cancel", "Confirm save", Date.now() >= holdUntil);
    }
    function stopTimers() { clearInterval(tickTimer); clearTimeout(holdTimer); tickTimer = holdTimer = 0; }

    function renderChanged() {
      phase = "changed";
      refs = {};
      head("Changed", "expired", "r", "");
      conBody.replaceChildren(
        P("banner", "Changed on disk"),
        P("k-err", `✕ ${fileName(tab)} changed on disk after you loaded it, so nothing was written.`),
        P("k-dim", "  RELOAD reads the file again and drops your text. KEEP MY TEXT goes back to it; saving again compares it with the file now on disk."));
      setKeys("Keep my text", "Reload", false);
      holdUntil = Date.now() + d.holdMs;
      clearTimeout(holdTimer);
      holdTimer = setTimeout(() => { if (phase === "changed") setKeys("Keep my text", "Reload", true); }, d.holdMs + 20);
      done(cancelKey);
    }

    // ---------- actions
    async function load(t) {
      const my = ++seq;
      stopTimers();
      tab = t; phase = "loading"; draft = loaded = ""; prep = null; ask = null; status = null; savedChars = 0; exists = false;
      renderBusy("▶ Reading the file…", "Reading");
      let res;
      try {
        res = await d.api("config-read", { kind: "notes", role: t });
      } catch (e) {
        if (my === seq) renderBroken("Lost the dashboard before it answered.");
        return;
      }
      if (my !== seq) return;
      if (!res.ok) { renderBroken(d.errText(res)); return; }
      const text = str(res.data.text);
      exists = res.data.exists !== false;
      loaded = draft = norm(text);
      savedChars = Number.isFinite(Number(res.data.chars)) ? Number(res.data.chars) : cp(text);
      renderEdit();
    }

    function switchTab(t) {
      if (t === tab || phase === "preparing" || phase === "saving") return;
      if (phase === "edit" && dirty()) { ask = { to: t }; showAsk(); return; }
      load(t);
    }

    function backToEdit(text, kind) {
      stopTimers();
      prep = null;
      status = text ? { text, kind: kind || "err" } : null;
      renderEdit();
    }

    async function save() {
      if (!canSave()) return;
      const my = ++seq, sent = draft, role = tab;
      phase = "preparing";
      refs.ta.disabled = true;
      refs.ask.hidden = true;
      ask = null;
      setKeys("Back", "Save", false);
      status = { text: "▶ Checking what would be written…", kind: "dim" };
      setText(refs.status, status.text);
      let res;
      try {
        res = await d.api("config-prepare", { kind: "notes", role, text: sent });
      } catch (e) {
        if (my === seq) backToEdit("Lost the dashboard before it answered. Nothing was written.");
        return;
      }
      if (my !== seq) return;
      if (res.status === 403) { backToEdit(DEMO); return; }
      if (!res.ok) { backToEdit(d.errText(res)); return; }
      const data = res.data;
      if (typeof data.token !== "string" || !data.token || !Array.isArray(data.diff)) { backToEdit("The dashboard's answer was not understood. Nothing was written."); return; }
      if (data.changed === false) { backToEdit("NO CHANGE · the file on disk already reads this", "dim"); return; }
      const secs = Number(data.expires);
      prep = { token: data.token, tab: role, text: sent, data, deadline: Date.now() + (Number.isFinite(secs) && secs > 0 ? secs : 120) * 1000 };
      holdUntil = Date.now() + d.holdMs;
      renderConfirm();
      tickTimer = setInterval(tickConfirm, 1000);
      holdTimer = setTimeout(tickConfirm, d.holdMs + 20);
    }

    async function run() {
      if (phase !== "confirm" || !prep || !prep.token || Date.now() < holdUntil) return;
      const my = ++seq, token = prep.token, sent = prep;
      prep.token = null;   // single use, whatever the answer
      phase = "saving";
      stopTimers();
      head("Saving", "running", "g blink", "");
      setKeys("Cancel", "Confirm save", false);
      conBody.append(P("cursor", "▶ Writing…"));
      let res;
      try {
        res = await d.api("config-run", { token });
      } catch (e) {
        if (my === seq) backToEdit("Lost the dashboard before it answered. Check the file before saving again.");
        return;
      }
      if (my !== seq) return;
      if (res.ok) {
        const totals = sent.data.totals && typeof sent.data.totals === "object" ? sent.data.totals : null;
        override = totals ? { totals, file: fileName(sent.tab), chars: cp(sent.text) } : null;
        loaded = norm(sent.text);
        savedChars = cp(sent.text);
        exists = true;
        draft = sent.text;
        backToEdit(res.data.applies === "RESTART" || sent.data.applies === "RESTART" ? SAVED : "SAVED", "ok");
        d.refresh();
      } else if (res.status === 409 && res.data.error === "changed") {
        renderChanged();
      } else if (res.status === 403) {
        backToEdit("The confirmation is no longer valid (expired, already used, or replaced by a newer check). Save again to start over.");
      } else {
        backToEdit(d.errText(res));
      }
    }

    function exit() {
      seq++;
      stopTimers();
      on = false; phase = "idle"; prep = null; ask = null; status = null; refs = {};
      d.back();
    }

    function enter() {
      if (on || !usable()) return;
      on = true; override = null;
      load("");
    }

    // The Close / Cancel key and Esc: what they mean depends on where the editor is.
    function close() {
      if (!on) return false;
      if (phase === "saving") return true;
      if (phase === "preparing") { seq++; backToEdit(null); return true; }
      if (phase === "confirm" || phase === "expired") { backToEdit(null); return true; }
      if (phase === "changed") { backToEdit("Your text is kept. SAVE again to compare it with the file now on disk.", "dim"); return true; }
      if (ask) { ask = null; showAsk(); return true; }
      if (phase === "edit" && dirty()) { ask = { leave: true }; showAsk(); return true; }
      exit();
      return true;
    }

    function go() {
      if (!on || goKey.disabled) return;
      if (phase === "edit") save();
      else if (phase === "confirm") run();
      else if (phase === "changed" && Date.now() >= holdUntil) load(tab);
    }
    goKey.addEventListener("click", go);

    // A poll brought new numbers: the gauges follow, the box and its text do not move.
    function onState() {
      if (override) {
        const f = list(block().files).find(x => x && x.name === override.file);
        if ((f && Number.isFinite(Number(f.chars)) ? Number(f.chars) : 0) === override.chars) override = null;
      }
      if (phase === "edit") live();
    }

    function openKey() {
      if (!usable()) return P("k-dim", "  Notes can be edited once the factory is bound to a project.");
      const row = el("p", "nopen");
      row.append(button("svckey", "Edit notes", enter));
      return row;
    }

    return { openKey, close, onState, active: () => on };
  }

  window.CuNotes = Object.freeze({ create, projected, level, cp });
})();
