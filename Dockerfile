# Passdesk images. Two targets, built with this repo's root as the context:
#
#   * `standalone` (--target standalone): a minimal image for running
#     Passdesk on plain Docker (BROKER_DEPLOYMENT=standalone, no sbx).
#   * `sbx` (the last stage, so plain `docker build` with no --target still
#     produces it): the pinned shell-docker base plus the broker, for the
#     Docker Sandboxes deployment the AI Code Factory builds. Unchanged by
#     the standalone target.
#
# Both take two optional named build contexts (--build-context NAME=DIR):
#   * `factory-config`: a directory holding this deployment's policy.toml
#     overlay (baked in as policy.local.toml, merged table by table).
#   * `plugins`: optional plugin packages (e.g. a deployment's commercial
#     add-on); they sit on PYTHONPATH but load only when PASSDESK_PLUGINS
#     names them (passdesk/core/plugins.py).
# When a context isn't given, the default stages of the same name below
# stand in (an empty overlay, no plugins), so `docker build` alone works; a
# --build-context of the same name overrides the stage.

# --- default build contexts ------------------------------------------------------

# An empty plugins directory.
FROM scratch AS plugins

# An empty policy overlay. Built on the standalone base rather than scratch
# because scratch can't RUN.
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS factory-config
RUN : > /policy.toml

# --- standalone ------------------------------------------------------------------

# Pinned to the multi-arch index digest (linux/amd64 + linux/arm64). To upgrade, run:
#   docker buildx imagetools inspect python:3.13-slim-bookworm
# and replace the digest with the top-level "Digest:" value (here and in the
# factory-config stage above, which shares it so there is one pull).
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS standalone

