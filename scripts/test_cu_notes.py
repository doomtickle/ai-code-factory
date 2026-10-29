"""Tests for the control unit's NOTES editor (doomtickle/passdesk-ops#186,
scripts/dashboard-ui/cu-notes.js): the file is in the second origin's allowlist and is
served like the other control-unit files, the page loads it as a plain script before cu.js,
its code draws text only and holds neither the page's key nor a request of its own, and its
behaviour (gauges, SAVE dark while over, the diff as text, the confirm hold, a file that
changed, the demo refusal, unsaved edits) under node (scripts/test_cu_notes.js via
scripts/test_cu_page.js).

  python3 -m unittest discover -s scripts -p 'test_cu_notes.py' -v
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import dashboard_demo
import dashboard_setup
import dashboard_term as dt
import test_dashboard_configwrite as cwt
from test_dashboard_controls import UI, Env

HERE = Path(__file__).resolve().parent
JS = (UI / "cu-notes.js").read_text(encoding="utf-8")


class AllowlistTests(unittest.TestCase):
    def test_script_is_in_the_allowlist(self):
        self.assertEqual(dt.STATIC.get("/cu-notes.js"), ("cu-notes.js", "text/javascript; charset=utf-8"))
        self.assertTrue((UI / "cu-notes.js").is_file())

    def test_served_on_the_second_origin_to_the_page_only(self):
        e = Env(self.addCleanup)
        script = {"Sec-Fetch-Dest": "script", "Sec-Fetch-Site": "same-origin"}
        resp, body = e.get("/cu-notes.js", **script)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/javascript; charset=utf-8")
        self.assertEqual(body.decode("utf-8"), JS)
        self.assertNotIn(e.key.encode(), body)
        self.assertEqual(e.get("/cu-notes.js", Cookie=None, **script)[0].status, 403, "the cookie is needed")
        self.assertEqual(e.get("/cu-notes.js", Host=f"127.0.0.1:{e.port}", **script)[0].status, 403, "only under the second origin's Host")
        self.assertEqual(e.get("/cu-notes.js.map", **script)[0].status, 404)

    def test_page_loads_it_before_cu_js_with_no_inline_script(self):
        html = (UI / "cu.html").read_text(encoding="utf-8")
        tags = re.findall(r'<script\b[^>]*>', html)
        self.assertEqual(tags, ['<script src="/cu-notes.js" defer>', '<script src="/cu.js" defer>'])
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html), "inline script")


class CodeTests(unittest.TestCase):
    def test_draws_text_only(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(sink, JS, sink)
        self.assertNotRegex(JS, r"\.(href|src|style|dataset|title)\b")
        self.assertNotRegex(JS, r"\bon[a-z]+\s*=")
        # Only fixed roles, a fixed state and a label built from a file name the editor itself made.
        self.assertEqual(sorted(re.findall(r"setAttribute\((.*)\);", JS)), sorted([
            '"role", "tablist"', '"role", "tab"', '"aria-selected", t === tab ? "true" : "false"',
            '"aria-label", `Notes: ${fileName(tab)}`', '"role", "alert"']))

    def test_classes_come_from_fixed_maps(self):
        self.assertEqual(sorted(re.findall(r"className = ([^;]+);", JS)), ["`ngauge ${GAUGE[lv]}`", "`nstatus ${cls}`"])
        self.assertIn('const GAUGE = { ok: "k-ok", warn: "k-warn", over: "k-err" };', JS)
        self.assertIn('const DIFF = { "+": ["nd nd-add", "+"], "-": ["nd nd-del", "-"], " ": ["nd nd-ctx", " "] };', JS)
        # A diff line's class is picked from the map by an op that matched one of its keys, never by the server's text.
        self.assertIn('const op = DIFF[r.op] ? r.op : "?";', JS)

    def test_holds_no_key_and_makes_no_request_of_its_own(self):
        for word in ("fetch(", "XMLHttpRequest", "WebSocket", "postMessage", "X-Factory-Control", "cu-key", "localStorage", "document.cookie", "location"):
            self.assertNotIn(word, JS, word)
        self.assertEqual(sorted(set(re.findall(r'd\.api\("([a-z-]+)"', JS))), ["config-prepare", "config-read", "config-run"])
        cu = (UI / "cu.js").read_text(encoding="utf-8")
        lent = cu[cu.index("window.CuNotes.create({"):cu.index("// ---------- start")]
        self.assertIsNone(re.search(r"\bkey\b", lent), "cu.js lends its request function, not its key")

    def test_a_request_needs_a_press_and_a_token(self):
        # config-run carries only the token prepare returned; text goes only to config-prepare.
        self.assertIn('d.api("config-run", { token })', JS)
        self.assertIn('d.api("config-prepare", { kind: "notes", role, text: sent })', JS)
        self.assertIn('d.api("config-read", { kind: "notes", role: t })', JS)

    def test_css_block_is_last_and_inline_free(self):
        css = (UI / "cu.css").read_text(encoding="utf-8")
        marker = "/* ---------- NOTES editor on SETUP"
        self.assertEqual(css.count(marker), 1)
        block = css[css.index(marker):]
        self.assertIsNone(re.search(r"https?://|url\(|@import", block))


HAVE_NODE = shutil.which("node") is not None


def js_totals(cases):
    """cu-notes.js's own arithmetic (CuNotes.projected) on [base, project, tab, role, saved, new] rows."""
    code = ('global.window = {}; require(process.argv[1]); const P = window.CuNotes.projected;'
            ' console.log(JSON.stringify(JSON.parse(process.argv[2]).map(a => P(...a))));')
    out = subprocess.run(["node", "-e", code, str(UI / "cu-notes.js"), json.dumps(cases)],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(HAVE_NODE, "no node")
class EndToEndTests(cwt.Base):
    """The real control endpoints, driven over HTTP with exactly the bodies cu-notes.js sends, checking the
    fields it parses and that its gauge arithmetic gives the totals the host reports."""

    def post(self, name, body, want=200):
        status, out, _ = self.env.post(name, body)
        self.assertEqual(status, want, out)
        return out

    def bases(self):
        """What the gauges start from: each running role's instructions_chars in the unit's state."""
        cfg = self.post("state", {})["config"]
        self.assertEqual((cfg["notes"]["project"], cfg["notes"]["limit"]), ("proj", 20000))
        out = {}
        for r in cfg["roles"]:
            if r.get("instructions_chars") is not None:
                self.assertIsInstance(r["instructions_chars"], int)
                out[r["name"]] = r["instructions_chars"]
        self.assertEqual(sorted(out), ["developer", "reviewer"])
        return out

    def gauge_matches(self, tab, text, saved_chars, totals, bases):
        roles = sorted(totals)
        rows = [[bases[r], "proj", tab, r, saved_chars, len(text)] for r in roles]   # Python len() counts code points, as cp() does
        self.assertEqual(dict(zip(roles, js_totals(rows))), totals)

    def test_the_limit_the_box_takes_is_the_limit_the_host_enforces(self):
        self.assertEqual(int(re.search(r"const TEXT_MAX = (\d+);", JS).group(1)), dashboard_setup.NOTES_LIMIT)
        self.assertEqual(int(re.search(r"const DEFAULT_LIMIT = (\d+);", JS).group(1)), dashboard_setup.NOTES_LIMIT)
        self.assertIn("over 20,000", self.post("config-prepare", {"kind": "notes", "role": "", "text": "x" * 20001}, 400)["error"])

    def test_read_prepare_run_with_what_the_editor_parses(self):
        bases = self.bases()
        read = self.post("config-read", {"kind": "notes", "role": ""})
        self.assertEqual(read, {"text": "", "chars": 0, "exists": False})
        text = "Run tests with `npm test`.\nCafé \U0001F600 <b>not html</b>\n"
        out = self.post("config-prepare", {"kind": "notes", "role": "", "text": text})
        self.assertIsInstance(out["token"], str)
        self.assertEqual((out["expires"], out["limit"], out["applies"], out["changed"]), (120, 20000, "RESTART", True))
        self.assertIsInstance(out["path"], str)
        self.assertEqual(set(out["totals"]), {"developer", "reviewer"})
        self.assertTrue(out["diff"] and all(set(d) >= {"op", "text"} and d["op"] in " +-" for d in out["diff"]))
        self.assertIn({"op": "+", "text": "Café \U0001F600 <b>not html</b>"}, out["diff"])   # text, as sent
        self.gauge_matches("", text, 0, out["totals"], bases)
        self.assertEqual(self.post("config-run", {"token": out["token"]}), {"ok": True, "applies": "RESTART"})
        # Polled state agrees with the totals prepare reported, so the editor's stand-in can be dropped.
        self.assertEqual(self.bases(), out["totals"])
        again = self.post("config-read", {"kind": "notes", "role": ""})
        self.assertEqual(again, {"text": text, "chars": len(text), "exists": True})
        # A role's own tab: only that role's total moves.
        bases = self.bases()
        mine = "Docs in British English."
        out = self.post("config-prepare", {"kind": "notes", "role": "developer", "text": mine})
        self.gauge_matches("developer", mine, 0, out["totals"], bases)
        self.assertEqual(out["totals"]["reviewer"], bases["reviewer"])
        self.post("config-run", {"token": out["token"]})
        self.assertEqual(self.bases(), out["totals"])
        # Editing the saved file: the gauge subtracts what the file held.
        longer = mine + " Commit messages in the imperative."
        out = self.post("config-prepare", {"kind": "notes", "role": "developer", "text": longer})
        self.gauge_matches("developer", longer, len(mine), out["totals"], self.bases())

    def test_astral_characters_count_once_on_both_sides(self):
        bases = self.bases()
        text = "\U0001F600" * 30 + "e\u0301" + "\U0001F468\u200d\U0001F469\n"
        self.assertGreater(len(text.encode("utf-16-le")) // 2, len(text))   # JS .length would count more
        out = self.post("config-prepare", {"kind": "notes", "role": "", "text": text})
        self.gauge_matches("", text, 0, out["totals"], bases)
        self.post("config-run", {"token": out["token"]})
        self.assertEqual(self.post("config-read", {"kind": "notes", "role": ""})["chars"], len(text))
        self.assertEqual(self.bases(), out["totals"])
        # 20,000 characters (25,000 UTF-16 units) pass the file limit, so the refusal is a role's total (409); one more is the file limit (400).
        full = "\U0001F600" * 5000 + "x" * 15000
        self.assertEqual(len(full), 20000)
        self.assertIn("over 20,000", self.post("config-prepare", {"kind": "notes", "role": "", "text": full + "x"}, 400)["error"])
        self.assertNotIn("the file", self.post("config-prepare", {"kind": "notes", "role": "", "text": full}, 409)["error"])

    def test_unchanged_text_reports_no_change(self):
        self.write(self.notes, "same\n")
        out = self.post("config-prepare", {"kind": "notes", "role": "", "text": "same\n"})
        self.assertIs(out["changed"], False)

    def test_changed_on_disk_is_a_409_with_the_error_the_editor_matches(self):
        out = self.post("config-prepare", {"kind": "notes", "role": "", "text": "mine\n"})
        self.write(self.notes, "edited in a terminal\n")
        self.assertEqual(self.post("config-run", {"token": out["token"]}, 409), {"error": "changed"})
        self.assertEqual(self.notes.read_text(), "edited in a terminal\n")
        self.assertIn('error === "changed"', JS)
        # The token was spent: a second press gets the 403 the editor shows as "no longer valid".
        self.assertIsInstance(self.post("config-run", {"token": out["token"]}, 403)["error"], str)

    def test_over_the_limit_is_a_409_and_the_gauge_says_so_first(self):
        self.write(self.root / "roles" / "reviewer" / "instructions.md", "r" * 15000)
        bases = self.bases()
        text = "n" * 6000
        rows = [[bases["reviewer"], "proj", "", "reviewer", 0, len(text)]]
        self.assertGreater(js_totals(rows)[0], 20000)   # SAVE is dark, so the editor would not have asked
        out = self.post("config-prepare", {"kind": "notes", "role": "", "text": text}, 409)
        self.assertIn("reviewer", out["error"])
        self.assertFalse(self.notes.exists())

    def test_the_editor_and_the_host_agree_on_editing_down(self):
        self.write(self.root / "roles" / "reviewer" / "instructions.md", "r" * 15000)
        self.write(self.notes, "s" * 6000)
        bases = self.bases()
        self.assertGreater(bases["reviewer"], 20000)
        for n, allowed in ((5500, True), (6000, True), (6500, False)):
            text = "s" * n
            total = js_totals([[bases["reviewer"], "proj", "", "reviewer", 6000, n]])[0]
            editor_allows = not (total > 20000 and total > bases["reviewer"])
            self.assertEqual(editor_allows, allowed, n)
            status, out, _ = self.env.post("config-prepare", {"kind": "notes", "role": "", "text": text})
            self.assertEqual(status == 200, allowed, (n, out))

    def test_demo_refuses_with_a_403_the_editor_shows_as_its_own_fixed_text(self):
        self.assertTrue(dashboard_demo.REFUSED.startswith("demo mode"))
        # The editor goes by the status, never by the host's wording.
        self.assertIn("res.status === 403", JS)
        self.assertNotIn("demo mode", JS)
        self.assertIn('const DEMO = "DEMO · CHANGES DISABLED";', JS)


@unittest.skipIf(shutil.which("node") is None, "no node")
class ScriptTests(unittest.TestCase):
    def test_editor_under_node(self):
        proc = subprocess.run(["node", str(HERE / "test_cu_page.js"), str(UI / "cu.js")],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = next(x for x in proc.stdout.splitlines() if x.startswith("NOTES "))
        self.assertEqual(json.loads(line[len("NOTES "):]), [
            "gauge math, live", "amber near the limit, red over, SAVE dark while over", "diff is drawn as text",
            "confirm stays dark for the hold, then writes once", "cancel drops the token; an old confirmation expires",
            "changed on disk: say so, offer RELOAD", "demo: a fixed refusal, nothing saved", "refusals reach the user as text",
            "unsaved edits are asked about before they are lost", "a role already over the limit may be edited down, not up",
            "astral characters count once, as the host counts them", "no project, no editor"])


if __name__ == "__main__":
    unittest.main()
