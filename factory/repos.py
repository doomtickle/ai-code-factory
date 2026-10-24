"""The git repositories of a project, which rw roles clone as lanes.

Same listing as project_repos in scripts/lib/common.sh and templates/agent/factory-lane;
scripts/test_project_repos.sh runs them on one fixture and fails if they differ.
The dispatch bridge imports this one.
"""

import os


def _has_git(path):
    return os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git"))


def project_repos(project):
    """Relative paths of the repos: ["."] if `project` is itself a repo, else its
    non-hidden, non-symlink immediate subfolders that are repos, in byte order.
    Names with a newline are skipped. A missing or unreadable project has none."""
    project = os.fspath(project)
    if _has_git(project):
        return ["."]
    try:
        names = os.listdir(project)
    except OSError:
        return []
    repos = []
    for name in names:
        path = os.path.join(project, name)
        if (name.startswith(".") or "\n" in name or os.path.islink(path)
                or not os.path.isdir(path) or not _has_git(path)):
            continue
        repos.append(name)
    return sorted(repos, key=lambda n: n.encode("utf-8", "surrogateescape"))
