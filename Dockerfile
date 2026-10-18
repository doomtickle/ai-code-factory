# Passdesk broker image: the pinned shell-docker base (git, Python 3.14, pip,
# npm) plus the broker server and its policy. Built with this repo's root as
# the build context.
# Pinned to the multi-arch index digest (linux/amd64 + linux/arm64). To upgrade, run:
#   docker buildx imagetools inspect docker/sandbox-templates:shell-docker
# and replace the digest with the top-level "Digest:" value.
FROM docker/sandbox-templates:shell-docker@sha256:1560168ac5fb9ce23d413c878349334c5845c07e264cd675d7867f0c78ad1761

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
