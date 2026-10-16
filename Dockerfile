# Passdesk broker image: the pinned shell-docker base (git, Python 3.14, pip,
# npm) plus the broker server and its policy. Built with this repo's root as
# the build context.
# Pinned to the multi-arch index digest (linux/amd64 + linux/arm64). To upgrade, run:
#   docker buildx imagetools inspect docker/sandbox-templates:shell-docker
# and replace the digest with the top-level "Digest:" value.
FROM docker/sandbox-templates:shell-docker@sha256:1560168ac5fb9ce23d413c878349334c5845c07e264cd675d7867f0c78ad1761

USER root
COPY server.py policy.toml run.sh /opt/factory-broker/
COPY passdesk /opt/factory-broker/passdesk
# A consumer's local policy overlay (empty if none; passed as the
# factory-config build context, see ai-code-factory's scripts/00-build-templates.sh).
COPY --from=factory-config policy.toml /opt/factory-broker/policy.local.toml
# Read-only for everyone; the server runs with PYTHONDONTWRITEBYTECODE (run.sh)
# since it can't write __pycache__ here anyway. The check imports every tool
# and parses the baked-in policy.
RUN find /opt/factory-broker -type d -exec chmod 0555 {} + \
 && find /opt/factory-broker -type f -exec chmod 0444 {} + && chmod 0555 /opt/factory-broker/run.sh \
 && cd /opt/factory-broker \
 && PYTHONDONTWRITEBYTECODE=1 python3 -c "import passdesk.tools; from passdesk.core import policy; policy.load_policy()" \
 && for t in git python3 npm setsid pgrep; do command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }; done

# Holds the Jira/Gemini/npm-registry credential files a consumer's start script
# delivers over stdin at runtime (see CLAUDE.md's "Credentials" exception).
# Owned by "agent" (the user server.py runs as) and mode 0700 so only that
# user can read or create files here; server.py reads them fresh on every call.
RUN mkdir -p /etc/factory-broker/secrets && chown agent:agent /etc/factory-broker/secrets \
 && chmod 0700 /etc/factory-broker/secrets

USER agent
