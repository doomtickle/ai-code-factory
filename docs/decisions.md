# Decision log

Dated entries, for the user to review. Newest first.

## 2026-10-16

- **Extraction.** Pulled out of `ai-code-factory` with history preserved, via
  a subtree split of its former `broker/` directory rather than a plain copy.
- **Consumption.** `ai-code-factory` consumes this repository as a git
  submodule at `broker/`, pinned by commit — not by an image digest, because
  there is no registry yet to push a built image to. See that repo's
  `docs/broker-extraction.md` (Phase 2) for the plan to move to a
  digest-pinned image once one exists.
- **Submodule URL.** `ai-code-factory`'s `.gitmodules` points at the relative
  URL `../passdesk-broker.git`, so it resolves next to whatever host
  `ai-code-factory`'s own `origin` is on (GitHub, in this case). **Not done
  yet:** the user needs to create the `passdesk-broker` repository on GitHub
  and push both repositories there before the submodule link is live for
  anyone other than this sandbox.
- **Naming.** The project and Python package are renamed from `agentbroker`
  to **Passdesk** (package `passdesk/`), and the MCP server now reports
  itself as `"passdesk"` (`serverInfo.name`, see `passdesk/core/config.py`).
  Deployment identity is kept as-is on the consumer side: `ai-code-factory`
  still calls the sandbox `factory-broker` and uses the paths
  `/opt/factory-broker` and `/etc/factory-broker` — those are its deployment
  choices, not this project's identity.
- **License.** Apache-2.0, copyright holder "Daron Adkins" (**the user
  should confirm this is correct**).
