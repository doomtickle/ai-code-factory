"""Shared fixtures for the broker tests."""

import io
import tempfile
import unittest
import urllib.error
from pathlib import Path

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import config

POLICY = {
    "web": {"allow_hosts": ["docs.python.org", "*.example.com"], "max_bytes": 1000},
    "git": {"repos": ["acme/widgets"], "protected_branches": ["main", "release/*"],
            "push_branch_prefix": "factory/", "default_base": "main"},
    "packages": {"npm_deny": ["evil-pkg"], "npm_allow": [], "pypi_deny": ["bad-lib"], "pypi_allow": []},
    "filters": {},
}

FAKE_GH = "ghp_" + "a" * 36
FAKE_ANT = "sk-ant-api03-" + "b" * 30
FAKE_AWS = "AKIA" + "C" * 16

TEST_CLOUD_ID = "3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90"
JIRA_POLICY = {**POLICY, "jira": {"site": "example.atlassian.net", "cloud_id": TEST_CLOUD_ID,
                                  "projects": [], "max_results": 25, "max_comments": 5}}


class _FakeHTTPResponse:
    """Stands in for the object `with urllib.request.urlopen(...) as resp:` yields."""

    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


def fake_http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="https://generativelanguage.googleapis.com/", code=code,
                                  msg="error", hdrs=None, fp=io.BytesIO(body))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config.WORKSPACE = root / "ws"
        config.STATE = root / "state"
        config.WORKSPACE.mkdir()
        config.STATE.mkdir()
        # Point credential files somewhere that does not exist by default, so
        # tests never accidentally read a real developer machine's secret.
        config.JIRA_SECRET_PATH = root / "secrets" / "jira"
        config.GEMINI_SECRET_PATH = root / "secrets" / "gemini"
        config.NPM_REGISTRY_SECRET_PATH = root / "secrets" / "npm-registry"

    def tearDown(self):
        self.tmp.cleanup()

    def write_secret(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
