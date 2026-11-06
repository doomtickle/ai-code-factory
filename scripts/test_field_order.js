// The Thoughtfield's plain words and label placement (dashboard-ui/field-order.js), pure functions run under node.
//   node scripts/test_field_order.js        (run by test_field_order.py)
"use strict";
const assert = require("node:assert");
const FO = require("./dashboard-ui/field-order.js");

const job = (id, role, status, verdict, start, end, extra) => Object.assign({ id, lane: role + "-lane1", role, order: "ord", label: id, task: id, status, verdict, start, end, i: 0 }, extra);

// steps are numbered by start time, whatever order they arrive in; a held write is the step after the last job
{
  const a = job("a", "developer", "done", "", 600, 610), b = job("b", "reviewer", "done", "revise", 615, 620), c = job("c", "developer", "running", "", 625, null);
  const steps = FO.number([c, a, b], [{ id: "g1", decided: "" }]);
  assert.deepStrictEqual(steps.map(s => s.n), [1, 2, 3, 4]);
  assert.deepStrictEqual(steps.map(s => s.job ? s.job.id : "card"), ["a", "b", "c", "card"]);
  assert.deepStrictEqual(steps.map(s => s.tone), ["done", "rev", "run", "wait"]);
  assert.strictEqual(FO.number([b, a], [])[0].job.id, "a", "oldest first");
}

// one plain state per job, only from the fixed tones
{
  const t = (st, v) => FO.jobTone(job("x", v ? "reviewer" : "developer", st, v, 1, 2));
  assert.strictEqual(t("running", ""), "run");
  assert.strictEqual(t("done", ""), "done");
  assert.strictEqual(t("done", "approved"), "ok");
  assert.strictEqual(t("done", "revise"), "rev");
  for (const s of ["failed", "cancelled", "interrupted"]) assert.strictEqual(t(s, ""), "bad", s);
  assert.strictEqual(t("<img src=x onerror=1>", ""), "done", "an odd status is only done, never a class");
  assert.strictEqual(FO.cardTone({ decided: "" }), "wait");
  assert.strictEqual(FO.cardTone({ decided: "approve" }), "ok");
  assert.strictEqual(FO.cardTone({ decided: "decline" }), "rev");
  assert.deepStrictEqual(Object.keys(FO.TONES).sort(), ["bad", "done", "ok", "rev", "run", "wait"]);
}

// the order's one-line state comes from its newest step; a held write beats everything
{
  const word = (jobs, cards) => FO.orderWord(FO.number(jobs, cards)).word;
  const dev = (st, s, e) => job("d" + s, "developer", st, "", s, e), rev = (st, v, s, e) => job("r" + s, "reviewer", st, v, s, e);
  assert.strictEqual(word([dev("running", 1, null)]), "Working");
  assert.strictEqual(word([dev("done", 1, 2), rev("running", "", 3, null)]), "Reviewing");
  assert.strictEqual(word([dev("done", 1, 2), rev("done", "revise", 3, 4)]), "Changes requested");
  assert.strictEqual(word([dev("done", 1, 2), rev("done", "revise", 3, 4), dev("running", 5, null)]), "Fixing");
  assert.strictEqual(word([dev("done", 1, 2), rev("done", "approved", 3, 4)]), "Approved");
  assert.strictEqual(word([dev("failed", 1, 2)]), "Failed");
  assert.strictEqual(word([dev("cancelled", 1, 2)]), "Cancelled");
  assert.strictEqual(word([dev("done", 1, 2)]), "Done");
  assert.strictEqual(word([dev("done", 1, 2), rev("done", "approved", 3, 4)], [{ id: "g", decided: "" }]), "Waiting for you");
  assert.strictEqual(FO.orderWord(FO.number([rev("running", "", 3, null)], [{ id: "g", decided: "" }])).tone, "wait");
  // only the newest job counts: an older failure is history
  assert.strictEqual(word([dev("failed", 1, 2), dev("done", 3, 4)]), "Done");
  assert.strictEqual(FO.orderWord([]).word, "");
}

// names: a branch, or for a job that stands alone its own words; cut to fit
{
  const own = FO.number([job("20261106-f01", "developer", "done", "", 1, 2, { label: "Tidy packaging metadata" })], []);
  assert.strictEqual(FO.orderName("20261106-f01", "Tidy packaging metadata", own), "Tidy packaging metadata");
  assert.strictEqual(FO.orderName("rate-limit", "Add rate limit", FO.number([job("j1", "developer", "done", "", 1, 2)], [])), "rate-limit");
  assert.ok(FO.orderName("x", "y".repeat(60), FO.number([job("x", "developer", "done", "", 1, 2)], [])).length <= 26);
  assert.strictEqual(FO.cut("abcdef", 4), "abc…");
}

// the queue for labels: waiting, running, failed, then the rest newest first
{
  const r = FO.rank([{ id: "old", tone: "ok", newest: 1 }, { id: "new", tone: "done", newest: 9 }, { id: "bad", tone: "bad", newest: 2 }, { id: "run", tone: "run", newest: 3 }, { id: "wait", tone: "wait", newest: 0 }]);
  assert.deepStrictEqual(r.map(o => o.id), ["wait", "run", "bad", "new", "old"]);
  assert.deepStrictEqual(r.map(o => o.prio), [0, 1, 2, 3, 4]);
}

