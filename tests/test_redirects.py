"""Authenticated requests never follow redirects."""

import base64
import email
import io
import unittest
import urllib.error
import urllib.request
from unittest import mock
from urllib.response import addinfourl

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import (
    config,
    net,
)
from passdesk.tools import gemini, jira

from .base import JIRA_POLICY, POLICY, Base


class RedirectSafetyTests(Base):
    """A 3xx must never carry Authorization / x-goog-api-key to a new host."""

    def setUp(self):
        super().setUp()
        self.write_secret(config.JIRA_SECRET_PATH, "user@example.com:supersecrettoken123")
        self.write_secret(config.GEMINI_SECRET_PATH, "gemini-secret-key")

    @staticmethod
    def _redirecting_opener(captured: list) -> urllib.request.OpenerDirector:
        """A minimal opener whose only HTTPS handler answers every request with a
        302 to an attacker-controlled host, wired through the real HTTPErrorProcessor
        and the production RefuseRedirects handler (not a mock of either)."""

        class StubHandler(urllib.request.BaseHandler):
            def https_open(self, req):
                captured.append(dict(req.header_items()))
                headers = email.message_from_string("Location: https://evil.example/steal\r\n")
                resp = addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
                resp.msg = "Found"
                return resp

        opener = urllib.request.OpenerDirector()
        opener.add_handler(StubHandler())
        opener.add_handler(urllib.request.HTTPErrorProcessor())
        opener.add_handler(net.RefuseRedirects())
        return opener

    def test_redirect_request_refuses_instead_of_building_a_follow_up_request(self):
        req = urllib.request.Request("https://api.atlassian.com/x",
                                     headers={"Authorization": "Basic secret"})
        with self.assertRaises(urllib.error.HTTPError):
            net.RefuseRedirects().redirect_request(req, None, 302, "Found", {}, "https://evil.example/steal")

    def test_jira_call_refuses_redirect_and_credential_never_reaches_it(self):
        captured: list = []
        with mock.patch.object(jira, "NO_REDIRECT_OPENER", self._redirecting_opener(captured)):
            data, err = jira.jira_call("/rest/api/3/issue/PROJ-1", {}, JIRA_POLICY)
        self.assertIsNone(data)
        self.assertIn("HTTP 302", err)
        self.assertNotIn("supersecrettoken123", err)
        # Only the original request was ever sent: no second, follow-up request
        # to the redirect target - which is what would have carried the header.
        self.assertEqual(len(captured), 1)
        expected_auth = "Basic " + base64.b64encode(b"user@example.com:supersecrettoken123").decode()
        self.assertEqual(captured[0].get("Authorization"), expected_auth)

    def test_gemini_refuses_redirect_and_credential_never_reaches_it(self):
        captured: list = []
        with mock.patch.object(gemini, "NO_REDIRECT_OPENER", self._redirecting_opener(captured)):
            out = gemini.tool_gemini_flash_query({"prompt": "hi"}, POLICY)
        self.assertIn("HTTP 302", out)
        self.assertNotIn("gemini-secret-key", out)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].get("X-goog-api-key"), "gemini-secret-key")


if __name__ == "__main__":
    unittest.main()
