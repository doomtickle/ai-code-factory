#!/usr/bin/env bash
set -euo pipefail

# Builds the sandbox templates locally and loads them into the sbx runtime.
# No registry push: each image goes docker build -> tar -> sbx template load.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

require_bin docker
require_sbx

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# broker/ is a git submodule (passdesk-broker); an uninitialized checkout has
# an empty directory and no Dockerfile here.
[ -f "$ROOT/broker/Dockerfile" ] \
  || die "missing $ROOT/broker/Dockerfile - run 'git submodule update --init' in $ROOT"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

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
# The broker builds from its own submodule checkout, with that directory as
# the build context.
# This machine's policy overlay (an empty one if there is none) goes in as its
# own build context, so it never has to sit in the repo.
mkdir "$tmp/config"
if [ -f "$FACTORY_CONFIG/policy.toml" ]; then
  echo "Including local broker policy from $FACTORY_CONFIG/policy.toml"
  cp "$FACTORY_CONFIG/policy.toml" "$tmp/config/policy.toml"
else
  : > "$tmp/config/policy.toml"
fi
build broker "$ROOT/broker/Dockerfile" "$ROOT/broker" --build-context "factory-config=$tmp/config"

sbx template ls
