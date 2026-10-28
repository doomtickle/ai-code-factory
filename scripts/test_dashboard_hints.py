"""FIRST SHIFT and the start-up hints in /api/state (doomtickle/passdesk-ops#181): the state view
computes the hints `factory start` prints, off a fake `sbx` on a private PATH (no real sbx), cached,
bounded, and display only."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard as d
import dashboard_demo as demo
import dashboard_hints as dh
from factory import fingerprints as fp
from factory import hints as factory_hints
from factory.context import Paths
from test_dashboard import Fixture, UI

LOGIN = "(global)  oauth  anthropic  configured\n"
FAKE_SBX = """#!/bin/sh
echo "$@" >> "$SBX_DIR/calls"
[ -e "$SBX_DIR/fail" ] && exit 3
[ -e "$SBX_DIR/slow" ] && sleep 30
case "$*" in
  "secret ls") cat "$SBX_DIR/secrets" 2>/dev/null;;
  "ls -q") cat "$SBX_DIR/names" 2>/dev/null;;
  *) echo "unexpected: $*" >> "$SBX_DIR/bad"; exit 9;;
esac
exit 0
"""


class HintsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.fx = Fixture(base)
        self.paths = Paths(str(base), str(base / "config"), str(self.fx.state))
        self.sbx_dir = base / "sbxdir"
        self.sbx_dir.mkdir()
        bin_dir = base / "bin"
        bin_dir.mkdir()
        sbx = bin_dir / "sbx"
        sbx.write_text(FAKE_SBX)
        sbx.chmod(sbx.stat().st_mode | stat.S_IXUSR)
        self.environ = {"PATH": f"{bin_dir}:/usr/bin:/bin", "SBX_DIR": str(self.sbx_dir), "HOME": str(base)}
        self.source = dh.Hints(self.paths, self.environ)
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj", paths=self.paths, hint_source=self.source)
        self.now = self.fx.now

    def sbx(self, secrets="", names=""):
        (self.sbx_dir / "secrets").write_text(secrets)
        (self.sbx_dir / "names").write_text(names)

    def calls(self):
        try:
            return (self.sbx_dir / "calls").read_text().splitlines()
        except OSError:
            return []

    def hints(self, now=None, refresh=True):
        now = self.now if now is None else now
        if refresh:
            self.source.refresh(now)
        return self.board.snapshot(now)["hints"]


class StateTests(HintsBase):
    ROLES = "factory-broker\nfactory-coordinator\nfactory-developer\nfactory-reviewer\n"

    def test_no_login_is_first_shift_login(self):
        self.sbx(secrets="", names="factory-coordinator\n")
        self.assertEqual(self.hints(), [{"code": "login", "text":
                         "FIRST SHIFT · 1. POWER ON and type /login · 2. press RESTART"}])

    def test_login_but_no_roles_is_first_shift_roles(self):
        self.sbx(secrets=LOGIN, names="factory-coordinator\n")
        out = self.hints()
        self.assertEqual([h["code"] for h in out], ["roles"])
        self.assertIn("role sandboxes are not created yet", out[0]["text"])
        self.assertIn("press RESTART", out[0]["text"])

    def test_one_role_missing_still_counts(self):
        self.sbx(secrets=LOGIN, names="factory-coordinator\nfactory-developer\n")
        self.assertEqual([h["code"] for h in self.hints()], ["roles"])

    def test_login_and_roles_and_nothing_stale_is_quiet(self):
        self.sbx(secrets=LOGIN, names=self.ROLES)
        self.assertEqual(self.hints(), [])

    def test_stale_sandboxes_come_from_the_service_lamp_not_from_sbx(self):
        (self.fx.roles / "developer" / "kit").mkdir()
        (self.fx.roles / "developer" / "kit" / "spec.yaml").write_text("name: x\n")
        fp.record_sandbox_fingerprint(self.paths, "factory-developer")
        (self.fx.roles / "developer" / "kit" / "spec.yaml").write_text("name: changed\n")
        self.sbx(secrets=LOGIN, names=self.ROLES)
        out = self.hints()
        self.assertEqual(out, [{"code": "stale", "text": "out of date (made from an older image or kit): "
                                "factory-developer; run 'factory update' to recreate them"}])
        self.assertFalse(dh.first_shift(out))
        self.assertEqual(self.board.service(self.now)["lines"], ["factory-developer: kit changed"])
        self.assertNotIn("stale", self.board.service(self.now))

    def test_other_hints_follow_and_first_shift_still_leads_the_page(self):
        (Path(self.paths.config)).mkdir()
        (Path(self.paths.config) / "policy.toml").write_text('[git]\nrepos = ["acme/app"]\n')
        self.sbx(secrets=LOGIN, names=self.ROLES)
        out = self.hints()
        self.assertEqual([h["code"] for h in out], ["github"])
        self.assertIn("no GitHub token is stored: run 'factory secret save github'", out[0]["text"])
        self.sbx(secrets="", names=self.ROLES)
        self.assertEqual([h["code"] for h in self.hints(self.now + dh.HINTS_TTL)], ["login", "github"])

    def test_the_texts_are_the_ones_factory_start_prints(self):
        out = factory_hints.collect(self._ctx(secrets=""), stale=[])
        self.assertEqual(out[0][0], "login")
        self.assertIn("first time: type /login in the Coordinator", out[0][1])

    def _ctx(self, secrets):
        self.sbx(secrets=secrets)
        return dh.Probe(self.paths, self.environ)

    def test_keychain_checks_are_not_made(self):
        (Path(self.paths.config)).mkdir()
        (Path(self.paths.config) / "policy.toml").write_text(
            '[gitlab]\nurl = "https://gitlab.example.com"\n[packages]\nnpm_private_registry = "https://npm.example.com/"\n')
        self.sbx(secrets=LOGIN, names=self.ROLES)
        with mock.patch.object(factory_hints.keychain, "has", side_effect=AssertionError("keychain asked")):
            self.assertEqual(self.hints(), [])

    def test_the_demo_and_a_board_without_a_source_show_none(self):
        self.sbx(secrets="", names="")
        self.assertEqual(d.Dashboard(self.fx.state, self.fx.roles, "proj", paths=self.paths).snapshot(self.now)["hints"], [])
        with tempfile.TemporaryDirectory() as tmp:
            demo.build(Path(tmp))
            board = d.Dashboard(Path(tmp), d.DOCS_ROOT / "roles", demo.PROJECT, demo=True, hint_source=self.source)
            self.assertEqual(board.snapshot()["hints"], [])
            self.assertFalse(board.first_shift(self.now))
        self.assertEqual(self.calls(), [])

    def test_first_shift_flag_for_the_control_unit(self):
        self.sbx(secrets="", names="")
        self.hints()
        self.assertTrue(self.board.first_shift(self.now))
        self.sbx(secrets=LOGIN, names=self.ROLES)
        self.source.refresh(self.now + dh.HINTS_TTL)
        self.assertFalse(self.board.first_shift(self.now + dh.HINTS_TTL))

    def test_text_is_one_printable_line(self):
        out = dh.compose(["factory-x\x1b[31m\nevil"], [])
        self.assertNotRegex(out[0]["text"], r"[^\x20-\x7e]")
        self.assertEqual(json.loads(json.dumps(out)), out)


class CacheAndBoundsTests(HintsBase):
    def test_sbx_runs_only_the_two_fixed_forms_once_per_refresh_and_not_per_poll(self):
        self.sbx(secrets=LOGIN, names="factory-developer\nfactory-reviewer\n")
        self.hints()
        self.assertEqual(sorted(self.calls()), ["ls -q", "secret ls"])
        for i in range(5):  # polls inside the TTL read the cache and start nothing
            self.board.snapshot(self.now + i)
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse((self.sbx_dir / "bad").exists())

    def test_a_poll_never_waits_for_sbx(self):
        self.sbx(secrets=LOGIN, names="")
        started = []
        with mock.patch.object(dh.threading, "Thread") as thread:
            thread.side_effect = lambda **kw: started.append(kw) or mock.Mock(is_alive=lambda: False)
            self.assertEqual(self.board.snapshot(self.now)["hints"], [])  # nothing computed yet
        self.assertEqual(len(started), 1)
        self.assertEqual(self.calls(), [])

    def test_a_stale_cache_refreshes_in_the_background(self):
        self.sbx(secrets=LOGIN, names="")
        self.board.snapshot(self.now)
        self.source._thread.join(10)
        self.assertEqual([h["code"] for h in self.board.snapshot(self.now + 1)["hints"]], ["roles"])

    def test_a_failing_sbx_keeps_the_last_answer(self):
        self.sbx(secrets=LOGIN, names="")
        self.assertEqual([h["code"] for h in self.hints()], ["roles"])
        (self.sbx_dir / "fail").write_text("")
        self.assertEqual([h["code"] for h in self.hints(self.now + dh.HINTS_TTL)], ["roles"])

    def test_a_failing_sbx_never_shows_first_shift_first(self):
        (self.sbx_dir / "fail").write_text("")
        self.assertEqual(self.hints(), [])

    def test_a_slow_sbx_is_cut_off(self):
        (self.sbx_dir / "slow").write_text("")
        with mock.patch.object(dh, "PROBE_TIMEOUT", 0.3):
            self.assertEqual(self.hints(), [])
        self.assertEqual(self.calls()[:1], ["secret ls"])

    def test_the_probe_refuses_any_other_command(self):
        probe = dh.Probe(self.paths, self.environ)
        for argv in (["sbx", "rm", "factory-broker"], ["sbx", "ls"], ["rm", "-rf", "/"], ["sbx", "secret", "ls", "-x"]):
            got = probe.capture(argv)
            self.assertEqual(got.status, 126, argv)
        self.assertEqual(self.calls(), [])
        self.assertTrue(probe.failed)

    def test_only_one_refresh_runs_at_a_time(self):
        self.sbx(secrets=LOGIN, names="")
        self.source._run.acquire()
        try:
            self.source.refresh(self.now)
        finally:
            self.source._run.release()
        self.assertEqual(self.calls(), [])

    def test_the_hints_are_read_outside_the_board_lock(self):
        self.sbx(secrets=LOGIN, names="")
        self.source.refresh(self.now)
        seen = []
        real = self.source.get
        self.source.get = lambda now: seen.append(self.board.lock.locked()) or real(now)
        self.board.snapshot(self.now)
        self.assertEqual(seen, [False])


class PageTests(unittest.TestCase):
    def test_the_page_draws_the_banner_as_text_and_the_unit_blinks_restart(self):
        app = (UI / "app.js").read_text()
        self.assertIn("state.hints", app)
        self.assertIn('$("crt-shift")', app)
        self.assertNotIn("innerHTML", app[app.index("function renderShift"):app.index("function renderTerm")])
        self.assertIn('id="crt-shift"', (UI / "index.html").read_text())
        cu = (UI / "cu.js").read_text()
        self.assertIn("st.first_shift", cu)
        self.assertIn('run || first ? " a blink" : due === "restart"', cu)


if __name__ == "__main__":
    unittest.main()
