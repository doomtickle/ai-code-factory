---
name: held-result-shapes
base: 3cedbbe73f3589aa7bd05df3e919cda1446261ce
reference: 45fa02180d7a96b05d45920e6182a18896dc13ab
tier: smoke
area: docs
---
Work only from commit 3cedbbe73f3589aa7bd05df3e919cda1446261ce (`base`). Do not look at any later history, other branches or refs, or the reflog; solve this from the files as they stand at `base`.

Docs: describe the three result shapes of a held write, so a developer never mistakes a finished write for an unfinished one.

Goal: when the owner has holds on, a `git_push`, `github_create_pr` or `gitlab_create_mr` result takes one of three shapes. `roles/developer/instructions.md` describes only one of them (the queued one) and the README's description of holds is incomplete. Developers have reported a push as "queued" that had in fact completed, because its result mentions "held for approval". Rewrite the developer instructions so the developer reads the first line and knows what to do for each shape, and bring the README's holds paragraph in line.

Files: `roles/developer/instructions.md`, `README.md` (the paragraph on approval holds only).

Known facts (the gateway is not in this checkout at `base`; these are its behaviours):
- Not yet approved: the result starts `Queued for approval (id X); nothing has been sent yet...`. Nothing was sent. Once the owner approves, the gateway makes the write itself. The developer must not wait, poll or loop; it reports the branch or PR as queued for approval with that id. The Coordinator may later send the developer back to call the same tool once with the same arguments to collect the outcome.
- Approved, the gateway is making the write now: the first line starts `Approved (id X); the gateway is making this write now`. Under way: report that.
- Approved while the call was still waiting: the call completes as usual. The result's first line states the write and the approval together, for example a push line followed by `held for approval (id X), approved by <user> at <time> after <N> s`. The write is done: carry on (for example, open the PR). Older gateways print the same approval on a separate last line after a first line that starts `Pushed` or `Opened`; that is also complete.
- Only a first line starting `Queued` or `Approved` means the write is not finished. A result that starts with `Pushed` or `Opened` and contains `approved by` is done even though it says "held for approval".
- `github_create_issue` and plugin tools keep the older flow (the call returns an approval id and asks the agent to retry later); leave that description as it is.

Constraints: keep the instructions short and in the file's existing voice; no new rules beyond these shapes. Don't touch other role files.

Done when:
- `roles/developer/instructions.md` lists all three shapes, each with the first-line text to look for and what the developer does or reports.
- It states that only a first line of `Queued` or `Approved` means unfinished, and that a first line of `Pushed` or `Opened` that contains `approved by` means finished.
- The README's holds paragraph shows the under-way and completed shapes alongside the queued one, and still says issues and plugin tools use the older flow.
Tests required: none; check by reading that every example string appears exactly as in Known facts and the docs build (`python3 scripts/docs.py --root . --out /tmp/docs.html`) still succeeds.
Out of scope: the gateway's code or its result text, the reviewer and Coordinator instructions, any other README section.
Risk: trivial
