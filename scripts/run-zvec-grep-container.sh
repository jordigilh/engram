#!/usr/bin/env bash
# Run the dedicated zg image behind the existing loopback MCP endpoint.
#
# zg deliberately binds only to loopback. Podman port publishing cannot
# forward a socket bound to the container's 127.0.0.1, so a tiny socat sidecar
# shares the zg network namespace and exposes only an internal bridge port.
# The published host port remains 127.0.0.1:7999; no public interface is used.

set -euo pipefail

RUNTIME="${ZVEC_GREP_CONTAINER_RUNTIME:-${HOME}/go/bin/docker}"
ZG_IMAGE="${ZVEC_GREP_CONTAINER_IMAGE:-quay.io/jordigilh/zvec-grep:latest}"
PROXY_IMAGE="${ZVEC_GREP_PROXY_IMAGE:-quay.io/jordigilh/zg-loopback-proxy:latest}"
ZG_NAME="${ZVEC_GREP_CONTAINER_NAME:-engram-zvec-grep}"
PROXY_NAME="${ZVEC_GREP_PROXY_NAME:-engram-zvec-grep-proxy}"
STATE_DIR="${ZVEC_GREP_STATE_DIR:-${HOME}/.engram/zvec-grep}"
WORKSPACE_ROOTS="${ZVEC_GREP_WORKSPACE_ROOTS:-}"
OPENCODE_TEMP_ROOT="${ZVEC_GREP_OPENCODE_TEMP_ROOT:-}"

if [[ ! -x "$RUNTIME" ]]; then
    echo "zvec-grep container runtime is not executable: $RUNTIME" >&2
    exit 1
fi
if [[ ! -d "$STATE_DIR" ]]; then
    mkdir -p "$STATE_DIR"
fi

if [[ -z "$WORKSPACE_ROOTS" ]]; then
    echo "ZVEC_GREP_WORKSPACE_ROOTS must explicitly authorize workspace mount roots" >&2
    exit 1
fi

WORKSPACE_MOUNT_ARGS=()
SEEN_WORKSPACE_ROOTS=()

add_workspace_mount() {
    local configured_root="$1"
    local description="$2"
    if [[ -z "$configured_root" || ! -d "$configured_root" ]]; then
        echo "${description} is not a directory: $configured_root" >&2
        exit 1
    fi

    local workspace_root
    workspace_root="$(cd "$configured_root" && pwd -P)"
    local duplicate=false
    for seen_root in "${SEEN_WORKSPACE_ROOTS[@]}"; do
        if [[ "$seen_root" == "$workspace_root" ]]; then
            duplicate=true
            break
        fi
    done
    if [[ "$duplicate" == true ]]; then
        return 0
    fi
    SEEN_WORKSPACE_ROOTS+=("$workspace_root")
    WORKSPACE_MOUNT_ARGS+=(--volume "$workspace_root:$workspace_root:rw")
}

IFS=':' read -r -a CONFIGURED_WORKSPACE_ROOTS <<< "$WORKSPACE_ROOTS"
for configured_root in "${CONFIGURED_WORKSPACE_ROOTS[@]}"; do
    add_workspace_mount "$configured_root" "configured zvec workspace mount"
done

# OpenCode creates temporary worktrees below macOS's per-user temp directory,
# which is not covered by the normal checkout roots. launchd may not export
# TMPDIR, so use Apple's per-user query rather than a shared temp fallback.
# Mount only the exact application subtree; never broaden this to the temp
# parent or HOME. Other hosts must use the explicit override below.
if [[ -z "$OPENCODE_TEMP_ROOT" &&
    "$(uname -s 2>/dev/null || true)" == "Darwin" ]]; then
    darwin_user_temp_dir="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null || true)"
    if [[ -n "$darwin_user_temp_dir" && "$darwin_user_temp_dir" != "/" &&
        -d "$darwin_user_temp_dir" ]]; then
        OPENCODE_TEMP_ROOT="${darwin_user_temp_dir%/}/opencode"
    fi
fi

