"""The principal CLI's core logic: on-disk formats must match what
core.auth (and ai-code-factory's scripts/start-broker.sh) read and write."""

import json
import stat
import unittest
from unittest import mock

from passdesk import principal
from passdesk.core import auth, config

from .base import Base


class PrincipalBase(Base):
    def grants(self) -> dict:
        path = config.STATE / "grants.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def token_hash_path(self, name):
        return config.STATE / "tokens" / f"{name}.sha256"


class AddTests(PrincipalBase):
    def test_add_returns_a_high_entropy_token(self):
        token = principal.add("alice", ["git_fetch"])
        self.assertEqual(len(token), 64)  # 32 bytes, hex-encoded - secrets.token_hex(32)
        int(token, 16)  # raises if not hex

    def test_token_hash_matches_what_auth_expects(self):
        token = principal.add("alice", ["git_fetch"])
        digest = self.token_hash_path("alice").read_text()
        self.assertEqual(digest, principal.token_digest(token) + "\n")
        got, reason = auth.authenticate(f"Bearer {token}")
        self.assertIsNone(reason)
        self.assertEqual(got, "alice")

    def test_grants_json_is_object_form_auth_reads(self):
        principal.add("alice", ["git_fetch", "npm_fetch"])
        grants = self.grants()
        self.assertEqual(grants["alice"], {"tools": ["git_fetch", "npm_fetch"], "source": principal.SOURCE})
        self.assertEqual(auth.principal_tools("alice"), {"git_fetch", "npm_fetch"})

    def test_token_file_is_mode_0600(self):
        principal.add("alice", ["git_fetch"])
        mode = stat.S_IMODE(self.token_hash_path("alice").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_grants_json_is_mode_0600(self):
        principal.add("alice", ["git_fetch"])
        mode = stat.S_IMODE((config.STATE / "grants.json").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_state_and_tokens_dirs_are_mode_0700(self):
        principal.add("alice", ["git_fetch"])
        self.assertEqual(stat.S_IMODE(config.STATE.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((config.STATE / "tokens").stat().st_mode), 0o700)

    def test_creates_state_dirs_if_missing(self):
        # A brand-new deployment: nothing exists yet under BROKER_STATE.
        import shutil
        shutil.rmtree(config.STATE)
        principal.add("alice", ["git_fetch"])
        self.assertTrue((config.STATE / "tokens" / "alice.sha256").is_file())

    def test_rejects_unknown_tool(self):
        with self.assertRaises(principal.PrincipalError) as cm:
            principal.add("alice", ["not_a_real_tool"])
        self.assertIn("unknown tool", str(cm.exception))
        self.assertFalse(self.token_hash_path("alice").exists())
        self.assertNotIn("alice", self.grants())

    def test_rejects_no_tools(self):
        with self.assertRaises(principal.PrincipalError):
            principal.add("alice", [])

    def test_rejects_duplicate_principal(self):
        principal.add("alice", ["git_fetch"])
        with self.assertRaises(principal.PrincipalError) as cm:
            principal.add("alice", ["npm_fetch"])
        self.assertIn("already exists", str(cm.exception))
        # the original grant and token are untouched
        self.assertEqual(auth.principal_tools("alice"), {"git_fetch"})

    def test_rejects_duplicate_principal_with_token_but_no_grant(self):
        # e.g. a grant was hand-edited away but the token file remains.
        principal.add("alice", ["git_fetch"])
        grants = self.grants()
        del grants["alice"]
        (config.STATE / "grants.json").write_text(json.dumps(grants))
        with self.assertRaises(principal.PrincipalError):
            principal.add("alice", ["npm_fetch"])

    def test_different_principals_coexist(self):
        principal.add("alice", ["git_fetch"])
        principal.add("bob", ["npm_fetch"])
        self.assertEqual(auth.principal_tools("alice"), {"git_fetch"})
        self.assertEqual(auth.principal_tools("bob"), {"npm_fetch"})

    def test_tools_are_deduped_and_sorted(self):
        principal.add("alice", ["npm_fetch", "git_fetch", "npm_fetch"])
        self.assertEqual(self.grants()["alice"]["tools"], ["git_fetch", "npm_fetch"])

    def test_seeds_from_roles_json_when_grants_json_is_missing(self):
        # A deployment that has only ever run start-broker.sh (pre-grants.json):
        # add() must not make bob's grant disappear just by creating grants.json.
        (config.STATE / "tokens").mkdir(parents=True)
        (config.STATE / "roles.json").write_text(
            json.dumps({"bob": {"tools": ["npm_fetch"], "source": "roles/bob/role.conf"}}))
        principal.add("alice", ["git_fetch"])
        grants = self.grants()
        self.assertEqual(grants["alice"], {"tools": ["git_fetch"], "source": principal.SOURCE})
        self.assertEqual(grants["bob"], {"tools": ["npm_fetch"], "source": "roles/bob/role.conf"})
        self.assertEqual(auth.principal_tools("bob"), {"npm_fetch"})

    def test_removes_token_file_if_grants_write_fails(self):
        with mock.patch.object(principal, "_save_grants", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            principal.add("alice", ["git_fetch"])
        self.assertFalse(self.token_hash_path("alice").exists())
        self.assertNotIn("alice", self.grants())


class InvalidNameTests(PrincipalBase):
    def test_rejects_empty_name(self):
        with self.assertRaises(principal.PrincipalError):
            principal.add("", ["git_fetch"])

    def test_rejects_path_traversal(self):
        for bad in ("../escape", "..", ".", "a/b", "a\0b"):
            with self.subTest(bad=bad), self.assertRaises(principal.PrincipalError):
                principal.add(bad, ["git_fetch"])

    def test_rejects_uppercase_and_symbols(self):
        for bad in ("Alice", "alice!", "alice.bob", " alice", "alice "):
            with self.subTest(bad=bad), self.assertRaises(principal.PrincipalError):
                principal.add(bad, ["git_fetch"])

    def test_rejects_leading_digit_or_dash(self):
        for bad in ("1alice", "-alice", "_alice"):
            with self.subTest(bad=bad), self.assertRaises(principal.PrincipalError):
                principal.add(bad, ["git_fetch"])

    def test_rejects_trailing_newline(self):
        # `$` in NAME_RE matches just before a trailing "\n", not only at the
        # true end of string; fullmatch (not match) is what actually rejects it.
        with self.assertRaises(principal.PrincipalError):
            principal.add("alice\n", ["git_fetch"])

    def test_accepts_digits_underscore_dash_after_first_char(self):
        principal.add("alice-2_lane", ["git_fetch"])
        self.assertEqual(auth.principal_tools("alice-2_lane"), {"git_fetch"})

    def test_no_files_written_for_an_invalid_name(self):
        with self.assertRaises(principal.PrincipalError):
            principal.add("../escape", ["git_fetch"])
        self.assertFalse((config.STATE / "tokens").exists())
        self.assertFalse((config.STATE / "grants.json").exists())


class ListTests(PrincipalBase):
    def test_list_is_empty_for_a_fresh_state(self):
        self.assertEqual(principal.list_principals(), [])

    def test_list_never_exposes_hash_or_token(self):
        token = principal.add("alice", ["git_fetch", "npm_fetch"])
        people = principal.list_principals()
        self.assertEqual(len(people), 1)
        p = people[0]
        self.assertEqual(p.name, "alice")
        self.assertEqual(p.tools, ["git_fetch", "npm_fetch"])
        self.assertTrue(p.has_token)
        self.assertNotIn(token, vars(p).values())
        digest = self.token_hash_path("alice").read_text().strip()
        self.assertNotIn(digest, str(vars(p)))

    def test_list_sorted_by_name(self):
        principal.add("bob", ["git_fetch"])
        principal.add("alice", ["npm_fetch"])
        self.assertEqual([p.name for p in principal.list_principals()], ["alice", "bob"])

    def test_list_flags_a_grant_with_no_token(self):
        (config.STATE / "tokens").mkdir(parents=True)
        (config.STATE / "grants.json").write_text(json.dumps({"ghost": ["git_fetch"]}))
        [p] = principal.list_principals()
        self.assertEqual(p.name, "ghost")
        self.assertFalse(p.has_token)
        self.assertEqual(p.tools, ["git_fetch"])

    def test_list_handles_a_token_with_no_grant(self):
        (config.STATE / "tokens").mkdir(parents=True)
        (config.STATE / "tokens" / "orphan.sha256").write_text("a" * 64 + "\n")
        [p] = principal.list_principals()
        self.assertEqual(p.name, "orphan")
        self.assertTrue(p.has_token)
        self.assertEqual(p.tools, [])

    def test_list_reads_the_bare_list_grant_form_too(self):
        # the shape start-broker.sh's transition shim (roles.json) still uses,
        # and grants.json may legitimately carry for an older entry.
        (config.STATE / "tokens").mkdir(parents=True)
        (config.STATE / "grants.json").write_text(json.dumps({"legacy": ["git_fetch"]}))
        [p] = principal.list_principals()
        self.assertEqual(p.tools, ["git_fetch"])

    def test_list_tolerates_malformed_grants_without_raising(self):
        # Mixed/invalid shapes must read as "no grant", not raise - matches
        # auth.grant_tools()'s own deny-by-default treatment of these.
        (config.STATE / "tokens").mkdir(parents=True)
        (config.STATE / "grants.json").write_text(json.dumps({
            "mixed_types": [1, "git_fetch", None],
            "not_a_list": "git_fetch",
            "null_grant": None,
        }))
        people = {p.name: p.tools for p in principal.list_principals()}
        self.assertEqual(people["mixed_types"], [])
        self.assertEqual(people["not_a_list"], [])
        self.assertEqual(people["null_grant"], [])


class RevokeTests(PrincipalBase):
    def test_revoke_removes_token_and_grant(self):
        principal.add("alice", ["git_fetch"])
        principal.revoke("alice")
        self.assertFalse(self.token_hash_path("alice").exists())
        self.assertNotIn("alice", self.grants())
        self.assertEqual(principal.list_principals(), [])

    def test_revoked_token_no_longer_authenticates(self):
        token = principal.add("alice", ["git_fetch"])
        principal.revoke("alice")
        got, reason = auth.authenticate(f"Bearer {token}")
        self.assertIsNone(got)
        self.assertIsNotNone(reason)

    def test_revoke_unknown_principal_raises(self):
        with self.assertRaises(principal.PrincipalError):
            principal.revoke("nobody")

    def test_revoke_leaves_other_principals_untouched(self):
        principal.add("alice", ["git_fetch"])
        principal.add("bob", ["npm_fetch"])
        principal.revoke("alice")
        self.assertEqual(auth.principal_tools("bob"), {"npm_fetch"})

    def test_revoke_token_only_entry(self):
        (config.STATE / "tokens").mkdir(parents=True)
        (config.STATE / "tokens" / "orphan.sha256").write_text("a" * 64 + "\n")
        principal.revoke("orphan")
        self.assertFalse(self.token_hash_path("orphan").exists())

    def test_revoke_grant_only_entry(self):
        (config.STATE / "tokens").mkdir(parents=True)
        (config.STATE / "grants.json").write_text(json.dumps({"ghost": ["git_fetch"]}))
        principal.revoke("ghost")
        self.assertNotIn("ghost", self.grants())

    def test_revoke_from_roles_json_state_with_multiple_principals(self):
        # A deployment with only roles.json (bob and carol); bob has a token.
        # revoke("bob") should remove bob's token and grant, write grants.json
        # with carol still present, and bob should no longer authenticate.
        token = "b" * 64
        (config.STATE / "tokens").mkdir(parents=True, exist_ok=True)
        self.token_hash_path("bob").write_text(principal.token_digest(token) + "\n")
        (config.STATE / "roles.json").write_text(json.dumps({
            "bob": {"tools": ["git_fetch"], "source": "roles/bob/role.conf"},
            "carol": {"tools": ["npm_fetch"], "source": "roles/carol/role.conf"}
        }))

        # Bob's token works before the revoke.
        got, reason = auth.authenticate(f"Bearer {token}")
        self.assertEqual(got, "bob")
        self.assertIsNone(reason)

        # Revoke bob
        principal.revoke("bob")

        # Bob's token is gone
        self.assertFalse(self.token_hash_path("bob").exists())

        # grants.json was created with carol's grant but not bob's
        grants = self.grants()
        self.assertNotIn("bob", grants)
        self.assertEqual(grants["carol"], {"tools": ["npm_fetch"], "source": "roles/carol/role.conf"})

        # Carol still authenticates with her tools
        self.assertEqual(auth.principal_tools("carol"), {"npm_fetch"})

        # Bob no longer authenticates
        got, reason = auth.authenticate(f"Bearer {token}")
        self.assertIsNone(got)
        self.assertIsNotNone(reason)


class AtomicWriteTests(PrincipalBase):
    def test_grants_json_has_no_partial_file_left_behind(self):
        principal.add("alice", ["git_fetch"])
        leftovers = [p for p in config.STATE.iterdir() if p.name.startswith(".grants.json.")]
        self.assertEqual(leftovers, [])

    def test_tokens_dir_has_no_partial_file_left_behind(self):
        principal.add("alice", ["git_fetch"])
        leftovers = [p for p in (config.STATE / "tokens").iterdir() if not p.name.endswith(".sha256")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
