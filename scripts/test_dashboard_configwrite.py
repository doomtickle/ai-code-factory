"""Tests for the control unit's config write (scripts/dashboard_configwrite.py and the
config-read, config-prepare and config-run controls, doomtickle/passdesk-ops#185, #186):
which keys and values a role override takes, the validation `factory start` runs, in-place
updates that keep comments and order, notes limits, symlinked and non-regular targets, the
digest check against a concurrent edit, the token (single use, expiry, bound to "config"),
the body caps, the header checks on the new routes, and the SERVICE DUE lamp afterwards.

  python3 -m unittest discover -s scripts -p 'test_dashboard_configwrite.py' -v
"""

import hashlib
import json
import os
import re
import stat
import unittest
from pathlib import Path

import dashboard_configwrite as cw
import dashboard_setup
import dashboard_term as dt
from factory import roles
from dashboard_service import Paths
from test_dashboard_controls import Env

DEV_CONF = ('TEMPLATE="developer"\nWORKSPACE="rw"\nLANES="3"\nBROKER_TOOLS="git_fetch git_push"\n'
            'MODEL="claude-sonnet-5"\nMODELS="opus"\nMAX_TURNS="200"\n')
REV_CONF = 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nLANES="1"\nBROKER_TOOLS=""\nMODEL="opus"\n'


