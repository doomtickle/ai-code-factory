// The NOTES editor (scripts/dashboard-ui/cu-notes.js) on test_cu_page.js's fake DOM, fake
// server and fake timers: the gauges' arithmetic, SAVE dark while a role is over, the diff
// drawn as text, CONFIRM dark for the hold, a file that changed on disk, the demo refusal,
// and unsaved edits asked about before they are lost. Required by test_cu_page.js, which
// passes its harness; the names it returns print on a "NOTES [...]" line.
"use strict";

const SHARED_HDR = "\n# This project (shop)\n".length;                   // 23
const DEV_HDR = "\n# This project (shop), for the developer\n".length;    // 42
const LIMIT = 20000;

module.exports = async function run(h) {
  const { assert, doc, $, calls, server, advance, flush, key } = h;
  const results = [];
  const test = async (name, fn) => { await reset(); await fn(); results.push(name); };

  // The fake DOM has no query: walk the tree.
  const walk = (n, out = []) => { out.push(n); for (const c of n.children || []) walk(c, out); return out; };
  const find = (pred, root = $("con-body")) => walk(root).find(pred);
  const all = (pred, root = $("con-body")) => walk(root).filter(pred);
  const button = text => find(n => n.tagName === "button" && n.textContent === text);
  const area = () => find(n => n.tagName === "textarea");
  const gauge = name => find(n => n.className.startsWith("ngauge") && n.textContent.includes(name.padEnd(14)));
  const text = () => $("con-body").textContent;
  const goKey = $("con-go"), cancelKey = $("con-cancel");
  const type = async v => { const ta = area(); ta.value = v; ta.fire("input"); await flush(); };
  const countCalls = name => calls.filter(c => c[0] === name).length;

  // The host: the shared file holds 100 characters and the developer's 50. A role's total is its
  // instructions (5,000) plus each non-empty file under its heading.
  const files = { "": "s".repeat(100), developer: "d".repeat(50), reviewer: "" };
  const config = () => ({
    roles: [{ name: "developer", on: true, instructions_chars: 5000 + SHARED_HDR + 100 + DEV_HDR + 50 },
            { name: "reviewer", on: true, instructions_chars: 5000 + SHARED_HDR + 100 },
            { name: "researcher", on: false, optional: true }],
    notes: { project: "shop", limit: LIMIT, applies: "START", files: [{ name: "shop.md", role: null, chars: 100 }] },
    look: {},
  });
  let readOf = null;
  const prepared = (over) => Object.assign({ token: "tok1", expires: 120, path: "/home/u/projects/shop.md", changed: true, applies: "RESTART",
    totals: { developer: 6000, reviewer: 6000 }, limit: LIMIT, diff: [{ op: " ", text: "same" }, { op: "-", text: "old" }, { op: "+", text: "new" }] }, over);
  let onPrepare, onRun;
  function host() {
    server.config = config();
    server.handlers = {
      "config-read": async body => { readOf = body; return [200, { text: files[body.role] || "", chars: (files[body.role] || "").length, exists: body.role in files }]; },
      "config-prepare": async body => onPrepare(body),
      "config-run": async body => onRun(body),
    };
    onPrepare = async () => [200, prepared()];
    onRun = async () => [200, { ok: true, applies: "RESTART" }];
  }

  async function reset() {
    for (let i = 0; i < 6 && !$("scr-console").hidden; i++) {
      const discard = button("Discard");
      if (discard) discard.click(); else cancelKey.click();
      await flush();
    }
    assert.strictEqual($("scr-console").hidden, true, "back on the deck");
    host();
    calls.length = 0;
    await advance(2000);
  }
  async function openEditor() {
    $("svc-setup").click(); await flush();
    assert.ok(!find(n => n.tagName === "textarea"), "SETUP alone has no editor");
    button("Edit notes").click(); await flush();
    assert.ok(area(), "the editor loaded the shared file");
  }
  const pass = async () => { await advance(2700); };   // past the hold

  await test("gauge math, live", async () => {
    await openEditor();
    assert.deepStrictEqual(calls.find(c => c[0] === "config-read")[1], { kind: "notes", role: "" });
    assert.strictEqual(area().value, "s".repeat(100));
    const dev = 5000 + SHARED_HDR + 100 + DEV_HDR + 50;
    assert.ok(gauge("developer").textContent.includes(`${dev.toLocaleString("en-US")} / 20,000`), gauge("developer").textContent);
    assert.strictEqual(gauge("developer").className, "ngauge k-ok");
    assert.ok(!find(n => n.textContent.startsWith("  researcher")), "a role that is off has no gauge");
    // 100 -> 300 shared characters: every role grows by 200.
    await type("y".repeat(300));
    assert.ok(gauge("developer").textContent.includes(`${(dev + 200).toLocaleString("en-US")} / 20,000`), gauge("developer").textContent);
    assert.ok(gauge("reviewer").textContent.includes(`${(5000 + SHARED_HDR + 300).toLocaleString("en-US")} / 20,000`));
    // Emptied: the heading goes with the file.
    await type("");
    assert.ok(gauge("reviewer").textContent.includes("5,000 / 20,000"), gauge("reviewer").textContent);
    assert.ok(gauge("developer").textContent.includes(`${(5000 + DEV_HDR + 50).toLocaleString("en-US")} / 20,000`));
    // Characters, not UTF-16 units: an astral one counts once.
    await type("😀".repeat(10));
    assert.ok(gauge("reviewer").textContent.includes(`${(5000 + SHARED_HDR + 10).toLocaleString("en-US")} / 20,000`), gauge("reviewer").textContent);
    // The developer's own tab: only the developer's gauge follows its text.
    await type("😀");   // dirty, so it asks first
    button("DEVELOPER").click(); await flush();
    button("Discard").click(); await flush();
    assert.strictEqual(readOf.role, "developer");
    await type("z".repeat(80));
    assert.ok(gauge("developer").textContent.includes(`${(5000 + SHARED_HDR + 100 + DEV_HDR + 80).toLocaleString("en-US")} / 20,000`), gauge("developer").textContent);
    assert.ok(gauge("reviewer").textContent.includes(`${(5000 + SHARED_HDR + 100).toLocaleString("en-US")} / 20,000`), "reviewer does not read the developer's notes");
  });

  await test("amber near the limit, red over, SAVE dark while over", async () => {
    await openEditor();
    await type("a".repeat(2000));
    assert.strictEqual(goKey.disabled, false, "a change within the limit can be saved");
    // Exactly at the limit: developer = 5,115 + s = 20,000 for s = 14,885. Saved, not over.
    await type("a".repeat(14885));
    assert.ok(gauge("developer").textContent.includes("20,000 / 20,000"), gauge("developer").textContent);
    assert.strictEqual(gauge("developer").className, "ngauge k-warn", "amber at the limit");
    assert.strictEqual(goKey.disabled, false, "the limit itself is allowed");
    // One over for the developer; the reviewer, with no notes of its own, is still within it.
    await type("a".repeat(14886));
    assert.strictEqual(gauge("developer").className, "ngauge k-err");
    assert.ok(gauge("developer").textContent.includes("OVER by 1"));
    assert.strictEqual(gauge("reviewer").className, "ngauge k-warn");
    assert.strictEqual(goKey.disabled, true, "SAVE is dark while a role is over");
    assert.ok(text().includes("developer would be over 20,000 characters: cut 1 to save"));
    goKey.click(); await flush();
    assert.strictEqual(countCalls("config-prepare"), 0, "a dark SAVE sends nothing");
    await type("a".repeat(14885));
    assert.strictEqual(goKey.disabled, false);
    // Nothing typed, nothing to save.
    await type(files[""]);
    assert.strictEqual(goKey.disabled, true);
    assert.ok(text().includes("No changes yet."));
  });

  await test("diff is drawn as text", async () => {
    await openEditor();
    await type("hello");
    onPrepare = async () => [200, prepared({ diff: [
      { op: " ", text: "keep" }, { op: "-", text: "<img src=x onerror=alert(1)>" }, { op: "+", text: "<b>bold</b> ‮evil" },
      { op: "<script>", text: "odd op" }, { op: "+", text: "tab\there" }] })];
    goKey.click(); await flush();
    assert.deepStrictEqual(calls.find(c => c[0] === "config-prepare")[1], { kind: "notes", role: "", text: "hello" });
    assert.ok(text().includes("/home/u/projects/shop.md"), "the path is shown");
    assert.ok(text().includes("- <img src=x onerror=alert(1)>"));
    assert.ok(text().includes("+ <b>bold</b> ⟨U+202E⟩evil"), "bidi characters are made visible");
    assert.ok(text().includes("hidden or control character"));
    assert.ok(text().includes("? odd op"), "an unknown op is drawn as a plain line");
    const tags = new Set(all(() => true).map(n => n.tagName));
    for (const t of tags) assert.ok(["div", "p", "h5", "span", "button", "#text"].includes(t), `no ${t} element came out of the text`);
    const cls = n => n.className;
    assert.deepStrictEqual(all(n => cls(n).startsWith("nd ")).map(cls),
      ["nd nd-ctx", "nd nd-del", "nd nd-add", "nd nd-ctx", "nd nd-add"], "classes come from the fixed map");
    assert.ok(text().includes("developer") && text().includes("6,000 of 20,000"), "the host's totals");
    assert.ok(text().includes("Applies on restart"));
  });

  await test("confirm stays dark for the hold, then writes once", async () => {
    await openEditor();
    await type("hello");
    goKey.click(); await flush();
    assert.strictEqual(countCalls("config-run"), 0, "SAVE alone writes nothing");
    assert.strictEqual(goKey.textContent, "Confirm save");
    assert.strictEqual(goKey.disabled, true, "CONFIRM is dark at first");
    goKey.click(); await flush();
    await advance(2000);
    assert.strictEqual(goKey.disabled, true, "still dark inside the hold");
    goKey.click(); await flush();
    assert.strictEqual(countCalls("config-run"), 0);
    await advance(700);
    assert.strictEqual(goKey.disabled, false);
    goKey.click(); await flush();
    assert.deepStrictEqual(calls.filter(c => c[0] === "config-run").map(c => c[1]), [{ token: "tok1" }]);
    goKey.click(); await flush();
    assert.strictEqual(countCalls("config-run"), 1, "the token is used once");
    assert.ok(text().includes("SAVED · SERVICE DUE · RESTART TO APPLY"), text());
    assert.ok(countCalls("state") > 0, "the state is fetched again");
    assert.strictEqual(area().value, "hello");
    assert.strictEqual(goKey.disabled, true, "saved, so nothing to save");
  });

  await test("cancel drops the token; an old confirmation expires", async () => {
    await openEditor();
    await type("hello");
    goKey.click(); await flush(); await pass();
    cancelKey.click(); await flush();
    assert.strictEqual(area().value, "hello", "the text is kept");
    assert.strictEqual(countCalls("config-run"), 0);
    goKey.click(); await flush();                 // asks again
    assert.strictEqual(countCalls("config-prepare"), 2);
    await advance(121_000);
    assert.strictEqual(goKey.disabled, true);
    assert.ok(goKey.textContent.startsWith("Expired"));
    goKey.click(); await flush();
    assert.strictEqual(countCalls("config-run"), 0, "an expired confirmation sends nothing");
  });

  await test("changed on disk: say so, offer RELOAD", async () => {
    await openEditor();
    await type("mine");
    goKey.click(); await flush(); await pass();
    onRun = async () => [409, { error: "changed" }];
    files[""] = "theirs";
    goKey.click(); await flush();
    assert.ok(text().includes("changed on disk"), text());
    assert.strictEqual(goKey.textContent, "Reload");
    assert.strictEqual(goKey.disabled, true, "RELOAD drops the text, so it is dark for the hold too");
    await pass();
    goKey.click(); await flush();
    assert.strictEqual(area().value, "theirs", "RELOAD shows the file as it is now");
    assert.strictEqual(countCalls("config-read"), 2);
    // KEEP MY TEXT instead.
    await type("mine");
    goKey.click(); await flush(); await pass();
    goKey.click(); await flush();
    cancelKey.click(); await flush();
    assert.strictEqual(area().value, "mine");
  });

  await test("demo: a fixed refusal, nothing saved", async () => {
    await openEditor();
    await type("hello");
    onPrepare = async () => [403, { error: "this is a demo" }];
    goKey.click(); await flush();
    assert.ok(text().includes("DEMO · CHANGES DISABLED"));
    assert.ok(!text().includes("this is a demo"), "the fixed text, not the server's");
    assert.strictEqual(countCalls("config-run"), 0);
    assert.strictEqual(area().value, "hello");
  });

  await test("refusals reach the user as text", async () => {
    await openEditor();
    await type("hello");
    onPrepare = async () => [409, { error: "developer would exceed 20000 characters" }];
    goKey.click(); await flush();
    assert.ok(text().includes("developer would exceed 20000 characters"));
    onPrepare = async () => [400, { error: "<b>bad</b>" }];
    goKey.click(); await flush();
    assert.ok(text().includes("<b>bad</b>"));
    onPrepare = async () => [200, prepared({ changed: false })];
    goKey.click(); await flush();
    assert.ok(text().includes("NO CHANGE"));
    onPrepare = async () => [200, { nonsense: true }];
    goKey.click(); await flush();
    assert.ok(text().includes("not understood"));
    assert.strictEqual(countCalls("config-run"), 0);
    // A failed read.
    server.handlers["config-read"] = async () => [403, { error: "refused" }];
    button("DEVELOPER").click(); await flush();
    button("Discard").click(); await flush();
    assert.ok(text().includes("refused"));
    assert.ok(!area(), "no box to type in when the file could not be read");
  });

  await test("unsaved edits are asked about before they are lost", async () => {
    await openEditor();
    button("DEVELOPER").click(); await flush();     // nothing typed: switches at once
    assert.strictEqual(readOf.role, "developer");
    assert.strictEqual(area().value, "d".repeat(50));
    await type("dirty");
    assert.ok(button("DEVELOPER *"), "the tab says it has unsaved text");
    const reads = countCalls("config-read");
    button("SHARED").click(); await flush();
    assert.strictEqual(countCalls("config-read"), reads, "nothing was read yet");
    assert.ok(text().includes("Unsaved changes in DEVELOPER"));
    button("Keep editing").click(); await flush();
    assert.strictEqual(area().value, "dirty");
    assert.ok(!text().includes("Unsaved changes in DEVELOPER"));
    // Back asks too; Esc a second time puts the question away.
    cancelKey.click(); await flush();
    assert.ok(text().includes("Unsaved changes in DEVELOPER"));
    key("Escape"); await flush();
    assert.strictEqual(area().value, "dirty");
    assert.ok(!text().includes("Unsaved changes in DEVELOPER"));
    cancelKey.click(); await flush();
    button("Discard").click(); await flush();
    assert.ok(text().includes("Setup · effective configuration"), "back on SETUP");
    assert.strictEqual($("scr-console").hidden, false);
    cancelKey.click(); await flush();
    assert.strictEqual($("scr-console").hidden, true, "Close closes");
  });

  await test("a role already over the limit may be edited down, not up", async () => {
    // The developer's instructions are over on their own: 21,000 + notes. The host lets a change through that does not raise it.
    files[""] = "s".repeat(100); files.developer = "d".repeat(50);
    const c = config();
    c.roles[0].instructions_chars = 21000 + SHARED_HDR + 100 + DEV_HDR + 50;
    server.config = c;
    await advance(2500);
    await openEditor();
    assert.strictEqual(gauge("developer").className, "ngauge k-err", "red: it is over");
    await type("s".repeat(60));
    assert.strictEqual(goKey.disabled, false, "shorter: SAVE is allowed");
    assert.ok(text().includes("developer still over 20,000, but this change does not add to it"), text());
    await type("s".repeat(101));
    assert.strictEqual(goKey.disabled, true, "longer: SAVE is dark");
    assert.ok(text().includes("developer would be over 20,000 characters: cut"));
    await type("s".repeat(60));
    goKey.click(); await flush();
    assert.strictEqual(countCalls("config-prepare"), 1, "the shorter text is sent");
  });

  await test("no project, no editor", async () => {
    server.config = Object.assign(config(), { notes: { project: "(no project)", limit: LIMIT, files: [] } });
    await advance(2500);
    $("svc-setup").click(); await flush();
    assert.ok(!button("Edit notes"));
    assert.ok(text().includes("once the factory is bound to a project"));
  });

  server.config = null;
  server.handlers = {};
  await reset().catch(() => {});
  return results;
};