if [[ -n "$OPENCODE_TEMP_ROOT" ]]; then
    if [[ "$OPENCODE_TEMP_ROOT" != /* ]]; then
        echo "ZVEC_GREP_OPENCODE_TEMP_ROOT must be an absolute path: $OPENCODE_TEMP_ROOT" >&2
        exit 1
    fi
    if [[ ! -d "$OPENCODE_TEMP_ROOT" ]]; then
        if [[ -n "${ZVEC_GREP_OPENCODE_TEMP_ROOT:-}" ]]; then
            echo "configured OpenCode temp mount is not a directory: $OPENCODE_TEMP_ROOT" >&2
            exit 1
        fi
        # The LaunchAgent can start before OpenCode creates its first
        # worktree. Creating only this known child keeps the future mount
        # visible without authorizing the broader temp directory.
        mkdir -p "$OPENCODE_TEMP_ROOT"
    fi
    canonical_opencode_temp_root="$(cd "$OPENCODE_TEMP_ROOT" && pwd -P)"
    if [[ "$(basename "$canonical_opencode_temp_root")" != "opencode" ]]; then
        echo "OpenCode temp mount must be the exact opencode directory: $OPENCODE_TEMP_ROOT" >&2
        exit 1
    fi
    add_workspace_mount "$canonical_opencode_temp_root" "OpenCode temp workspace mount"
fi

cleanup() {
    "$RUNTIME" rm --force "$PROXY_NAME" "$ZG_NAME" >/dev/null 2>&1 || true
}

wait_for_health() {
    for _ in {1..30}; do
        if curl --silent --show-error --fail --max-time 2 http://127.0.0.1:7999/healthz >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

start_containers() {
    local zvec_image="$1"
    local proxy_image="$2"
    cleanup

    # The OpenCode worktree indexer uses the root-scoped management tools over
    # MCP. Keep the endpoint loopback-only; the full toolset is not exposed
    # through the public Engram route.
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
        "${WORKSPACE_MOUNT_ARGS[@]}" \
        "$zvec_image" \
        --server run \
        --listen 127.0.0.1:7999 \
        --mcp-toolset full >/dev/null || return 1

    "$RUNTIME" run \
        --pull=never \
        --detach \
        --name "$PROXY_NAME" \
        --network "container:$ZG_NAME" \
        "$proxy_image" \
        TCP-LISTEN:7998,fork,reuseaddr,bind=0.0.0.0 \
        TCP:127.0.0.1:7999 >/dev/null || {
        cleanup
        return 1
    }

    if ! wait_for_health; then
        echo "zvec-grep container did not become healthy on 127.0.0.1:7999" >&2
        "$RUNTIME" logs "$ZG_NAME" 2>&1 || true
        cleanup
        return 1
    fi
    return 0
}

monitor_containers() {
    while
        "$RUNTIME" inspect --format '{{.State.Running}}' "$ZG_NAME" 2>/dev/null | grep --quiet '^true$' &&
            "$RUNTIME" inspect --format '{{.State.Running}}' "$PROXY_NAME" 2>/dev/null | grep --quiet '^true$'
    do
        sleep 5
    done
    echo "zvec-grep container or loopback bridge exited" >&2
    return 1
}

trap cleanup EXIT
trap 'exit 143' HUP INT TERM

# Pull explicitly so the deployed image is refreshed before startup. Keep the
# subsequent runs offline and deterministic; Podman 5.8 can otherwise resolve
# a multi-architecture registry reference differently between pull and run.
"$RUNTIME" pull --policy=always "$ZG_IMAGE"
"$RUNTIME" pull --policy=always "$PROXY_IMAGE"

previous_zvec_image="$("$RUNTIME" inspect --format '{{.Image}}' "$ZG_NAME" 2>/dev/null || true)"
previous_proxy_image="$("$RUNTIME" inspect --format '{{.Image}}' "$PROXY_NAME" 2>/dev/null || true)"

if ! start_containers "$ZG_IMAGE" "$PROXY_IMAGE"; then
    if [[ -n "$previous_zvec_image" && -n "$previous_proxy_image" ]] &&
        start_containers "$previous_zvec_image" "$previous_proxy_image"; then
        echo "new zvec images failed health checks; restored the previous running image" >&2
        monitor_containers
        exit $?
    fi
    echo "new zvec images failed and the previous image could not be restored" >&2
    exit 1
fi

echo "zvec-grep container ready at http://127.0.0.1:7999/mcp" >&2
monitor_containers
