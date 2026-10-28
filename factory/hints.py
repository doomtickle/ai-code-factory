"""What the factory still needs, as a list: the one source of the start-up hints, for `factory
start` / `factory setup` (factory/commands/start.py prints them) and the dashboard's FIRST SHIFT
banner (scripts/dashboard_hints.py). Each hint is (code, text); the code names the kind, so the
dashboard can tell the first-login state from the rest.

collect() makes the same checks `factory start` always made, in the same order, with the same
words. `stale` lets a caller that already knows the out-of-date sandboxes (the dashboard's SERVICE DUE
lamp, which reads files only) skip the `sbx ls -q` that fingerprints.stale_sandboxes runs.
`keychain=False` leaves out the checks that ask the OS keychain. `roles=True` adds the check the
dashboard needs between the first /login and the RESTART: a login is stored but the role sandboxes
are not created yet (`factory start` itself creates them, so it never asks).
"""

from factory import fingerprints, keychain, policy, roles as roles_mod, sbx

STALE, LOGIN, ROLES, GITHUB, KEYCHAIN, CONFIG = "stale", "login", "roles", "github", "keychain", "config"


def _fields(line):
    """awk's default fields: split on runs of spaces and tabs."""
    return [f for f in line.replace("\t", " ").split(" ") if f]


def stale_hint(names):
    return ("out of date (made from an older image or kit): %s; run 'factory update' to recreate them"
            % " ".join(names))


def missing_message(ctx, service, message):
    """MESSAGE if the keychain has no SERVICE; if it is locked or unavailable, say that instead,
    since whether SERVICE is stored can't be told; None if it is stored."""
    found = keychain.has(ctx, service)
    if found == keychain.NOT_STORED:
        return message
    if found != keychain.FOUND:
        return "the keychain is locked or unavailable, so %s couldn't be checked; unlock it and run this again" % service
    return None


def missing_roles(ctx):
    """The role sandboxes that should exist and `sbx ls -q` does not list."""
    existing = set(sbx.sandbox_names(ctx))
    return [roles_mod.role_sandbox(r) for r in roles_mod.list_roles(ctx.paths) if roles_mod.role_sandbox(r) not in existing]


def collect(ctx, stale=None, keychain_checks=True, roles=False):
    """[(code, text)] in the order `factory start` prints them. STALE: the out-of-date sandbox
    names when the caller has them (a list, maybe empty), else they are looked up (and left out
    while FACTORY_UPDATING is set, since `factory update` recreates those sandboxes next)."""
    paths = ctx.paths
    out = []
    if stale is None:
        stale = [] if ctx.environ.get("FACTORY_UPDATING") else fingerprints.stale_sandboxes(ctx)
    if stale:
        out.append((STALE, stale_hint(stale)))
    logged_in = sbx.claude_login_stored(ctx)
    if not logged_in:
        out.append((LOGIN, "first time: type /login in the Coordinator (POWER ON on the dashboard) to sign in with your "
                           "Claude subscription, then run 'factory start' again (or press RESTART on the dashboard) to "
                           "create the role sandboxes"))
    elif roles and missing_roles(ctx):
        out.append((ROLES, "the role sandboxes are not created yet: press RESTART on the dashboard (or run 'factory "
                           "start' again) to create them"))
    if policy.git_push_enabled(paths):
        lines = ctx.capture(["sbx", "secret", "ls"]).out.rstrip("\n").split("\n")
        if any(f[:1] == ["(global)"] and f[2:3] == ["github"] for f in map(_fields, lines)):
            out.append((GITHUB, "a global GitHub token is stored, so sbx injects it for every sandbox, not just the "
                                "broker. Run 'factory secret save github', then remove the global one: sbx secret rm "
                                "github"))
        elif policy.github_repos_listed(paths) and not any(" github " in line for line in lines):
            out.append((GITHUB, "git push/PR is enabled but no GitHub token is stored: run 'factory secret save github' "
                                "(see README: Enabling git push and pull requests)"))
    if keychain_checks and policy.any_jira_tool_granted(paths):
        out.append((KEYCHAIN, missing_message(
            ctx, "factory-jira", "Jira tools are granted but no token is stored in the keychain (see README: Enabling "
                                 "Jira)")))
    if policy.any_jira_tool_granted(paths) and not policy.jira_cloud_id_set(paths):
        out.append((CONFIG, "Jira tools are granted but [jira].cloud_id is not set; set it in %s/policy.toml (see README: "
                            "Enabling Jira)" % paths.config))
    if keychain_checks and policy.npm_private_registry_needs_token(paths):
        out.append((KEYCHAIN, missing_message(
            ctx, "factory-npm-registry", "npm_fetch can't download private npm packages until a token is stored in the "
                                         "keychain (see README: Enabling a private npm registry)")))
    if keychain_checks and policy.gitlab_needs_token(paths):
        out.append((KEYCHAIN, missing_message(
            ctx, "factory-gitlab", "[gitlab].url is set but no GitLab token is stored: run 'factory secret save gitlab' "
                                   "(see README: Enabling GitLab)")))
    why = policy.gitlab_mr_unheld(paths)
    if why:
        out.append((CONFIG, "GitLab merge requests open without approval: %s (see README: Enabling approvals)" % why))
    return [(code, text) for code, text in out if text]
