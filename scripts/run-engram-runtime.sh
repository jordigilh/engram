#!/usr/bin/env bash
# Keep the image-backed gateway running across Podman machine restarts.
# launchd supervises this script; the script supervises the foreground
# container and waits for Podman to become usable again after a VM restart.

set -u

PODMAN="${PODMAN_BIN:-}"
IMAGE="${ENGRAM_RUNTIME_IMAGE:-quay.io/jordigilh/engram:runtime-latest}"
CONFIG="${ENGRAM_RUNTIME_CONFIG:-${HOME}/.engram/runtime/instances.toml}"
CONTAINER="${ENGRAM_RUNTIME_CONTAINER:-engram-runtime}"
PORT="${ENGRAM_RUNTIME_PORT:-8896}"
RETRY_SECONDS="${ENGRAM_RUNTIME_RETRY_SECONDS:-10}"

mkdir -p "${HOME}/.engram/logs"

if [ -z "$PODMAN" ]; then
  for candidate in /opt/podman/bin/podman /opt/homebrew/bin/podman /usr/local/bin/podman; do
    if [ -x "$candidate" ]; then
      PODMAN="$candidate"
      break
    fi
  done
fi

if [ ! -x "$PODMAN" ]; then
  printf 'engram-runtime: podman not found at %s\n' "$PODMAN" >&2
  exit 1
fi

if [ ! -r "$CONFIG" ]; then
  printf 'engram-runtime: config not readable at %s\n' "$CONFIG" >&2
  exit 1
fi

child=""
stopping=0

stop_child() {
  stopping=1
  if [ -n "$child" ]; then
    kill "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
  fi
}

trap stop_child INT TERM

while [ "$stopping" -eq 0 ]; do
  # `podman machine` is a separate VM on macOS. `podman info` is the
  # readiness check that matters to the run/pull commands below.
  if ! "$PODMAN" info >/dev/null 2>&1; then
    "$PODMAN" machine start podman-machine-default >/dev/null 2>&1 || true
    sleep "$RETRY_SECONDS"
    continue
  fi

  # Refresh the mutable channel when the service starts, but keep using the
  # cached image if Quay is temporarily unavailable during a Podman restart.
  "$PODMAN" pull "$IMAGE" >/dev/null 2>&1 || true

  "$PODMAN" run \
    --rm \
    --replace \
    --name "$CONTAINER" \
    --pull=never \
    --add-host host.containers.internal:host-gateway \
    --mount "type=bind,src=${CONFIG},dst=/etc/engram/instances.toml,ro" \
    --publish "127.0.0.1:${PORT}:8896" \
    "$IMAGE" &
  child=$!
  wait "$child"
  status=$?
  child=""

  if [ "$stopping" -eq 1 ]; then
    break
  fi

  printf 'engram-runtime: container exited with status %s; retrying in %ss\n' "$status" "$RETRY_SECONDS" >&2
  sleep "$RETRY_SECONDS"
done
