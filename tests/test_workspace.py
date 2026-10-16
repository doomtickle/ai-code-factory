"""Workspace path containment and the .factory/ exchange directories."""

import os
import unittest

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import config, workspace
from passdesk.core.errors import Denied

from .base import Base


class WorkspaceTests(Base):
    def test_path_containment(self):
        (config.WORKSPACE / "a.txt").write_text("x")
        self.assertEqual(workspace.workspace_path("a.txt"), (config.WORKSPACE / "a.txt").resolve())
        with self.assertRaises(Denied):
            workspace.workspace_path("../state")
        os.symlink("/etc", config.WORKSPACE / "link")
        with self.assertRaises(Denied):
            workspace.workspace_path("link/passwd")

    def test_factory_dir_is_gitignored(self):
        workspace.factory_dir("incoming")
        self.assertEqual((config.WORKSPACE / ".factory/.gitignore").read_text(), "*\n")


if __name__ == "__main__":
    unittest.main()
