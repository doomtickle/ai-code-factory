# Developer sandbox template: pinned Claude Code base. Its tools (ripgrep, git,
# jq, curl, node, rsync) come from the pinned base, plus pinned yarn and
# factory-test below, so nothing is installed at sandbox start and every
# teammate gets byte-identical tooling. Built with the repo root as the build
# context (see scripts/00-build-templates.sh).
# Pinned to the multi-arch index digest (linux/amd64 + linux/arm64). To upgrade, run:
#   docker buildx imagetools inspect docker/sandbox-templates:claude-code
# and replace the digest with the top-level "Digest:" value (keep in sync with
# templates/reviewer/Dockerfile).
FROM docker/sandbox-templates:claude-code@sha256:868e3f5ef10579e06903d42e86448851ed59319ab8b9fba6dea2772c449bb2a6

# Fail the build if a digest bump drops a tool the developer agent relies on.
RUN for t in rg git jq curl node npm rsync sha256sum; do command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }; done

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
# Last, so editing the script reuses the cached yarn layer.
COPY --chmod=0555 templates/agent/factory-test /usr/local/bin/factory-test
USER agent
