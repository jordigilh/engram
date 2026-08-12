#!/usr/bin/env bash
# Safety net for the failure class found in docs/FINDINGS.md 2026-08-12:
# any live-mode cocoindex launchd job can end up silently unloaded from the
# gui/$UID domain (not crash-looping -- fully absent from `launchctl list`)
# with nothing auto-restarting it. That specific incident's root cause (a
# "one-time" cleanup script that never reached its own restart step) has
# been fixed at the source, but this watchdog is a general-purpose safety
# net for *any* future cause of the same silent-outage class -- a stray
# `launchctl bootout`, a future maintenance script with the same bug shape,
# manual `launchctl unload` left uncommitted, etc.
#
# Runs every 15 min via io.vectorize.cocoindex.watchdog.plist (StartInterval).
# Only touches jobs that are *expected* to run continuously (KeepAlive=true,
# "live" mode) -- NOT one-shot/on-demand jobs like dcm's OnDemand backfill.
set -uo pipefail

HINDSIGHT_DIR="${HOME}/.hindsight"
LOG_FILE="${HINDSIGHT_DIR}/logs/cocoindex-watchdog.log"
UID_NUM="$(id -u)"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $*" >>"$LOG_FILE"
}

# label -> plist filename, for every job that should be running continuously.
declare -a SERVICES=(
    "io.vectorize.cocoindex.service"
    "io.vectorize.cocoindex.engram"
    "io.vectorize.cocoindex.koku"
    "io.vectorize.cocoindex.praxis"
)

any_intervention=0

for label in "${SERVICES[@]}"; do
    plist="${HOME}/Library/LaunchAgents/${label}.plist"

    if [ ! -f "$plist" ]; then
        log "WARN: ${label} has no deployed plist at ${plist} -- skipping (not an outage, a config gap)"
        continue
    fi

    if launchctl list "$label" >/dev/null 2>&1; then
        continue
    fi

    any_intervention=1
    log "OUTAGE DETECTED: ${label} is not loaded in gui/${UID_NUM} -- re-bootstrapping"
    if launchctl bootstrap "gui/${UID_NUM}" "$plist" 2>>"$LOG_FILE"; then
        sleep 2
        if launchctl list "$label" >/dev/null 2>&1; then
            log "RECOVERED: ${label} back up"
        else
            log "ERROR: ${label} bootstrap reported success but job still not listed -- needs manual investigation"
        fi
    else
        log "ERROR: ${label} bootstrap failed -- needs manual investigation"
    fi
done

if [ "$any_intervention" -eq 0 ]; then
    # Quiet heartbeat, not one line per service -- keeps the log skimmable
    # for the once-in-a-while human read while still proving the watchdog
    # itself is alive and running on schedule.
    log "heartbeat: all ${#SERVICES[@]} live services present, no action needed"
fi