// the hover history: oldest first, a row for each start and end, the hovered step marked
{
  const a = job("a", "developer", "done", "", 615, 621), b = job("b", "reviewer", "running", "", 622, null, { lane: "reviewer-lane2" }), steps = FO.number([a, b], []);
  const h = FO.history(steps, 1);
  assert.deepStrictEqual(h.rows.map(r => r.t), [615, 621, 622], "time order, oldest first");
  assert.deepStrictEqual(h.rows.map(r => r.what), ["work started", "finished", "review started"]);
  assert.deepStrictEqual(h.rows.map(r => r.cur), [false, false, true], "only the hovered step is marked");
  assert.strictEqual(h.rows[2].lane, "reviewer-lane2");
  const done = FO.history(FO.number([a, job("c", "reviewer", "done", "revise", 622, 628)], []), 1).rows.map(r => r.what);
  assert.ok(done.includes("asked for changes"), done.join());
  assert.ok(FO.history(FO.number([job("c", "reviewer", "done", "approved", 622, 628)], []), 0).rows.some(r => r.what === "approved"));
  assert.ok(FO.history(FO.number([job("c", "developer", "failed", "", 622, 628)], []), 0).rows.some(r => r.what === "failed"));
  const many = FO.number(Array.from({ length: 12 }, (_, k) => job("m" + k, "developer", "done", "", 100 + k * 10, 105 + k * 10)), []);
  const big = FO.history(many, 11);
  assert.ok(big.rows.some(r => r.cur), "the hovered step stays in a long history");
  assert.strictEqual(big.before + big.after + big.rows.filter(r => !r.end).length, 12, "what is cut is counted");
  assert.strictEqual(FO.hhmm(615), "10:15");
  assert.strictEqual(FO.hhmm(-20), "23:40");
}

// label placement: clear of crystals and each other; running and waiting orders are never the ones dropped
{
  const env = (boxes, over) => Object.assign({ W: 800, top: 60, rimAt: () => 520, cx: 400, cy: 260, boxes, blocked: r => r[0] < 560 && r[2] > 240 && r[1] < 400 }, over || {});
  const box = (x, y, r) => [x - r, y - r, x + r, y + r];
  const item = (id, x, y, force) => ({ id, w: 150, h: 26, anchors: [{ x, y, r: 20 }], force: !!force });
  const overlap = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
  const items = [item("a", 120, 120, true), item("b", 120, 200), item("c", 700, 120, true), item("d", 700, 220)];
  const boxes = items.map(i => box(i.anchors[0].x, i.anchors[0].y, 20));
  const p = FO.placeLabels(items, env(boxes));
  assert.deepStrictEqual(p.hidden, []);
  const rects = items.map(i => { const at = p.at.get(i.id); return [at.x, at.y, at.x + i.w, at.y + i.h]; });
  rects.forEach((r, i) => {
    boxes.forEach(b => assert.ok(!overlap(r, b), "label " + i + " covers a crystal"));
    assert.ok(!env(boxes).blocked(r), "label " + i + " covers the witness");
    assert.ok(r[0] >= 0 && r[2] <= 800 && r[1] >= 60 && r[3] <= 520, "label " + i + " is on the sky");
    rects.slice(i + 1).forEach(o => assert.ok(!overlap(r, o), "two labels overlap"));
  });
  // a crowd: more labels than room. The ones dropped are the lowest ranked; every forced one is placed
  const crowd = Array.from({ length: 40 }, (_, k) => item("o" + k, 60 + (k % 10) * 75, 80 + Math.floor(k / 10) * 90, k < 3 || k === 30));
  const ranked = FO.rank(crowd.map(c => Object.assign(c, { tone: c.force ? "run" : "done", newest: 100 - +c.id.slice(1) })));
  const q = FO.placeLabels(ranked, env(crowd.map(c => box(c.anchors[0].x, c.anchors[0].y, 20))));
  assert.ok(q.hidden.length > 0 && q.at.size > 0, "some placed, some hidden");
  ["o0", "o1", "o2", "o30"].forEach(id => assert.ok(q.at.has(id), id + " is running and keeps its label"));
  const firstHidden = ranked.findIndex(i => q.hidden.includes(i.id));
  ranked.slice(firstHidden).forEach(i => assert.ok(i.force || q.hidden.includes(i.id), "after the first dropped label, only forced ones are shown: " + i.id));
  // no room at all (a phone): a forced label is still placed, an ordinary one is dropped
  const tiny = FO.placeLabels([item("w", 200, 100, true), item("x", 200, 160)], { W: 220, top: 60, rimAt: () => 300, cx: 110, cy: 160, boxes: [box(200, 100, 20), box(200, 160, 20)], blocked: () => true });
  assert.ok(tiny.at.has("w"), "a waiting or running order keeps a label");
  assert.ok(tiny.hidden.includes("x"));
}
console.log("field order checks passed");
