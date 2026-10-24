"""Tests for factory/repos.py (project_repos). Fixtures are the ones scripts/test_project_repos.sh
runs against common.sh, dispatch/server.py and factory-lane."""

import os
import subprocess
import tempfile
import unittest

from factory.repos import project_repos

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def mkdir(*parts):
    os.makedirs(os.path.join(*parts), exist_ok=True)


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class ProjectReposTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.call, ["rm", "-rf", self.tmp])

    def folder(self):
        f = os.path.join(self.tmp, "folder")
        for name in ("normal", "Capital", "real-repo"):
            mkdir(f, name, ".git")
        mkdir(f, "gitfile")
        write(os.path.join(f, "gitfile", ".git"), "gitdir: /elsewhere\n")
        mkdir(f, ".hidden", ".git")
        mkdir(f, "no-git", "src")
        mkdir(f, "outer", "inner", ".git")
        mkdir(f, "new\nline", ".git")
        os.symlink("real-repo", os.path.join(f, "link-to-repo"))
        return f

    def test_a_folder_of_repos(self):
        # Capital sorts before lowercase: byte order, not locale order.
        self.assertEqual(project_repos(self.folder()), ["Capital", "gitfile", "normal", "real-repo"])

    def test_accepts_a_path_object(self):
        import pathlib
        self.assertEqual(project_repos(pathlib.Path(self.folder())), ["Capital", "gitfile", "normal", "real-repo"])

    def test_a_project_with_a_git_directory_is_just_dot(self):
        p = os.path.join(self.tmp, "repo-dir")
        mkdir(p, ".git")
        mkdir(p, "sub", ".git")
        self.assertEqual(project_repos(p), ["."])

    def test_a_project_with_a_gitfile_is_just_dot(self):
        p = os.path.join(self.tmp, "repo-file")
        mkdir(p, "sub", ".git")
        write(os.path.join(p, ".git"), "gitdir: /elsewhere\n")
        self.assertEqual(project_repos(p), ["."])

    def test_no_repos(self):
        mkdir(self.tmp, "empty", "docs")
        self.assertEqual(project_repos(os.path.join(self.tmp, "empty")), [])

    def test_missing_project(self):
        self.assertEqual(project_repos(os.path.join(self.tmp, "nope")), [])

    def test_a_file_is_not_a_project(self):
        write(os.path.join(self.tmp, "file"), "x")
        self.assertEqual(project_repos(os.path.join(self.tmp, "file")), [])

    def test_a_repo_that_is_a_plain_file_is_skipped(self):
        p = os.path.join(self.tmp, "p")
        mkdir(p)
        write(os.path.join(p, "afile"), "")
        self.assertEqual(project_repos(p), [])

    def test_a_symlinked_project_folder_is_followed(self):
        real = self.folder()
        link = os.path.join(self.tmp, "project-link")
        os.symlink(real, link)
        self.assertEqual(project_repos(link), ["Capital", "gitfile", "normal", "real-repo"])

    def test_non_utf8_names_sort_by_bytes(self):
        p = os.path.join(self.tmp, "bytes")
        mkdir(p)
        for raw in (b"b\xff", b"a", b"\xc3\xa9"):
            os.makedirs(os.path.join(os.fsencode(p), raw, b".git"))
        self.assertEqual([os.fsencode(r) for r in project_repos(p)], [b"a", b"b\xff", b"\xc3\xa9"])

    def test_matches_the_bash_and_sandbox_implementations(self):
        folders = [self.folder()]
        for name, git in (("dir", "d"), ("file", "f"), ("none", None)):
            p = os.path.join(self.tmp, "parity-" + name)
            mkdir(p, "sub", ".git")
            if git == "d":
                mkdir(p, ".git")
            elif git == "f":
                write(os.path.join(p, ".git"), "gitdir: /elsewhere\n")
            folders.append(p)
        for folder in folders:
            via_common = subprocess.run(
                ["bash", "-c", '. "$1/scripts/lib/common.sh"; project_repos "$2"', "_", ROOT, folder],
                stdout=subprocess.PIPE, check=True).stdout.decode().splitlines()
            via_lane = subprocess.run(
                [os.path.join(ROOT, "templates/agent/factory-lane"), "repos", folder],
                stdout=subprocess.PIPE, check=True).stdout.decode().splitlines()
            self.assertEqual(project_repos(folder), via_common, folder)
            self.assertEqual(sorted(via_lane), sorted(via_common), folder)


if __name__ == "__main__":
    unittest.main()
