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
TRUSTED_DIGEST_FILE="${ZVEC_GREP_TRUSTED_DIGEST_FILE:-${STATE_DIR}/trusted-digests.env}"
REQUIRE_DIGEST="${ZVEC_GREP_REQUIRE_DIGEST:-true}"
COSIGN_KEY="${ZVEC_GREP_COSIGN_KEY:-}"

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
IFS=':' read -r -a CONFIGURED_WORKSPACE_ROOTS <<< "$WORKSPACE_ROOTS"
for configured_root in "${CONFIGURED_WORKSPACE_ROOTS[@]}"; do
    if [[ -z "$configured_root" || ! -d "$configured_root" ]]; then
        echo "configured zvec workspace mount is not a directory: $configured_root" >&2
        exit 1
    fi
    workspace_root="$(cd "$configured_root" && pwd -P)"
    duplicate=false
    for seen_root in "${SEEN_WORKSPACE_ROOTS[@]}"; do
        if [[ "$seen_root" == "$workspace_root" ]]; then
            duplicate=true
            break
        fi
    done
    if [[ "$duplicate" == true ]]; then
        continue
    fi
    SEEN_WORKSPACE_ROOTS+=("$workspace_root")
    WORKSPACE_MOUNT_ARGS+=(--volume "$workspace_root:$workspace_root:rw")
done

cleanup() {
    "$RUNTIME" rm --force "$PROXY_NAME" "$ZG_NAME" >/dev/null 2>&1 || true
}

read_trusted_digest() {
    local key="$1"
    [[ -r "$TRUSTED_DIGEST_FILE" ]] || return 0
    local value
    value="$(awk -F= -v wanted="$key" '$1 == wanted { gsub(/[[:space:]]/, "", $2); print $2; exit }' \
        "$TRUSTED_DIGEST_FILE")"
    if [[ -n "$value" ]]; then
        printf '%s\n' "$value"
        return 0
    fi
    local platform_arch
    case "$(uname -m)" in
        arm64|aarch64) platform_arch=ARM64 ;;
        x86_64|amd64) platform_arch=AMD64 ;;
        *) return 0 ;;
    esac
    awk -F= -v wanted="${key}_LINUX_${platform_arch}" \
        '$1 == wanted { gsub(/[[:space:]]/, "", $2); print $2; exit }' "$TRUSTED_DIGEST_FILE"
}

image_digest() {
    local image="$1"
    local digest
    digest="$("$RUNTIME" image inspect --format '{{.Digest}}' "$image" 2>/dev/null || true)"
    if [[ "$digest" =~ ^sha256:[0-9a-fA-F]{64}$ ]]; then
        printf '%s\n' "$digest"
        return 0
    fi
    "$RUNTIME" image inspect --format '{{json .RepoDigests}}' "$image" 2>/dev/null \
        | grep -Eo 'sha256:[0-9a-fA-F]{64}' | head -n 1 || true
}

verify_image() {
    local image="$1"
    local expected_key="$2"
    local expected="${!expected_key:-}"
    if [[ -z "$expected" ]]; then
        expected="$(read_trusted_digest "$expected_key")"
    fi
    if [[ "$REQUIRE_DIGEST" != true ]]; then
        return 0
    fi
    if [[ ! "$expected" =~ ^sha256:[0-9a-fA-F]{64}$ ]]; then
        echo "missing trusted digest for $image; set $expected_key or $TRUSTED_DIGEST_FILE" >&2
        return 1
    fi
    local actual
    actual="$(image_digest "$image")"
    if [[ "$actual" != "$expected" ]]; then
        echo "digest mismatch for $image: expected $expected, got ${actual:-<unknown>}" >&2
        return 1
    fi
    if [[ -n "$COSIGN_KEY" ]]; then
        if ! command -v cosign >/dev/null 2>&1; then
            echo "cosign is required by ZVEC_GREP_COSIGN_KEY but is not installed" >&2
            return 1
        fi
        cosign verify --key "$COSIGN_KEY" "$image@$actual" >/dev/null
    fi
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
        --mcp-toolset agent >/dev/null || return 1

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

# Pull explicitly so the deployed digest is refreshed before startup. Keep the
# subsequent runs offline and deterministic; Podman 5.8 can otherwise resolve
# a multi-architecture registry reference differently between pull and run.
"$RUNTIME" pull --policy=always "$ZG_IMAGE"
"$RUNTIME" pull --policy=always "$PROXY_IMAGE"
verify_image "$ZG_IMAGE" ZVEC_GREP_IMAGE_DIGEST
verify_image "$PROXY_IMAGE" ZVEC_GREP_PROXY_IMAGE_DIGEST

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
