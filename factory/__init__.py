"""The factory host CLI, in Python (stdlib only, 3.9+).

bin/factory is a thin bash shim that hands the commands listed in its
PORTED_COMMANDS to `main` in factory/cli.py and keeps running the rest itself
until they are ported (docs/py-cli-migration.md). The dispatch bridge imports
factory.conf, factory.roles and factory.repos so the role.conf parser and
project_repos exist once.
"""
