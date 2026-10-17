# Reviewer sandbox template: Claude Code base + ruff, yarn, factory-test and
# factory-lane, baked in at build time because the reviewer kit's egress policy
# only allows api.anthropic.com. Built with the repo root as the build context
# (see scripts/00-build-templates.sh).
# Pinned to the multi-arch index digest (linux/amd64 + linux/arm64) so every
# teammate builds from the identical base. To upgrade, run:
#   docker buildx imagetools inspect docker/sandbox-templates:claude-code
# and replace the digest with the top-level "Digest:" value (keep in sync with
# templates/developer/Dockerfile).
FROM docker/sandbox-templates:claude-code@sha256:868e3f5ef10579e06903d42e86448851ed59319ab8b9fba6dea2772c449bb2a6

ARG RUFF_VERSION=0.16.8

# System Python is externally managed (PEP 668), so install ruff as an
# isolated uv tool with its binary on the system PATH.
USER root
RUN for t in git node npm rsync sha256sum; do command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }; done
RUN UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin \
      uv tool install "ruff==${RUFF_VERSION}" \
 && ruff --version

# yarn v1 (the project's scripts and git hooks call it; corepack can't install
# it as the agent user) and factory-test, which runs tests in a sandbox-local
# copy with dependencies from the broker's npm_fetch mirror. The yarn tarball
# is pinned by SHA-256 and installed without its preinstall script.
ARG YARN_VERSION=1.22.22
ARG YARN_SHA256=c17d3797fb9a9115bf375e31bfd30058cac6bc9c3b8807a3d8cb2094794b51ca
USER root
RUN curl -fsSLo /tmp/yarn.tgz "https://registry.npmjs.org/yarn/-/yarn-${YARN_VERSION}.tgz" \
 && echo "${YARN_SHA256}  /tmp/yarn.tgz" | sha256sum -c - \
 && npm install -g --ignore-scripts --no-audit --no-fund /tmp/yarn.tgz \
 && rm /tmp/yarn.tgz \
 && [ "$(yarn --version)" = "${YARN_VERSION}" ]
# Last, so editing the scripts reuses the cached yarn layer. factory-lane
# manages the role's private git clones of the read-only project (lanes) and
# the reviewer's checkouts of branches to review.
COPY --chmod=0555 templates/agent/factory-test /usr/local/bin/factory-test
COPY --chmod=0555 templates/agent/factory-lane /usr/local/bin/factory-lane
USER agent
