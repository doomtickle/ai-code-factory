"""gemini_flash_query, and its per-role gating."""

import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import config, protocol
from passdesk.core.errors import Denied
from passdesk.tools import gemini

from .base import FAKE_GH, POLICY, Base, _FakeHTTPResponse, fake_http_error


class GeminiTests(Base):
    def setUp(self):
        super().setUp()
        self.write_secret(config.GEMINI_SECRET_PATH, "test-key")

    def test_query_success(self):
        body = {"candidates": [{"content": {"parts": [{"text": "a concise summary"}]}}]}
        resp = _FakeHTTPResponse(json.dumps(body).encode())
        with mock.patch("passdesk.core.net.NO_REDIRECT_OPENER.open", return_value=resp) as urlopen:
            out = gemini.tool_gemini_flash_query({"prompt": "summarize this page"}, POLICY)
        self.assertIn("a concise summary", out)
        self.assertIn("UNTRUSTED CONTENT", out)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.get_header("X-goog-api-key"), "test-key")
        self.assertIn("generativelanguage.googleapis.com", req.full_url)
        self.assertNotIn("test-key", req.full_url)  # key must not leak into the URL

    def test_http_error_hints_at_missing_key(self):
        with mock.patch("passdesk.core.net.NO_REDIRECT_OPENER.open", side_effect=fake_http_error(401, b"denied")):
            out = gemini.tool_gemini_flash_query({"prompt": "hi"}, POLICY)
        self.assertIn("HTTP 401", out)
        self.assertIn("Configuring credentials", out)

    def test_http_error_body_is_wrapped_untrusted(self):
        with mock.patch("passdesk.core.net.NO_REDIRECT_OPENER.open", side_effect=fake_http_error(500, b"oops")):
            out = gemini.tool_gemini_flash_query({"prompt": "hi"}, POLICY)
        self.assertIn("UNTRUSTED CONTENT", out)
        self.assertIn("oops", out)

    def test_missing_credential_file_denied(self):
        config.GEMINI_SECRET_PATH.unlink()
        with self.assertRaisesRegex(Denied, "no Gemini API key"):
            gemini.tool_gemini_flash_query({"prompt": "hi"}, POLICY)

    def test_reads_key_from_file_not_env(self):
        # A stray GEMINI_API_KEY in the environment must not be used.
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "env-key-should-be-ignored"}):
            body = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
            resp = _FakeHTTPResponse(json.dumps(body).encode())
            with mock.patch("passdesk.core.net.NO_REDIRECT_OPENER.open", return_value=resp) as urlopen:
                gemini.tool_gemini_flash_query({"prompt": "hi"}, POLICY)
            req = urlopen.call_args[0][0]
        self.assertEqual(req.get_header("X-goog-api-key"), "test-key")

    def test_prompt_over_limit_denied(self):
        policy = {**POLICY, "gemini": {"max_prompt_chars": 10}}
        with self.assertRaisesRegex(Denied, "exceeds"):
            gemini.tool_gemini_flash_query({"prompt": "x" * 11}, policy)

    def test_prompt_with_secret_denied(self):
        with self.assertRaisesRegex(Denied, "github-token"):
            gemini.tool_gemini_flash_query({"prompt": f"key {FAKE_GH}"}, POLICY)


class GeminiRoleGatingTests(Base):
    def setUp(self):
        super().setUp()
        (config.STATE / "tokens").mkdir()
        for role, tok in (("developer", b"dev-token"), ("reviewer", b"rev-token"), ("researcher", b"res-token")):
            (config.STATE / "tokens" / f"{role}.sha256").write_text(hashlib.sha256(tok).hexdigest() + "\n")
        (config.STATE / "roles.json").write_text(json.dumps({
            "developer": ["npm_fetch", "pip_download", "git_fetch", "git_push", "github_create_pr"],
            "reviewer": [],
            "researcher": ["web_fetch", "gemini_flash_query"],
        }))
        config.POLICY_PATH = Path(__file__).parents[1] / "policy.toml"
        self.write_secret(config.GEMINI_SECRET_PATH, "test-key")

    def call(self, role, args):
        return protocol.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "gemini_flash_query", "arguments": args}}, role)["result"]

    def test_researcher_allowed_developer_and_reviewer_denied(self):
        body = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        resp = _FakeHTTPResponse(json.dumps(body).encode())
        with mock.patch("passdesk.core.net.NO_REDIRECT_OPENER.open", return_value=resp):
            res = self.call("researcher", {"prompt": "hi"})
        self.assertFalse(res["isError"])
        for role in ("developer", "reviewer"):
            res = self.call(role, {"prompt": "hi"})
            self.assertTrue(res["isError"], msg=role)
            self.assertIn(f"principal {role!r} is not granted tool 'gemini_flash_query'", res["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
