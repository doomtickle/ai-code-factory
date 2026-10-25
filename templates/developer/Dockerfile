# Developer sandbox template: pinned Claude Code base. Its tools (ripgrep, git,
# jq, curl, node, rsync, python3, uv) come from the pinned base, plus pinned
# ruff, yarn, factory-test and factory-lane below, so nothing is installed at sandbox start
# and every teammate gets byte-identical tooling. Built with the repo root as
# the build context (see scripts/00-build-templates.sh).
# Pinned to the multi-arch index digest (linux/amd64 + linux/arm64). To upgrade, run:
#   docker buildx imagetools inspect docker/sandbox-templates:claude-code
# and replace the digest with the top-level "Digest:" value (keep in sync with
# templates/reviewer/Dockerfile).
FROM docker/sandbox-templates:claude-code@sha256:868e3f5ef10579e06903d42e86448851ed59319ab8b9fba6dea2772c449bb2a6

# Fail the build if a digest bump drops a tool the developer agent relies on.
RUN for t in rg git jq curl node npm rsync sha256sum python3 uv; do command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }; done

# ruff, the linter the developer instructions ask for on every finished change
# (keep the version in sync with templates/reviewer/Dockerfile). System Python
# is externally managed (PEP 668), so install it as an isolated uv tool with
# its binary on the system PATH. Pinned by version only: uv tool install
# can't verify a checksum, like the reviewer image.
ARG RUFF_VERSION=0.16.8
USER root
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
# ShellCheck, so a role (no egress) can run scripts/lint.sh, which prefers a
# shellcheck on PATH at the pinned version. The version and both hashes come from
# scripts/lib/common.sh via 00-build-templates.sh and have no default here, so a
# build without them fails. The asset is chosen by `dpkg --print-architecture`
# (amd64 on WSL2/Linux, arm64 on Apple silicon) and checked before extracting.
ARG SHELLCHECK_VERSION
ARG SHELLCHECK_LINUX_X86_64_SHA256
ARG SHELLCHECK_LINUX_AARCH64_SHA256
USER root
RUN : "${SHELLCHECK_VERSION:?build arg SHELLCHECK_VERSION is required}" \
      "${SHELLCHECK_LINUX_X86_64_SHA256:?build arg SHELLCHECK_LINUX_X86_64_SHA256 is required}" \
      "${SHELLCHECK_LINUX_AARCH64_SHA256:?build arg SHELLCHECK_LINUX_AARCH64_SHA256 is required}" \
 && case "$(dpkg --print-architecture)" in \
      amd64) sc_asset=linux.x86_64; sc_sha256="${SHELLCHECK_LINUX_X86_64_SHA256}" ;; \
      arm64) sc_asset=linux.aarch64; sc_sha256="${SHELLCHECK_LINUX_AARCH64_SHA256}" ;; \
      *) echo "no ShellCheck build for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac \
 && curl -fsSLo /tmp/shellcheck.tar.xz "https://github.com/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/shellcheck-v${SHELLCHECK_VERSION}.${sc_asset}.tar.xz" \
 && echo "${sc_sha256}  /tmp/shellcheck.tar.xz" | sha256sum -c - \
 && tar -xJf /tmp/shellcheck.tar.xz -C /tmp \
 && install -m 0755 "/tmp/shellcheck-v${SHELLCHECK_VERSION}/shellcheck" /usr/local/bin/shellcheck \
 && rm -rf /tmp/shellcheck.tar.xz "/tmp/shellcheck-v${SHELLCHECK_VERSION}" \
 && shellcheck --version | grep -qx "version: ${SHELLCHECK_VERSION}"
# Last, so editing the scripts reuses the cached layers above. factory-lane
# manages the role's private git clones of the read-only project (lanes) and
# the reviewer's checkouts of branches to review.
# Chrome for Testing's headless shell, so a role can render a page it built
# (the docs, the dashboard files) to a PNG and look at it with Read. It loads
# file:// and localhost only: the sandbox's network policy (the kit) is
# unchanged, so it can't fetch anything else. The image is built for the
# host's architecture (amd64 on WSL2/Linux, arm64 on Apple silicon), so the
# zip is chosen by `dpkg --print-architecture` and each one is pinned by exact
# version and its own SHA-256 (the builds are immutable per version; to
# upgrade, pick a version from
# https://googlechromelabs.github.io/chrome-for-testing/ that lists both
# linux64 and linux-arm64, and record both zips' sha256sum). Its runtime libraries and the two font packages come from
# the Ubuntu repositories the pinned base image is configured with, at
# whatever version those serve at build time: like git in the broker image,
# the base digest is their pin. `factory-shot` (below) wraps the flags it
# needs inside a microVM (no Chrome sandbox, no GPU, no /dev/shm).
ARG CHROME_VERSION=156.0.8078.47
ARG CHROME_SHA256_AMD64=0d5acc62c3e213f8c87d3c298d66ffcf9a236d12ea4b9f85318bf85dbc279daa
ARG CHROME_SHA256_ARM64=955059fa7020d7be85eb503e21339fd4d718f51f5996d3b60633fbf908bda132
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 \
      libdbus-1-3 libdrm2 libexpat1 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 \
      libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
      fonts-liberation fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && case "$(dpkg --print-architecture)" in \
      amd64) chs_platform=linux64; chs_sha256="${CHROME_SHA256_AMD64}" ;; \
      arm64) chs_platform=linux-arm64; chs_sha256="${CHROME_SHA256_ARM64}" ;; \
      *) echo "no Chrome for Testing build for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac \
 && curl -fsSLo /tmp/chs.zip "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/${chs_platform}/chrome-headless-shell-${chs_platform}.zip" \
 && echo "${chs_sha256}  /tmp/chs.zip" | sha256sum -c - \
 && unzip -q /tmp/chs.zip -d /opt \
 && rm /tmp/chs.zip \
 && mv "/opt/chrome-headless-shell-${chs_platform}" /opt/chrome-headless-shell \
 && chmod -R a+rX /opt/chrome-headless-shell \
 && ln -s /opt/chrome-headless-shell/chrome-headless-shell /usr/local/bin/chrome-headless-shell \
 && [ "$(chrome-headless-shell --version 2>/dev/null | grep -o "[0-9][0-9.]*")" = "${CHROME_VERSION}" ]
COPY --chmod=0555 templates/agent/factory-shot /usr/local/bin/factory-shot
COPY --chmod=0555 templates/agent/factory-test /usr/local/bin/factory-test
COPY --chmod=0555 templates/agent/factory-lane /usr/local/bin/factory-lane
USER agent