# git is the one thing the tools need that the base lacks (git_fetch,
# git_push). openssl is for minting a self-signed certificate inside the
# container (README, "TLS"). Neither can be pinned by digest: they come from
# the Debian repositories the pinned base image is configured with, at
# whatever version those serve at build time. The base image's own digest
# is the pin; the versions installed are printed in the build log.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssl \
 && rm -rf /var/lib/apt/lists/* \
 && git --version && openssl version

# A dedicated non-root user. The UID/GID match the first user on most Linux
# hosts so the bind-mounted workspace's exchange directory is writable
# without extra setup; override with --build-arg for another host UID.
ARG PASSDESK_UID=1000
ARG PASSDESK_GID=1000
RUN groupadd --gid "${PASSDESK_GID}" passdesk \
 && useradd --uid "${PASSDESK_UID}" --gid passdesk --home-dir /var/lib/passdesk --no-create-home \
            --shell /usr/sbin/nologin passdesk

ARG INSTALL_DIR=/opt/passdesk
COPY server.py policy.toml run.sh ${INSTALL_DIR}/
COPY passdesk ${INSTALL_DIR}/passdesk
COPY --from=factory-config policy.toml ${INSTALL_DIR}/policy.local.toml
COPY --from=plugins / ${INSTALL_DIR}/plugins/
# Read-only for everyone, root-owned: the server (running as passdesk) can't
# change its own code or the baked-in policy. The check imports every tool
# and parses the baked-in policy with its overlay.
RUN find ${INSTALL_DIR} -type d -exec chmod 0555 {} + \
 && find ${INSTALL_DIR} -type f -exec chmod 0444 {} + && chmod 0555 ${INSTALL_DIR}/run.sh \
 && cd ${INSTALL_DIR} \
 && PYTHONDONTWRITEBYTECODE=1 python3 -c "import passdesk.tools; from passdesk.core import policy; policy.load_policy()" \
 && for t in git python3 openssl; do command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }; done

# State (tokens, grants, audit log, git mirrors under ~/mirrors) and the
# credential files (README, "Configuring credentials"; Docker secrets under
# /run/secrets are found too). Both owned by passdesk, mode 0700, so only
# that user reads them. Mount a volume at /var/lib/passdesk to keep state
# across container recreation.
RUN mkdir -p /var/lib/passdesk /etc/passdesk/secrets /workspace \
 && chown passdesk:passdesk /var/lib/passdesk /etc/passdesk/secrets \
 && chmod 0700 /var/lib/passdesk /etc/passdesk/secrets

# /opt/passdesk on PYTHONPATH too, so `python3 -m passdesk ...` (principal
# CLI) and `python3 -m passdesk.doctor` work from any directory, e.g. under
# `docker compose exec`.
ENV BROKER_DEPLOYMENT=standalone \
    BROKER_STATE=/var/lib/passdesk \
    BROKER_WORKSPACE=/workspace \
    BROKER_SECRETS_DIR=/etc/passdesk/secrets \
    HOME=/var/lib/passdesk \
    PYTHONPATH=${INSTALL_DIR}:${INSTALL_DIR}/plugins \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR ${INSTALL_DIR}
USER passdesk
EXPOSE 8765
# No run.sh restart loop here: a container runtime restarts the process
# (compose `restart:`), and crashes go to the container log.
CMD ["python3", "/opt/passdesk/server.py"]

# --- sbx (default target; the AI Code Factory builds this one) -------------------

# Passdesk broker image: the pinned shell-docker base (git, Python 3.14, pip,
# npm) plus the broker server and its policy.
# Pinned to the multi-arch index digest (linux/amd64 + linux/arm64). To upgrade, run:
#   docker buildx imagetools inspect docker/sandbox-templates:shell-docker
# and replace the digest with the top-level "Digest:" value.
FROM docker/sandbox-templates:shell-docker@sha256:1560168ac5fb9ce23d413c878349334c5845c07e264cd675d7867f0c78ad1761 AS sbx

# Where the broker's files live in the image. Overridable (--build-arg
# INSTALL_DIR=...) for a deployment that doesn't want this path; the default
# is what the AI Code Factory and every existing deployment already expect.
ARG INSTALL_DIR=/opt/factory-broker

USER root
COPY server.py policy.toml run.sh ${INSTALL_DIR}/
COPY passdesk ${INSTALL_DIR}/passdesk
# A consumer's local policy overlay (empty if none; passed as the
# factory-config build context, see ai-code-factory's scripts/00-build-templates.sh).
COPY --from=factory-config policy.toml ${INSTALL_DIR}/policy.local.toml
# Optional plugin packages (e.g. a deployment's commercial add-on), passed as
# the "plugins" build context: an empty directory when there are none. They
# sit on PYTHONPATH but load only when PASSDESK_PLUGINS names them
# (passdesk/core/plugins.py).
COPY --from=plugins / ${INSTALL_DIR}/plugins/
ENV PYTHONPATH=${INSTALL_DIR}/plugins
# Read-only for everyone; the server runs with PYTHONDONTWRITEBYTECODE (run.sh)
# since it can't write __pycache__ here anyway. The check imports every tool
# and parses the baked-in policy.
RUN find ${INSTALL_DIR} -type d -exec chmod 0555 {} + \
 && find ${INSTALL_DIR} -type f -exec chmod 0444 {} + && chmod 0555 ${INSTALL_DIR}/run.sh \
 && cd ${INSTALL_DIR} \
 && PYTHONDONTWRITEBYTECODE=1 python3 -c "import passdesk.tools; from passdesk.core import policy; policy.load_policy()" \
 && for t in git python3 npm setsid pgrep; do command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }; done

# Holds the Jira/Gemini/npm-registry credential files a consumer's start script
# delivers over stdin at runtime (see CLAUDE.md's "Credentials" exception).
# Owned by "agent" (the user server.py runs as) and mode 0700 so only that
# user can read or create files here; server.py reads them fresh on every call.
RUN mkdir -p /etc/factory-broker/secrets && chown agent:agent /etc/factory-broker/secrets \
 && chmod 0700 /etc/factory-broker/secrets

USER agent
