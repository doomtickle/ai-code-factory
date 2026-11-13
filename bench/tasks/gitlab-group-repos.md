---
name: gitlab-group-repos
base: 9261676acfcf1b788464ce8f7ac872fe51b5289d
reference: 433ade911dce1ea39a235d00be27a8a1f53a2a95
tier: full
area: broker
---
Work only from commit 9261676acfcf1b788464ce8f7ac872fe51b5289d (`base`). Do not look at any later history, other branches or refs, or the reflog; solve this from the code as it stands at `base`. Read `CLAUDE.md` and `broker/CLAUDE.md` first: this is gateway policy and credentials code.

Gateway: GitLab group entries in `[git].repos`, and group access tokens.

Goal: owner decision of 30 Oct 2026. The owner works across many projects in one GitLab group; every switch means editing `[git].repos`, saving a new project access token and running `factory update`. Let one entry cover a whole group and let a GitLab group access token be the credential, so one policy edit and one token cover the group.

Files: `broker/passdesk/tools/git.py` (the repo allowlist and its checks), `broker/passdesk/doctor.py`, `broker/policy.toml`, `broker/tests/test_gitlab.py`, `factory/policy.py` (the host's reader and validator of `[git].repos`), `factory/commands/doctor.py`, `factory/commands/status.py`, `factory/hints.py`, `factory/build_templates.py`, and their tests; docs: `README.md`, `broker/README.md`, `broker/docs/decisions.md` (dated decision entry), `config.example/policy.toml`, `CLAUDE.md` and `broker/CLAUDE.md` (the one wording of the credential exception: "project access token" becomes "project or group access token", nothing else in those rules changes).

Known facts:
- Syntax: `"<gitlab host>/<group path>/*"`, for example `"gitlab.example.com/my-group/*"` or `"gitlab.example.com/my-group/sub/*"`, with `[gitlab] url = "https://gitlab.example.com/"` (trailing `/` required). The entry's host must be that instance. It matches any project under the group at any depth, subgroups included, case-insensitively like exact entries, and on whole path components: `my-group/*` covers `my-group/x` and `my-group/sub/x`, not `my-group-evil/x`, `my-group.x/y` or `my-group` itself.
- `*` is valid only as the final `/*` after a host and at least one group segment (GitLab allows a top group plus 20 subgroups). Refused: `host/*` (a whole instance), `**`, a `*` anywhere else, an empty entry, `.` or `..` or `-`-leading segments, a `.git` or `.atom` ending, a bad host or port. GitHub never gets a wildcard: `owner/*` is refused with a clear error ("group entries are for GitLab only"); list GitHub repos one by one.
- One matcher serves every consumer: the repo check used by `git_fetch`, `git_push`, `gitlab_create_mr` and the completion of any held write (a held MR is checked again against the group when it is approved), and the doctor. No consumer does its own exact membership. The concrete repo name still goes through the name validation first, and the remote check still confirms it is on `[gitlab].url`; matching never sees an unvalidated name. Name validation must not let a name ending in a newline through.
- A malformed entry matches nothing, even if nobody ran a validator (fail closed). The doctor, and on the host `factory/policy.py` (used by the image build, `factory start`, `status` and `doctor`), report malformed entries with the reason; a bad entry stops the image build before anything is built, and shows as a hint at start. Reasons the tests will read: a whole-instance entry says "name a group, not the whole instance"; a GitHub wildcard says "group entries are for GitLab only".
- The allowlist stays baked into the image from `policy.toml`: never read from a runtime-editable file or a tool argument. Nothing changes for `push_branch_prefix`, protected branches, approval holds or how the token file is delivered and scoped.
- The host never imports broker code. The two validators (broker and host) must agree: keep one shared table of entries and expected verdicts in `broker/tests/repos_entries.json` that both test suites read.
- Doctor: a group entry is shown as a group. The group token is checked by its role in the group, not by pushing: the token's own user is looked up once (`GET api/v4/user`), then its membership in each group (`GET api/v4/groups/<url-encoded path>/members/all/<id>`); `access_level` 30 (Developer) or above passes. Anything short of a confirmed Developer or above is a warning, never an OK: lower role, not a member (likely a project access token, which reaches one project; say so and give the fix), a failed or odd `/user` answer, or any other failure ("its group role could not be confirmed"). The token goes only to the GitLab API, with no redirects followed, and is never shown. With no token the group check is skipped and says so. A group-only policy needs no GitHub token.
- Unverified (no web access): that a real group access token can read its own `/user` and `members/all`. Say so in the decision entry.

Constraints: broker code is standard library only, Python 3.11+; host code Python 3.9. No new dependency. Never write a token into a message, log or test output.

Done when:
- A `[git].repos` group entry allows fetch, push and merge request creation for any project under that group (subgroups included) and nothing outside it, including lookalike names and the group itself; a held MR is re-checked against the group on completion.
- Every malformed wildcard form listed above is refused by the broker's doctor and by the host's validator with the stated reasons, matches nothing in the broker, and a bad entry stops the image build before anything is built.
- Exact entries and GitHub entries behave as before; GitHub wildcards are refused.
- The doctor reports a group's token by its role in the group, with warnings for each failure mode above, and never prints the token.
- `status` and `doctor` show a group entry as a group; a bad entry is a hint or warning with its fix.
- The docs and the decision entry describe the syntax, limits and choices; the credential wording covers group tokens.
Tests required: the shared entry table read by both suites; matching, boundaries, the newline name, name-check-before-match, other-instance and GitHub refusals; fetch, push and MR through a group and refused outside it; the doctor's group-token cases (including no redirect following and no token in output); the host policy reader and its problems; build refusal and start hint. Run the broker suite and the touched `factory/` tests (the full suite once, per Risk).
Out of scope: wildcards for GitHub, whole-instance wildcards, changing approval holds, runtime-editable allowlists, other forges.
Risk: high
