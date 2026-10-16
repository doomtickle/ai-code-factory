#!/usr/bin/env bash
set -euo pipefail

# Builds the sandbox templates locally and loads them into the sbx runtime.
# No registry push: each image goes docker build -> tar -> sbx template load.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

require_bin docker
require_sbx

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# broker/ is a git submodule (passdesk-broker); a plain "git clone" (without
# --recurse-submodules) leaves it uninitialized, so try once to initialize it
# before giving up (bin/factory's roles.sh does the same, but this script can
# also run standalone). Only a submodule with no trace of initialization at
# all: anything else goes through the checks below.
if ! broker_initialized "$ROOT"; then
  git "${BROKER_GIT_SAFE[@]}" -C "$ROOT" submodule update --init --quiet -- broker >/dev/null 2>&1 || true
fi
# The broker image is built from a "git archive" export of the pinned commit,
# never from the broker/ working tree, which an agent working on this repo
# can write into (see "broker/ submodule safety" in lib/common.sh). Building
# is also refused while broker/ has uncommitted work or is off the pin.
mkdir "$tmp/broker"
prepare_broker_context "$ROOT" "$tmp/broker"

# build ROLE DOCKERFILE CONTEXT [DOCKER-BUILD-ARGS...]
build() {
  local tag="factory-$1:$TEMPLATE_TAG" image="$1" dockerfile="$2" context="$3"
  shift 3
  echo "Building $tag..."
  docker build -t "$tag" -f "$dockerfile" "$@" "$context"
  echo "Loading $tag into sbx..."
  docker image save "$tag" -o "$tmp/$image.tar"
  sbx template load "$tmp/$image.tar"
  # The image id tells sandbox fingerprints (lib/roles.sh) whether a rebuild
  # changed anything: an unchanged, cached build keeps its id.
  mkdir -p "$FACTORY_STATE/images"
  docker image inspect --format '{{.Id}}' "$tag" > "$FACTORY_STATE/images/factory-$image-$TEMPLATE_TAG.id"
}

# developer/reviewer use the repo root as the context for shared files;
# .dockerignore limits it to what they COPY.
build developer "$ROOT/templates/developer/Dockerfile" "$ROOT"
build reviewer "$ROOT/templates/reviewer/Dockerfile" "$ROOT"
# The broker builds from the export of its pinned commit made above, with
# that directory as the build context.
# This machine's policy overlay (an empty one if there is none) goes in as its
# own build context, so it never has to sit in the repo.
mkdir "$tmp/config"
if [ -f "$FACTORY_CONFIG/policy.toml" ]; then
  echo "Including local broker policy from $FACTORY_CONFIG/policy.toml"
  cp "$FACTORY_CONFIG/policy.toml" "$tmp/config/policy.toml"
else
  : > "$tmp/config/policy.toml"
fi
# Optional plugin packages (e.g. Passdesk Pro) from the local config folder,
# as their own build context too: an empty directory when there are none.
# A plugin loads only when start-broker.sh names it in PASSDESK_PLUGINS.
mkdir "$tmp/plugins"
if [ -d "$FACTORY_CONFIG/plugins" ] && [ -n "$(ls -A "$FACTORY_CONFIG/plugins" 2>/dev/null)" ]; then
  echo "Including broker plugins from $FACTORY_CONFIG/plugins: $(ls "$FACTORY_CONFIG/plugins" | tr '\n' ' ')"
  cp -R "$FACTORY_CONFIG/plugins/." "$tmp/plugins/"
  find "$tmp/plugins" -name '__pycache__' -type d -prune -exec rm -rf {} +
fi
build broker "$tmp/broker/Dockerfile" "$tmp/broker" --build-context "factory-config=$tmp/config" \
  --build-context "plugins=$tmp/plugins"

sbx template ls
