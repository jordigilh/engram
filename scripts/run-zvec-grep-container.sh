#!/usr/bin/env bash
# Run the dedicated zg image behind the existing loopback MCP endpoint.
#
# zg deliberately binds only to loopback. Podman port publishing cannot
# forward a socket bound to the container's 127.0.0.1, so a tiny socat sidecar
# shares the zg network namespace and exposes only an internal bridge port.
# The published host port remains 127.0.0.1:7999; no public interface is used.

set -euo pipefail

RUNTIME="${ZVEC_GREP_CONTAINER_RUNTIME:-${HOME}/go/bin/docker}"
ZG_IMAGE="${ZVEC_GREP_CONTAINER_IMAGE:-quay.io/jordigilh/zvec-grep:zg-v0.0.1-test.3}"
PROXY_IMAGE="${ZVEC_GREP_PROXY_IMAGE:-localhost/zg-loopback-proxy:local}"
ZG_NAME="${ZVEC_GREP_CONTAINER_NAME:-engram-zvec-grep}"
PROXY_NAME="${ZVEC_GREP_PROXY_NAME:-engram-zvec-grep-proxy}"
STATE_DIR="${ZVEC_GREP_STATE_DIR:-${HOME}/.engram/zvec-grep}"

if [[ ! -x "$RUNTIME" ]]; then
    echo "zvec-grep container runtime is not executable: $RUNTIME" >&2
    exit 1
fi
if [[ ! -d "$STATE_DIR" ]]; then
    mkdir -p "$STATE_DIR"
fi

cleanup() {
    "$RUNTIME" rm --force "$PROXY_NAME" "$ZG_NAME" >/dev/null 2>&1 || true
}

trap cleanup EXIT
trap 'exit 143' HUP INT TERM

# Remove only the two service-owned names. This also recovers containers left
# behind by a launchd restart after the wrapper was interrupted.
cleanup

"$RUNTIME" run \
    --pull=never \
    --detach \
    --name "$ZG_NAME" \
    --userns=keep-id \
    --ulimit nofile=65536:65536 \
    --publish 127.0.0.1:7999:7998 \
    --env HOME=/var/lib/zg \
    --env ZVEC_GREP_HOME=/var/lib/zg \
    --volume "$STATE_DIR:/var/lib/zg:rw" \
    --volume "$HOME:$HOME:rw" \
    "$ZG_IMAGE" \
    --server run \
    --listen 127.0.0.1:7999 \
    --mcp-toolset agent >/dev/null

"$RUNTIME" run \
    --pull=never \
    --detach \
    --name "$PROXY_NAME" \
    --network "container:$ZG_NAME" \
    "$PROXY_IMAGE" \
    TCP-LISTEN:7998,fork,reuseaddr,bind=0.0.0.0 \
    TCP:127.0.0.1:7999 >/dev/null

ready=false
for _ in {1..30}; do
    if curl --silent --show-error --fail --max-time 2 http://127.0.0.1:7999/healthz >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [[ "$ready" != true ]]; then
    echo "zvec-grep container did not become healthy on 127.0.0.1:7999" >&2
    "$RUNTIME" logs "$ZG_NAME" 2>&1 || true
    exit 1
fi

echo "zvec-grep container ready at http://127.0.0.1:7999/mcp" >&2

while
    "$RUNTIME" inspect --format '{{.State.Running}}' "$ZG_NAME" 2>/dev/null | grep --quiet '^true$' &&
        "$RUNTIME" inspect --format '{{.State.Running}}' "$PROXY_NAME" 2>/dev/null | grep --quiet '^true$'
do
    sleep 5
done

echo "zvec-grep container or loopback bridge exited" >&2
exit 1
