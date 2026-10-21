# Developer sandbox template: pinned Claude Code base. Its tools (ripgrep, git,
# jq, curl, node, rsync) come from the pinned base, plus pinned yarn,
# factory-test and factory-lane below, so nothing is installed at sandbox start
# and every teammate gets byte-identical tooling. Built with the repo root as
# the build context (see scripts/00-build-templates.sh).
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
# Last, so editing the scripts reuses the cached layers above. factory-lane
# manages the role's private git clones of the read-only project (lanes) and
# the reviewer's checkouts of branches to review.
# Chrome for Testing's headless shell, so a role can render a page it built
# (the docs, the dashboard files) to a PNG and look at it with Read. It loads
# file:// and localhost only: the sandbox's network policy (the kit) is
# unchanged, so it can't fetch anything else. Pinned by exact version and
# SHA-256 (the builds are immutable per version; to upgrade, pick a version
# from https://googlechromelabs.github.io/chrome-for-testing/ and record the
# zip's sha256sum). Its runtime libraries and the two font packages come from
# the Ubuntu repositories the pinned base image is configured with, at
# whatever version those serve at build time: like git in the broker image,
# the base digest is their pin. `factory-shot` (below) wraps the flags it
# needs inside a microVM (no Chrome sandbox, no GPU, no /dev/shm).
ARG CHROME_VERSION=156.0.8078.47
ARG CHROME_SHA256=0d5acc62c3e213f8c87d3c298d66ffcf9a236d12ea4b9f85318bf85dbc279daa
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 \
      libdbus-1-3 libdrm2 libexpat1 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 \
      libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
      fonts-liberation fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSLo /tmp/chs.zip "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-headless-shell-linux64.zip" \
 && echo "${CHROME_SHA256}  /tmp/chs.zip" | sha256sum -c - \
 && unzip -q /tmp/chs.zip -d /opt \
 && rm /tmp/chs.zip \
 && mv /opt/chrome-headless-shell-linux64 /opt/chrome-headless-shell \
 && chmod -R a+rX /opt/chrome-headless-shell \
 && ln -s /opt/chrome-headless-shell/chrome-headless-shell /usr/local/bin/chrome-headless-shell \
 && [ "$(chrome-headless-shell --version 2>/dev/null | grep -o "[0-9][0-9.]*")" = "${CHROME_VERSION}" ]
COPY --chmod=0555 templates/agent/factory-shot /usr/local/bin/factory-shot
COPY --chmod=0555 templates/agent/factory-test /usr/local/bin/factory-test
COPY --chmod=0555 templates/agent/factory-lane /usr/local/bin/factory-lane
USER agent
