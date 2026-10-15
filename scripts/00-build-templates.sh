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

# build ROLE CONTEXT [DOCKER-BUILD-ARGS...]
build() {
  local tag="factory-$1:$TEMPLATE_TAG" image="$1" context="$2"
  shift 2
  echo "Building $tag..."
  docker build -t "$tag" -f "$ROOT/templates/$image/Dockerfile" "$@" "$context"
  echo "Loading $tag into sbx..."
  docker image save "$tag" -o "$tmp/$image.tar"
  sbx template load "$tmp/$image.tar"
}

# All three use the repo root as the context for shared files; .dockerignore
# limits it to what they COPY.
build developer "$ROOT"
build reviewer "$ROOT"
# This machine's policy overlay (an empty one if there is none) goes in as its
# own build context, so it never has to sit in the repo.
mkdir "$tmp/config"
if [ -f "$FACTORY_CONFIG/policy.toml" ]; then
  echo "Including local broker policy from $FACTORY_CONFIG/policy.toml"
  cp "$FACTORY_CONFIG/policy.toml" "$tmp/config/policy.toml"
else
  : > "$tmp/config/policy.toml"
fi
build broker "$ROOT" --build-context "factory-config=$tmp/config"

sbx template ls
