"""Workspace path containment and the .factory/ exchange directories."""

import os
import unittest
from pathlib import Path

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
        with workspace.factory_dir("incoming"):
            pass
        self.assertEqual((config.WORKSPACE / ".factory/.gitignore").read_text(), "*\n")

    def test_exchange_dir_name_is_overridable(self):
        old = config.EXCHANGE_DIR
        config.EXCHANGE_DIR = ".exchange"
        self.addCleanup(setattr, config, "EXCHANGE_DIR", old)
        with workspace.factory_dir("incoming"):
            pass
        self.assertTrue((config.WORKSPACE / ".exchange/incoming").is_dir())
        self.assertFalse((config.WORKSPACE / ".factory").exists())

    def outside(self) -> Path:
        d = Path(self.tmp.name) / "outside"
        d.mkdir(exist_ok=True)
        return d

    def test_symlinks_inside_the_workspace_are_refused_too(self):
        (config.WORKSPACE / "real").mkdir()
        (config.WORKSPACE / "real/a.txt").write_text("x")
        os.symlink("real", config.WORKSPACE / "alias")
        for rel in ("alias", "alias/a.txt"):
            with self.assertRaisesRegex(Denied, "'alias' is a symlink", msg=rel):
                workspace.workspace_path(rel)
        with self.assertRaisesRegex(Denied, "symlink"):
            workspace.read_workspace_file("alias/a.txt", 100)
        self.assertEqual(workspace.read_workspace_file("real/../real/a.txt", 100), b"x")

    def test_factory_symlink_is_refused(self):
        os.symlink(self.outside(), config.WORKSPACE / ".factory")
        with self.assertRaisesRegex(Denied, "'.factory' is a symlink"):
            with workspace.factory_dir("wheels"):
                pass
        self.assertEqual(list(self.outside().iterdir()), [])

    def test_factory_subdir_symlink_is_refused(self):
        (config.WORKSPACE / ".factory").mkdir()
        os.symlink(self.outside(), config.WORKSPACE / ".factory/wheels")
        with self.assertRaisesRegex(Denied, "'.factory/wheels' is a symlink"):
            with workspace.factory_dir("wheels"):
                pass
        self.assertEqual(list(self.outside().iterdir()), [])

    def test_dangling_gitignore_symlink_is_replaced_not_followed(self):
        (config.WORKSPACE / ".factory").mkdir()
        target = self.outside() / "planted"
        os.symlink(target, config.WORKSPACE / ".factory/.gitignore")
        with workspace.factory_dir("incoming"):
            pass
        self.assertFalse(target.exists(), ".gitignore was written through a symlink")
        gi = config.WORKSPACE / ".factory/.gitignore"
        self.assertFalse(gi.is_symlink())
        self.assertEqual(gi.read_text(), "*\n")

    def test_atomic_write_replaces_links_instead_of_writing_through(self):
        victim = self.outside() / "victim"
        victim.write_text("precious")
        with workspace.factory_dir("incoming") as d:
            os.symlink(victim, config.WORKSPACE / ".factory/incoming/sym")
            os.link(victim, config.WORKSPACE / ".factory/incoming/hard")
            for name in ("sym", "hard"):
                with workspace.atomic_write(d, name) as f:
                    f.write(b"new")
        self.assertEqual(victim.read_text(), "precious")
        self.assertEqual((config.WORKSPACE / ".factory/incoming/sym").read_bytes(), b"new")
        self.assertEqual(sorted(os.listdir(config.WORKSPACE / ".factory/incoming")), ["hard", "sym"])

    def test_reads_are_capped_and_regular_files_only(self):
        (config.WORKSPACE / "big").write_bytes(b"x" * 11)
        with self.assertRaisesRegex(Denied, "'big' is larger than 10 bytes; try less"):
            workspace.read_workspace_file("big", 10, hint="try less")
        (config.WORKSPACE / "d").mkdir()
        os.mkfifo(config.WORKSPACE / "fifo")
        for rel in ("d", "fifo"):
            with self.assertRaisesRegex(Denied, "not a regular file", msg=rel):
                workspace.read_workspace_file(rel, 10)
        with self.assertRaises(Denied):
            workspace.read_workspace_file("../state/x", 10)

    def test_copy_workspace_file_is_capped(self):
        (config.WORKSPACE / "f").write_bytes(b"y" * 5)
        dest = Path(self.tmp.name) / "copy"
        self.assertEqual(workspace.copy_workspace_file("f", dest, 5), 5)
        self.assertEqual(dest.read_bytes(), b"y" * 5)
        with self.assertRaisesRegex(Denied, "larger than 4 bytes"):
            workspace.copy_workspace_file("f", Path(self.tmp.name) / "copy2", 4)

    def test_run_capped(self):
        code, out, _ = workspace.run_capped(["printf", "abc"], cwd=Path(self.tmp.name), limit=3)
        self.assertEqual((code, out), (0, b"abc"))
        _, out, _ = workspace.run_capped(["yes"], cwd=Path(self.tmp.name), limit=1000)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
