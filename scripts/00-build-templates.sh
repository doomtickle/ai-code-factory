#!/usr/bin/env bash
set -euo pipefail

# Usage: 00-build-templates.sh [--build-only]
#
# Builds the sandbox templates locally and loads them into the sbx runtime.
# No registry push: each image goes docker build -> tar -> sbx template load.
# --build-only builds the same images (docker only) without sbx: no load, and
# nothing recorded in the state dir. CI uses it.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

build_only=""
case "${1:-}" in
  "") ;;
  --build-only) build_only=1 ;;
  *) die "usage: $0 [--build-only]" ;;
esac

require_bin docker
[ -n "$build_only" ] || require_sbx

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# The broker image is built from a "git archive" export of the committed
# broker/ (HEAD), never from the broker/ working tree, which an agent working
# on this repo can write into (see "the broker image's build context" in
# lib/common.sh). Uncommitted changes in broker/ are not in the image.
mkdir "$tmp/broker"
prepare_broker_context "$ROOT" "$tmp/broker"

# build ROLE DOCKERFILE CONTEXT [DOCKER-BUILD-ARGS...]
build() {
  local tag="factory-$1:$TEMPLATE_TAG" image="$1" dockerfile="$2" context="$3"
  shift 3
  echo "Building $tag..."
  docker build -t "$tag" -f "$dockerfile" "$@" "$context"
  [ -z "$build_only" ] || return 0
  echo "Loading $tag into sbx..."
  docker image save "$tag" -o "$tmp/$image.tar"
  sbx template load "$tmp/$image.tar"
  # The image id tells sandbox fingerprints (lib/roles.sh) whether a rebuild
  # changed anything: an unchanged, cached build keeps its id.
  mkdir -p "$FACTORY_STATE/images"
  docker image inspect --format '{{.Id}}' "$tag" > "$FACTORY_STATE/images/factory-$image-$TEMPLATE_TAG.id"
}

# developer/reviewer use the repo root as the context for shared files;
# .dockerignore limits it to what they COPY. They install the ShellCheck pinned
# in lib/common.sh (the Dockerfiles have no defaults for these args).
shellcheck_args=(
  --build-arg "SHELLCHECK_VERSION=$SHELLCHECK_VERSION"
  --build-arg "SHELLCHECK_LINUX_X86_64_SHA256=$SHELLCHECK_LINUX_X86_64_SHA256"
  --build-arg "SHELLCHECK_LINUX_AARCH64_SHA256=$SHELLCHECK_LINUX_AARCH64_SHA256"
)
build developer "$ROOT/templates/developer/Dockerfile" "$ROOT" "${shellcheck_args[@]}"
build reviewer "$ROOT/templates/reviewer/Dockerfile" "$ROOT" "${shellcheck_args[@]}"
# The broker builds from the export of the committed broker/ made above, with
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
# Optional plugin packages (docs/plugin-tools.md in broker/) from the local config folder,
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

[ -n "$build_only" ] || sbx template ls