class Base(unittest.TestCase):
    serve = True

    def setUp(self):
        self.env = Env(self.addCleanup, serve=self.serve)
        self.root = Path(self.env.tmp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.paths = Paths(str(self.root), str(self.config), str(self.env.fx.state))
        self.env.board.paths = self.paths
        self.write(self.root / "broker" / "tools.txt", "# tools\ngit_fetch\ngit_push\nweb_fetch\n")
        for name, conf in (("developer", DEV_CONF), ("reviewer", REV_CONF)):
            self.write(self.root / "roles" / name / "role.conf", conf)
            self.write(self.root / "roles" / name / "kit" / "spec.yaml", "name: x\n")
            self.write(self.root / "roles" / name / "instructions.md", "x" * 1000)
        self.override = self.config / "roles" / "developer.conf"
        self.notes = self.config / "projects" / "proj.md"
        self.dev_notes = self.config / "projects" / "proj.developer.md"

    def write(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def api(self, name, body, want=200):
        code, out = self.env.controls.api(name, body)
        self.assertEqual(code, want, out)
        return out

    def prepare_role(self, set_=None, unset=None, role="developer", want=200):
        return self.api("config-prepare", {"kind": "role", "role": role, "set": set_ or {}, "unset": unset or []},
                        want)

    def prepare_notes(self, text, role="", want=200):
        return self.api("config-prepare", {"kind": "notes", "role": role, "text": text}, want)

    def run_token(self, token, want=200):
        return self.api("config-run", {"token": token}, want)


class RoleOverrideTests(Base):
    serve = False

    def test_a_new_override_is_written_0600_in_a_0700_folder(self):
        out = self.prepare_role({"LANES": "4", "EFFORT": "high"})
        self.assertEqual(out["expires"], 120)
        self.assertTrue(out["changed"])
        self.assertEqual(out["applies"], "RESTART")
        self.assertTrue(out["path"].endswith("config/roles/developer.conf"))
        self.assertEqual(out["diff"], [{"op": "+", "text": 'EFFORT="high"'}, {"op": "+", "text": 'LANES="4"'}])
        self.assertFalse(self.override.exists())  # prepare writes nothing
        self.assertEqual(self.run_token(out["token"]), {"ok": True, "applies": "RESTART"})
        self.assertEqual(self.override.read_text(), 'EFFORT="high"\nLANES="4"\n')
        self.assertEqual(stat.S_IMODE(self.override.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.override.parent.stat().st_mode), 0o700)
        self.assertEqual([n for n in os.listdir(self.override.parent)], ["developer.conf"])  # no temp file left

    def test_comments_and_order_are_kept_keys_updated_in_place_appended_and_removed(self):
        self.write(self.override, '# my tuning\nMODEL="opus"\n\n# lanes\nLANES="2"\nEFFORT="low"\n')
        out = self.prepare_role({"LANES": "5", "MAX_TURNS": "50"}, ["EFFORT"])
        ops = [(d["op"], d["text"]) for d in out["diff"]]
        self.assertIn(("-", 'LANES="2"'), ops)
        self.assertIn(("+", 'LANES="5"'), ops)
        self.assertIn(("-", 'EFFORT="low"'), ops)
        self.assertIn(("+", 'MAX_TURNS="50"'), ops)
        self.run_token(out["token"])
        self.assertEqual(self.override.read_text(), '# my tuning\nMODEL="opus"\n\n# lanes\nLANES="5"\nMAX_TURNS="50"\n')
        keys = {k["key"]: k for k in dashboard_setup._role(self.paths, "developer", True)["keys"]}
        self.assertEqual((keys["LANES"]["value"], keys["LANES"]["source"]), ("5", "LOCAL"))

    def test_an_unchanged_override_reports_no_change_and_writes_nothing(self):
        self.write(self.override, 'LANES="2"\n')
        before = self.override.stat().st_mtime_ns
        out = self.prepare_role({"LANES": "2"})
        self.assertFalse(out["changed"])
        self.run_token(out["token"])
        self.assertEqual(self.override.stat().st_mtime_ns, before)
        self.assertFalse(self.prepare_role({}, ["EFFORT"], role="reviewer")["changed"])
        self.assertFalse((self.config / "roles" / "reviewer.conf").exists())

    def test_keys_that_move_the_boundary_or_are_unknown_are_refused(self):
        for key in ("BROKER_TOOLS", "WORKSPACE", "TEMPLATE", "OPTIONAL", "lanes", "NOPE", ""):
            out = self.prepare_role({key: "x"}, want=400)
            self.assertIn("can't be set from the dashboard", out["error"], key)
            self.assertIn("can't be set", self.prepare_role({}, [key], want=400)["error"], key)
        self.assertIn("named twice", self.prepare_role({"LANES": "2"}, ["LANES"], want=400)["error"])
        self.assertIn("named twice", self.prepare_role({}, ["LANES", "LANES"], want=400)["error"])
        self.api("config-prepare", {"kind": "role", "role": "developer", "set": [], "unset": []}, 400)
        self.api("config-prepare", {"kind": "role", "role": "developer", "set": {}, "unset": "LANES"}, 400)
        self.assertFalse(self.override.exists())

    def test_values_must_be_one_clean_line(self):
        for value in ('2"\nBROKER_TOOLS="web_fetch', "2\n", 'a"b', "a\\b", "a\tb", "a\x00", "a\x7f", "a\x85",
                      "a b", "x" * 201, 3, None, ["2"]):
            out = self.prepare_role({"MODEL": value}, want=400)
            self.assertIn("MODEL", out["error"], repr(value))
        self.assertFalse(self.override.exists())

    def test_values_go_through_the_validation_factory_start_runs(self):
        for key, value, words in (("LANES", "9", "LANES must be"), ("LANES", "0", "LANES must be"),
                                  ("LANES", " 3", "LANES must be"), ("EFFORT", "max", "EFFORT must be"),
                                  ("MODEL", "a b", "MODEL must"), ("MODELS", "ok b@d", "MODELS entries"),
                                  ("MAX_TURNS", "-1", "MAX_TURNS must"), ("MAX_SESSION_TOKENS", "1e6", "MAX_SESSION"),
                                  ("MAX_JOB_SECONDS", "1.5", "MAX_JOB_SECONDS")):
            out = self.prepare_role({key: value}, want=400)
            self.assertIn(words, out["error"], (key, value))
            self.assertIn("config/roles/developer.conf", out["error"])  # the real path, not the throwaway one
            self.assertNotIn("factory-cu-", out["error"])
        for key, value in (("LANES", "8"), ("EFFORT", ""), ("MODEL", "claude-opus-5-5"), ("MAX_TURNS", "0")):
            self.prepare_role({key: value})
        self.assertFalse(self.override.exists())

    def test_a_bad_existing_override_is_refused_and_left_alone(self):
        for text in ('WORKSPACE="ro"\n', 'BROKER_TOOLS="web_fetch"\nLANES="2"\n', "LANES=2\n", 'LANES="12"\n'):
            self.write(self.override, text)
            self.assertIn("developer.conf", self.prepare_role({"MODEL": "opus"}, want=400)["error"], text)
            self.assertEqual(self.override.read_text(), text)
        self.override.write_bytes(b'LANES="2"\n# \xff\n')
        self.assertIn("not UTF-8", self.prepare_role({"MODEL": "opus"}, want=409)["error"])
        self.override.write_bytes(b"#" * (cw.OVERRIDE_MAX + 1))
        self.assertIn("too large", self.prepare_role({"MODEL": "opus"}, want=409)["error"])

    def test_only_roles_this_machine_runs(self):
        self.write(self.root / "roles" / "researcher" / "role.conf", 'OPTIONAL="1"\nTEMPLATE="reviewer"\nWORKSPACE="ro"\n')
        for role in ("researcher", "../developer", "developer/../reviewer", "broker", "", None, 3, "nobody"):
            self.assertIn("roles this machine runs", self.prepare_role({"LANES": "2"}, role=role, want=400)["error"])
        self.assertFalse((self.config / "roles").exists())

    def test_a_symlinked_or_odd_target_is_refused(self):
        elsewhere = self.write(self.root / "elsewhere.conf", 'LANES="2"\n')
        (self.config / "roles").mkdir()
        self.override.symlink_to(elsewhere)
        self.assertIn("symlink", self.prepare_role({"LANES": "3"}, want=409)["error"])
        self.override.unlink()
        os.mkfifo(self.override)  # a FIFO must neither hang the read nor be replaced
        self.assertIn("not a regular file", self.prepare_role({"LANES": "3"}, want=409)["error"])
        self.override.unlink()
        self.override.mkdir()
        self.assertEqual(self.prepare_role({"LANES": "3"}, want=409)["error"].count("not a regular file"), 1)
        self.assertEqual(elsewhere.read_text(), 'LANES="2"\n')

    def test_a_symlinked_roles_folder_is_refused_at_prepare_and_at_run(self):
        real = self.root / "real-roles"
        real.mkdir()
        (self.config / "roles").symlink_to(real)
        self.assertIn("symlink", self.prepare_role({"LANES": "3"}, want=409)["error"])
        (self.config / "roles").unlink()
        out = self.prepare_role({"LANES": "3"})
        (self.config / "roles").symlink_to(real)  # swapped in between prepare and run
        self.assertIn("symlink", self.run_token(out["token"], want=409)["error"])
        self.assertEqual(os.listdir(real), [])

    def test_a_concurrent_edit_is_refused_as_changed(self):
        self.write(self.override, 'LANES="2"\n')
        out = self.prepare_role({"LANES": "3"})
        self.write(self.override, 'LANES="2"\nEFFORT="low"\n')  # edited by hand meanwhile
        self.assertEqual(self.run_token(out["token"], want=409)["error"], "changed")
        self.assertEqual(self.override.read_text(), 'LANES="2"\nEFFORT="low"\n')
        out = self.prepare_role({"LANES": "3"})
        self.override.unlink()  # removed meanwhile: also a change
        self.assertEqual(self.run_token(out["token"], want=409)["error"], "changed")
        self.assertFalse(self.override.exists())
        out = self.prepare_role({"LANES": "3"})
        self.write(self.override, "# made meanwhile\n")  # created meanwhile
        self.assertEqual(self.run_token(out["token"], want=409)["error"], "changed")

    def test_the_token_is_single_use_expires_and_is_bound_to_config(self):
        out = self.prepare_role({"LANES": "3"})
        self.run_token(out["token"])
        self.assertEqual(self.run_token(out["token"], want=403)["error"], "no confirmation: press the key again")
        for bad in ("", "x", None, 3):
            self.assertEqual(self.run_token(bad, want=403)["error"], "no confirmation: press the key again")
        out = self.prepare_role({"LANES": "4"})
        self.env.clock.t += 121
        self.assertEqual(self.run_token(out["token"], want=403)["error"], "the confirmation expired: press the key again")
        self.assertEqual(self.override.read_text(), 'LANES="3"\n')
        # A RESTART token can't write config, and a config token can't start a RESTART.
        self.env.bind()
        restart = self.api("prepare", {"action": "restart"})["confirm"]
        self.assertEqual(self.run_token(restart, want=403)["error"], "that confirmation was for another action")
        token = self.prepare_role({"LANES": "5"})["token"]
        out = self.api("run", {"action": "restart", "confirm": token}, 403)
        self.assertEqual(out["error"], "that confirmation was for another action")
        self.assertEqual(self.env.argv(), [])
        # A later prepare replaces the earlier token.
        first = self.prepare_role({"LANES": "5"})["token"]
        second = self.prepare_role({"LANES": "6"})["token"]
        self.run_token(first, want=403)
        self.run_token(second)
        self.assertEqual(self.override.read_text(), 'LANES="6"\n')

    def test_no_write_while_an_action_runs(self):
        out = self.prepare_role({"LANES": "3"})
        self.env.controls.phase, self.env.controls.action = "running", "restart"
        self.assertIn("still running", self.run_token(out["token"], want=409)["error"])
        self.assertIn("still running", self.prepare_role({"LANES": "3"}, want=409)["error"])
        self.env.controls.phase = "idle"
        self.assertFalse(self.override.exists())

    def test_a_failed_write_leaves_the_old_file_and_no_temp(self):
        self.write(self.override, 'LANES="2"\n')
        out = self.prepare_role({"LANES": "3"})
        real = os.replace

        def boom(*a, **k):
            raise OSError(28, "No space left on device")
        os.replace = boom
        try:
            self.assertEqual(self.run_token(out["token"], want=503)["error"], "the file could not be written; try again")
        finally:
            os.replace = real
        self.assertEqual(self.override.read_text(), 'LANES="2"\n')
        self.assertEqual(os.listdir(self.override.parent), ["developer.conf"])

    def test_the_service_due_lamp_lights_after_a_write(self):
        # A snapshot as `factory start` leaves it: the same override, enabled-roles written last.
        self.write(self.override, 'LANES="2"\n')
        snap = Path(self.paths.dispatch_state)
        self.write(snap / "role-overrides" / "developer.conf", 'LANES="2"\n')
        self.write(snap / "enabled-roles", "")
        now = self.env.clock()
        self.assertIsNone(self.env.board.service(now)["due"])
        self.run_token(self.prepare_role({"LANES": "3"})["token"])
        # At once, not after SERVICE_TTL: the write drops the lamp's cache.
        self.assertEqual(self.env.board.service(now)["due"], "restart")
        self.assertEqual(self.env.board.service(now)["lines"], ["role override developer.conf changed"])

    def test_the_setup_screen_shows_the_new_value_at_once(self):
        self.api("state", {})  # the SETUP data is now cached
        self.run_token(self.prepare_role({"LANES": "4"})["token"])
        role = next(r for r in self.api("state", {})["config"]["roles"] if r["name"] == "developer")
        self.assertEqual(next(k for k in role["keys"] if k["key"] == "LANES")["value"], "4")


class NotesTests(Base):
    serve = False

    def test_shared_and_role_notes_round_trip(self):
        self.assertEqual(self.api("config-read", {"kind": "notes", "role": ""}),
                         {"text": "", "chars": 0, "exists": False})
        out = self.prepare_notes("Run tests with `npm test`.\nCafé ✓\n")
        self.assertEqual((out["limit"], out["applies"], out["changed"]), (20000, "RESTART", True))
        self.assertTrue(out["path"].endswith("config/projects/proj.md"))
        self.assertEqual(out["totals"], {"developer": 1000 + len("\n# This project (proj)\n") + 34,
                                         "reviewer": 1000 + len("\n# This project (proj)\n") + 34})
        self.run_token(out["token"])
        self.assertEqual(self.notes.read_text(encoding="utf-8"), "Run tests with `npm test`.\nCafé ✓\n")
        self.assertEqual(stat.S_IMODE(self.notes.stat().st_mode), 0o600)
        self.assertEqual(self.api("config-read", {"kind": "notes", "role": ""}),
                         {"text": "Run tests with `npm test`.\nCafé ✓\n", "chars": 34, "exists": True})
        self.run_token(self.prepare_notes("Docs in British English.", role="developer")["token"])
        self.assertEqual(self.dev_notes.read_text(), "Docs in British English.")
        self.assertEqual(self.api("config-read", {"kind": "notes", "role": "developer"})["chars"], 24)
        self.assertFalse(self.api("config-read", {"kind": "notes", "role": "reviewer"})["exists"])

    def test_the_diff_shows_context_around_a_change(self):
        lines = [f"line {i}" for i in range(40)]
        self.write(self.notes, "\n".join(lines) + "\n")
        lines[20] = "line twenty"
        out = self.prepare_notes("\n".join(lines) + "\n")
        self.assertEqual(out["diff"][0], {"op": " ", "text": "⋯ 17 unchanged lines", "gap": True})
        self.assertEqual([d["op"] for d in out["diff"][1:-1]], [" ", " ", " ", "-", "+", " ", " ", " "])
        self.assertEqual(out["diff"][-1]["text"], "⋯ 16 unchanged lines")
        self.assertFalse(out["diff_truncated"])
        many = self.prepare_notes("\n".join(f"new {i}" for i in range(1000)))
        self.assertEqual(len(many["diff"]), cw.DIFF_LINES)
        self.assertTrue(many["diff_truncated"])

    def test_diff_lines_are_made_safe_to_show(self):
        out = self.prepare_notes("a‮b\x1b[31m\n")
        self.assertEqual(out["diff"], [{"op": "+", "text": "a⟨U+202E⟩b⟨U+001B⟩[31m"}])

    def test_text_limits_and_encoding(self):
        self.prepare_notes("x" * 18000)  # within the limit; with the instructions, too
        self.assertIn("over 20,000", self.prepare_notes("x" * 20001, want=400)["error"])
        self.assertIn("UTF-8", self.prepare_notes("a\ud800b", want=400)["error"])
        self.assertIn("NUL", self.prepare_notes("a\x00b", want=400)["error"])
        for text in (None, 3, ["x"]):
            self.prepare_notes(text, want=400)
        self.assertFalse(self.notes.exists())

    def test_a_role_over_the_limit_is_refused_naming_it(self):
        self.write(self.root / "roles" / "reviewer" / "instructions.md", "r" * 15000)
        out = self.prepare_notes("n" * 6000, want=409)
        self.assertIn("the reviewer's instructions and project notes would come to", out["error"])
        self.assertIn("over the 20,000 limit", out["error"])
        self.assertFalse(self.notes.exists())
        # The developer's own notes count only for the developer.
        self.prepare_notes("n" * 6000, role="developer")
        self.write(self.notes, "s" * 3000)
        self.assertIn("developer's", self.prepare_notes("n" * 17000, role="developer", want=409)["error"])
        # Already over the limit: a change that shrinks it is let through (the way back under).
        self.write(self.notes, "s" * 6000)
        self.prepare_notes("s" * 5500)
        self.assertIn("reviewer's", self.prepare_notes("s" * 6500, want=409)["error"])

    def test_notes_need_a_usable_project_and_a_running_role(self):
        for project in ("", "(no project)", ".hidden"):
            self.env.board.project = project
            self.assertIn("no project", self.prepare_notes("x", want=409)["error"])
            self.api("config-read", {"kind": "notes", "role": ""}, 409)
        self.env.board.project = "proj"
        for role in ("nobody", "../x", None, "broker"):
            self.prepare_notes("x", role=role, want=400)
            self.api("config-read", {"kind": "notes", "role": role}, 400)
        self.api("config-read", {"kind": "role", "role": "developer"}, 400)
        self.api("config-prepare", {"kind": "policy"}, 400)
        self.api("config-prepare", {"kind": "notes", "role": "", "text": "x", "path": "/etc/passwd"})
        self.assertFalse(Path("/etc/passwd.md").exists())

    def test_read_is_capped_and_refuses_symlinks_and_odd_files(self):
        self.write(self.notes, "é" * (dashboard_setup.NOTES_READ_MAX // 2 + 10))
        out = self.api("config-read", {"kind": "notes", "role": ""})
        self.assertEqual(len(out["text"].encode("utf-8")), dashboard_setup.NOTES_READ_MAX)
        self.assertIn("too large", self.prepare_notes("short", want=409)["error"])
        self.notes.unlink()
        secret = self.write(self.root / "secret.txt", "secret")
        self.notes.symlink_to(secret)
        self.assertIn("symlink", self.api("config-read", {"kind": "notes", "role": ""}, 409)["error"])
        self.assertIn("symlink", self.prepare_notes("x", want=409)["error"])
        self.notes.unlink()
        os.mkfifo(self.notes)
        self.assertIn("not a regular file", self.api("config-read", {"kind": "notes", "role": ""}, 409)["error"])
        self.notes.unlink()
        (self.config / "projects").rmdir()
        (self.config / "projects").symlink_to(self.root)
        self.write(self.root / "proj.md", "outside")
        self.assertIn("symlink", self.api("config-read", {"kind": "notes", "role": ""}, 409)["error"])
        self.assertIn("symlink", self.prepare_notes("x", want=409)["error"])
        self.assertEqual(secret.read_text(), "secret")

    def test_a_concurrent_notes_edit_is_refused(self):
        self.write(self.notes, "old\n")
        out = self.prepare_notes("new\n")
        self.write(self.notes, "edited\n")
        self.assertEqual(self.run_token(out["token"], want=409)["error"], "changed")
        self.assertEqual(self.notes.read_text(), "edited\n")

    def test_the_digest_is_of_the_file_as_prepared(self):
        self.write(self.notes, "old\n")
        self.prepare_notes("new\n")
        plan = self.env.controls.confirm[3]
        self.assertEqual(plan.digest, hashlib.sha256(b"old\n").hexdigest())
        self.assertEqual(plan.content, b"new\n")
        self.notes.unlink()
        self.prepare_notes("new\n")
        self.assertEqual(self.env.controls.confirm[3].digest, "absent")

    def test_the_lamp_lights_after_a_notes_write(self):
        snap = Path(self.paths.dispatch_state)
        self.write(snap / "enabled-roles", "")
        os.utime(snap / "enabled-roles", (1, 1))
        now = self.env.clock()
        self.assertIsNone(self.env.board.service(now)["due"])
        self.run_token(self.prepare_notes("x")["token"])
        self.assertEqual(self.env.board.service(now)["lines"], ["project notes proj.md changed"])


class HttpTests(Base):
    """The new routes behind the same front door, and config-prepare's larger body cap."""

    def test_config_routes_need_every_header(self):
        e = self.env
        body = {"kind": "notes", "role": ""}
        self.assertEqual(e.post("config-read", body)[0], 200)
        for name in ("config-read", "config-prepare", "config-run"):
            for over in ({"Origin": f"http://127.0.0.1:{e.port}"}, {"Origin": None}, {"Host": "evil.example"},
                         {"Sec-Fetch-Site": "cross-site"}, {"Cookie": None}, {"X-Factory-Control": None},
                         {"X-Factory-Control": "0" * 48}):
                status, out, resp = e.post(name, body, **over)
                self.assertEqual(status, 403, (name, over))
                self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
            self.assertEqual(e.post(name, body, **{"Content-Type": "text/plain"})[0], 415, name)
            self.assertEqual(e.post(name, body, method="GET")[0], 405, name)
        self.assertFalse(self.notes.exists())

    def test_a_full_write_over_http(self):
        e = self.env
        status, out, _ = e.post("config-prepare", {"kind": "notes", "role": "", "text": "\u00e9" * 18000})
        self.assertEqual(status, 200, out)  # 18,000 escaped characters: over 8 KiB, under 128 KiB
        status, out, _ = e.post("config-run", {"token": out["token"]})
        self.assertEqual((status, out), (200, {"ok": True, "applies": "RESTART"}))
        self.assertEqual(self.notes.read_text(encoding="utf-8"), "\u00e9" * 18000)

    def test_body_caps(self):
        e = self.env
        big = json.dumps({"kind": "notes", "role": "", "text": "x" * 9000}).encode()
        self.assertEqual(e.post("config-read", raw=big)[0], 413)  # 8 KiB on every other route
        self.assertEqual(e.post("config-run", raw=big)[0], 413)
        self.assertEqual(e.post("state", raw=big)[0], 413)
        self.assertEqual(e.post("config-prepare", raw=big)[0], 200)
        over = b"{" + b" " * dt.MAX_CONFIG_BODY + b"}"
        self.assertEqual(e.post("config-prepare", raw=over)[0], 413)
        # Refused on the declared length, before any of the body is read.
        out = e.raw((f"POST /cu/api/config-prepare HTTP/1.1\r\nHost: 127.0.0.1:{e.tport}\r\n"
                     f"Origin: http://127.0.0.1:{e.tport}\r\nCookie: {e.cookie}\r\nX-Factory-Control: {e.key}\r\n"
                     f"Content-Type: application/json\r\nContent-Length: {10 ** 9}\r\nConnection: close\r\n\r\n").encode())
        self.assertIn(b" 413 ", out.split(b"\r\n", 1)[0])
        # The longest notes fit: a browser's JSON.stringify sends non-ASCII as UTF-8, at most 4 bytes a character.
        self.assertLess(len(json.dumps({"kind": "notes", "role": "", "text": "\U0001F600" * 20000},
                                       ensure_ascii=False).encode()), dt.MAX_CONFIG_BODY)
        ok = b"{" + b" " * (dt.MAX_CONFIG_BODY - 2) + b"}"
        self.assertEqual(e.post("config-prepare", raw=ok)[0], 400)  # read, then refused for its content


class CuPageContractTests(Base):
    """The requests scripts/dashboard-ui/cu.js sends for a tuning change, byte for byte, through the
    real front door, and the answers it parses: token, expires, path, changed, applies, diff ops, the
    exact 409 "changed", and the knobs' values in the next state poll."""

    def keys(self, role="developer"):
        r = next(r for r in self.api("state", {})["config"]["roles"] if r["name"] == role)
        return {k["key"]: k for k in r["keys"]}

    def post(self, name, body):
        status, out, _ = self.env.post(name, body)
        return status, out

    def test_cu_js_names_the_keys_and_the_demo_line_the_host_uses(self):
        js = (Path(__file__).resolve().parent / "dashboard-ui" / "cu.js").read_text()
        tune = re.search(r"const TUNE_KEYS = \[([^\]]*)\]", js).group(1)
        self.assertEqual(sorted(re.findall(r'"([A-Z_]+)"', tune)), sorted(k for k in roles.ROLE_OVERRIDE_KEYS if k != "MODELS"))
        self.assertIn("/^demo mode/i", js)

    def test_prepare_run_and_the_knobs_afterwards(self):
        self.write(self.override, 'MAX_TURNS="80"\nMODEL="opus"\n')
        before = self.keys()
        self.assertEqual((before["MAX_TURNS"]["value"], before["MAX_TURNS"]["source"]), ("80", "LOCAL"))
        self.assertEqual(before["MAX_TURNS"]["repo"], "200")
        # What cu.js sends: LANES up, EFFORT set, the LOCAL MAX_TURNS unset; numbers are strings.
        status, out = self.post("config-prepare", {"kind": "role", "role": "developer",
                                                   "set": {"LANES": "5", "EFFORT": "high"}, "unset": ["MAX_TURNS"]})
        self.assertEqual(status, 200, out)
        self.assertIsInstance(out["token"], str)
        self.assertTrue(out["token"])
        self.assertIsInstance(out["expires"], int)
        self.assertGreater(out["expires"], 0)
        self.assertTrue(out["path"].endswith("roles/developer.conf"))
        self.assertIs(out["changed"], True)
        self.assertEqual(out["applies"], "RESTART")  # every change waits for a restart: never "LIVE"
        self.assertTrue(all(set(d) >= {"op", "text"} and d["op"] in " +-" and isinstance(d["text"], str)
                            for d in out["diff"]))
        ops = [(d["op"], d["text"]) for d in out["diff"]]
        self.assertIn(("-", 'MAX_TURNS="80"'), ops)
        self.assertIn(("+", 'LANES="5"'), ops)
        self.assertIn(("+", 'EFFORT="high"'), ops)
        self.assertEqual(self.override.read_text(), 'MAX_TURNS="80"\nMODEL="opus"\n')  # prepare writes nothing
        # The knobs still show the old values until CONFIRM.
        self.assertEqual(self.keys()["LANES"]["source"], "REPO")
        self.assertEqual(self.post("config-run", {"token": out["token"]}), (200, {"ok": True, "applies": "RESTART"}))
        after = self.keys()  # the next poll: the write dropped the SETUP cache
        self.assertEqual((after["LANES"]["value"], after["LANES"]["source"], after["LANES"]["repo"]), ("5", "LOCAL", "3"))
        self.assertEqual((after["EFFORT"]["value"], after["EFFORT"]["source"]), ("high", "LOCAL"))
        self.assertEqual((after["MAX_TURNS"]["value"], after["MAX_TURNS"]["source"]), ("200", "REPO"))
        self.assertEqual((after["MODEL"]["value"], after["MODEL"]["source"]), ("opus", "LOCAL"))  # untouched
        # The token was single use: a second CONFIRM is the 403 cu.js words as "no longer valid".
        status, out2 = self.post("config-run", {"token": out["token"]})
        self.assertEqual(status, 403, out2)

    def test_a_changed_file_is_the_exact_409_cu_js_matches(self):
        self.write(self.override, 'LANES="2"\n')
        status, out = self.post("config-prepare", {"kind": "role", "role": "developer", "set": {"LANES": "4"}, "unset": []})
        self.assertEqual(status, 200, out)
        self.write(self.override, 'LANES="7"\n')  # edited by hand after the diff was shown
        self.assertEqual(self.post("config-run", {"token": out["token"]}), (409, {"error": "changed"}))
        self.assertEqual(self.override.read_text(), 'LANES="7"\n')

    def test_a_bad_value_is_a_400_with_text_cu_js_shows(self):
        status, out = self.post("config-prepare", {"kind": "role", "role": "developer", "set": {"LANES": "9"}, "unset": []})
        self.assertEqual(status, 400, out)
        self.assertIsInstance(out["error"], str)
        self.assertTrue(out["error"])


if __name__ == "__main__":
    unittest.main()
