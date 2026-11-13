#!/usr/bin/env bash
# Run a command inside the stock runtime image with the production layout.
#
#   ops/container/run.sh <command> [args...]
#
# - The workspace is bind-mounted at the container path expected by the
#   existing code (110+ files hard-code /Users/fisher/...).  On Linux the
#   host-side source can be set with STOCK_RUNTIME_HOST_WORKSPACE; --mount,
#   not -v, keeps the image WORKDIR from shadowing it.
# - The AI and qlib venvs sit inside the workspace, so the bind mount would show
#   the host's macOS venvs there.  Each is overlaid with a named volume keyed by
#   the image id; Docker fills an empty volume from the image on first use, so
#   a rebuilt image never runs against a stale env.  Volumes of older images
#   are removed here.
# - ~/.hermes is mounted read-only for hermes-agent and the wrappers.
# - The Copilot token is supplied through COPILOT_GITHUB_TOKEN on Linux.  The
#   macOS gh-keychain fallback remains available for the original host mode,
#   but can be disabled with STOCK_RUNTIME_NO_KEYCHAIN=1.
set -euo pipefail

# Hermes cron runs with a minimal PATH.
DOCKER="${DOCKER:-$(command -v docker || echo /usr/local/bin/docker)}"
GH="$(command -v gh || echo /opt/homebrew/bin/gh)"
if [[ -z "${COPILOT_GITHUB_TOKEN:-}" && "${STOCK_RUNTIME_NO_KEYCHAIN:-0}" != "1" ]]; then
  export COPILOT_GITHUB_TOKEN="$($GH auth token 2>/dev/null || true)"
else
  export COPILOT_GITHUB_TOKEN="${COPILOT_GITHUB_TOKEN:-}"
fi

IMAGE="${STOCK_RUNTIME_IMAGE:-stock-runtime:latest}"
HOST_WS="${STOCK_RUNTIME_HOST_WORKSPACE:-/Users/fisher/.openclaw/workspace}"
HOST_HERMES="${STOCK_RUNTIME_HOST_HERMES:-/Users/fisher/.hermes}"
WS="${STOCK_RUNTIME_CONTAINER_WORKSPACE:-/Users/fisher/.openclaw/workspace}"
HERMES="${STOCK_RUNTIME_CONTAINER_HERMES:-/Users/fisher/.hermes}"

id="$("$DOCKER" image inspect -f '{{.Id}}' "$IMAGE")"
tag="${id#sha256:}"; tag="${tag:0:12}"
for vol in $("$DOCKER" volume ls -q --filter name='^stock-env-'); do
  [[ "$vol" == *"-$tag" ]] || "$DOCKER" volume rm "$vol" >/dev/null 2>&1 || true  # in use -> kept
done

exec "$DOCKER" run --rm ${STOCK_RUNTIME_DOCKER_ARGS:-} \
  --mount "type=bind,src=$HOST_WS,dst=$WS" \
  --mount "type=volume,src=stock-env-ai-$tag,dst=$WS/daily_stock_analysis/venv" \
  --mount "type=volume,src=stock-env-qlib-$tag,dst=$WS/factor_factory/qlib_lab/venv" \
  --mount "type=bind,src=$HOST_HERMES,dst=$HERMES,readonly" \
  -e HERMES_HOME="$HERMES" -e COPILOT_GITHUB_TOKEN \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  "$IMAGE" "$@"
