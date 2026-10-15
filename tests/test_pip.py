"""pip_download's requirements parsing."""

import unittest

from agentbroker import tools  # noqa: F401 - registers every tool
from agentbroker.core.errors import Denied
from agentbroker.tools import pip

from .base import Base


class RequirementsTests(Base):
    def test_pinned_ok(self):
        pins = pip.parse_requirements(
            "# comment\nrequests==2.32.3 \\\n    --hash=sha256:" + "a" * 64 + "\nidna[x]==3.7  # trailing\n")
        self.assertEqual(pins, [("requests", "2.32.3"), ("idna", "3.7")])

    def test_rejects_unpinned_and_options(self):
        for bad in ["requests>=2", "requests", "--index-url https://evil/simple\nx==1",
                    "-e git+https://github.com/x/y", "git+https://x/y.git", "./local", "x==1 --no-binary :all:"]:
            with self.assertRaises(Denied, msg=bad):
                pip.parse_requirements(bad)


if __name__ == "__main__":
    unittest.main()
