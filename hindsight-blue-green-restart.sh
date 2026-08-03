#!/usr/bin/env bash
# Blue/green restart for hindsight-api.
#
# Why this exists (see docs/FINDINGS.md 2026-06-26, 2026-07-07, 2026-08-02):
# hindsight-api needs a periodic full-process restart to reclaim Python heap
# fragmentation. A plain `pkill -f hindsight-api` (the old approach) leaves
# port 8888 unreachable for the ~15-20s it takes the respawned process to
# come back up. Cursor holds a persistent HTTP connection to that port for
# the hindsight/hindsight-docs/hindsight-issues MCP servers and does not
# auto-retry after a drop, so every restart silently disabled those tools
# until a manual reload.
#
# The fix: hindsight-proxy.py is the only thing that ever binds 8888, and it
# never restarts. hindsight-api binds an internal "blue" (18888) or "green"
# (18889) port instead. This script starts a fresh instance on the *standby*
# color, waits for it to pass a real health check, flips the proxy's active-
# backend state file to point at it, gives in-flight requests on the old
# instance a moment to drain, then tears the old instance down. Port 8888
# never refuses a connection at any point in this sequence.
#
# Invoked nightly by launchd/io.vectorize.hindsight.restart.plist in place of
# the old pkill. Safe to also run manually to force an out-of-schedule swap.
set -euo pipefail

HINDSIGHT_DIR="${HOME}/.hindsight"
STATE_DIR="${HINDSIGHT_DIR}/state"
STATE_FILE="${STATE_DIR}/active-backend.port"
LOG_FILE="${HINDSIGHT_DIR}/logs/blue-green-restart.log"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"

BLUE_PORT=18888
GREEN_PORT=18889
HEALTH_TIMEOUT_S=180
HEALTH_POLL_INTERVAL_S=2
HEALTH_CHECK_TIMEOUT_S=2
REQUIRED_CONSECUTIVE_HEALTHY=5
DRAIN_GRACE_S=10

mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $*" >>"$LOG_FILE"
}

active_port="$(cat "$STATE_FILE" 2>/dev/null || true)"
if [ "$active_port" = "$GREEN_PORT" ]; then
    active_color="green"; active_port=$GREEN_PORT
    standby_color="blue"; standby_port=$BLUE_PORT
else
    active_color="blue"; active_port=$BLUE_PORT
    standby_color="green"; standby_port=$GREEN_PORT
fi

standby_label="io.vectorize.hindsight.service-${standby_color}"
active_label="io.vectorize.hindsight.service-${active_color}"
standby_plist="${LAUNCH_AGENTS}/${standby_label}.plist"

log "swap start: active=${active_color}(:${active_port}) standby=${standby_color}(:${standby_port})"

if [ ! -f "$standby_plist" ]; then
    log "ERROR: ${standby_plist} not found -- aborting, leaving ${active_color} running untouched"
    exit 1
fi

# Defensive: clear out any lingering standby instance from a previous failed
# swap before starting a fresh one.
launchctl bootout "gui/${UID_NUM}/${standby_label}" >/dev/null 2>&1 || true

launchctl bootstrap "gui/${UID_NUM}" "$standby_plist"
log "bootstrapped ${standby_label}, polling http://localhost:${standby_port}/health"

# A single 200 from /health is not sufficient: hindsight-api can pass /health
# immediately on startup, then its background worker claims a startup burst
# of consolidation/graph_maintenance tasks that saturates the small DB
# connection pool (HINDSIGHT_API_DB_POOL_MAX_SIZE) for up to ~60-70s,
# stalling ordinary requests even though /health nominally passed. Require
# several consecutive *fast* successful checks before declaring the standby
# truly ready -- see docs/FINDINGS.md 2026-08-02.
healthy=0
consecutive=0
elapsed=0
while [ "$elapsed" -lt "$HEALTH_TIMEOUT_S" ]; do
    if curl -s -f -o /dev/null --max-time "$HEALTH_CHECK_TIMEOUT_S" "http://localhost:${standby_port}/health"; then
        consecutive=$((consecutive + 1))
        if [ "$consecutive" -ge "$REQUIRED_CONSECUTIVE_HEALTHY" ]; then
            healthy=1
            break
        fi
    else
        consecutive=0
    fi
    sleep "$HEALTH_POLL_INTERVAL_S"
    elapsed=$((elapsed + HEALTH_POLL_INTERVAL_S))
done

if [ "$healthy" -ne 1 ]; then
    log "ERROR: ${standby_color} never became consistently healthy after ${HEALTH_TIMEOUT_S}s -- rolling back, ${active_color} stays active"
    launchctl bootout "gui/${UID_NUM}/${standby_label}" >/dev/null 2>&1 || true
    exit 1
fi

log "${standby_color} passed ${REQUIRED_CONSECUTIVE_HEALTHY} consecutive fast health checks after ${elapsed}s -- flipping proxy to :${standby_port}"

tmp_state="${STATE_FILE}.tmp.$$"
echo "$standby_port" >"$tmp_state"
mv -f "$tmp_state" "$STATE_FILE"

log "draining ${active_color} for ${DRAIN_GRACE_S}s before teardown"
sleep "$DRAIN_GRACE_S"

launchctl bootout "gui/${UID_NUM}/${active_label}" >/dev/null 2>&1 || true
log "swap complete: active is now ${standby_color}(:${standby_port}), ${active_color} torn down"
