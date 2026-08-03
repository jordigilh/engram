#!/usr/bin/env bash
# One-time cleanup: reset kubernaut-docs/kubernaut-issues bank contents and
# force a clean backfill from the branch-scoped main-only mirrors.
#
# Why (see docs/FINDINGS.md 2026-08-03): months of feature-branch churn --
# fixed by watch-mirrors-config.sh/watch-mirrors-lib.sh -- left kubernaut-docs
# at 142,178 memory units for only 40,406 distinct document_ids (~3.5x
# redundancy) and kubernaut-issues at 60,762 units for 15,226 distinct
# document_ids (~4x). Rather than wait for Sonnet-based consolidation to
# slowly and expensively grind through and merge that duplication, this
# clears both banks and does a fresh backfill against the now-correct
# main-only mirrors.
#
# CocoIndex's own memoization (cocoindex.db) tracks "have I already processed
# this content" independently of Hindsight's state, so clearing the banks
# alone would NOT cause docs/issues to be resubmitted -- cocoindex.db is
# backed up and reset here too. That's safe for code_app (idempotent
# pgvector upserts, keyed on filepath:chunk_index) and transcript_app
# (protected by its own separate watermark file, unaffected by this reset).
#
# This empties kubernaut-docs/kubernaut-issues for the duration of the
# backfill (potentially hours for ~55K documents) -- both are on kubernaut's
# mandatory-recall list, so this is scheduled off-hours via
# launchd/io.vectorize.cocoindex.cleanup-once.plist (one-shot, 3:00 AM,
# self-removing). Safe to also run manually if you accept that window.
set -euo pipefail

HINDSIGHT_URL="${HINDSIGHT_URL:-http://localhost:8888}"
HINDSIGHT_DIR="${HOME}/.hindsight"
LOG_FILE="${HINDSIGHT_DIR}/logs/kubernaut-docs-issues-cleanup.log"
UID_NUM="$(id -u)"
COCOINDEX_DB="${HINDSIGHT_DIR}/cocoindex.db"
COCOINDEX_DB_BACKUP="${HINDSIGHT_DIR}/cocoindex.db.pre-cleanup-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $*" >>"$LOG_FILE"
}

log "=== kubernaut-docs/kubernaut-issues cleanup start ==="

if ! curl -s -f -o /dev/null --max-time 5 "${HINDSIGHT_URL}/health"; then
    log "ERROR: hindsight-api not healthy at ${HINDSIGHT_URL} -- aborting, nothing touched"
    exit 1
fi

log "stopping io.vectorize.cocoindex.service"
launchctl bootout "gui/${UID_NUM}/io.vectorize.cocoindex.service" >/dev/null 2>&1 || true
sleep 2

for bank in kubernaut-docs kubernaut-issues; do
    log "clearing bank ${bank}"
    resp="$(curl -s -w '\n%{http_code}' -X DELETE "${HINDSIGHT_URL}/v1/default/banks/${bank}/memories")"
    code="$(echo "$resp" | tail -1)"
    body="$(echo "$resp" | sed '$d')"
    log "DELETE ${bank}/memories -> HTTP ${code}: ${body}"
    if [ "$code" != "200" ]; then
        log "ERROR: clearing ${bank} failed (HTTP ${code}) -- aborting before touching cocoindex.db or restarting the service"
        exit 1
    fi
done

if [ -e "$COCOINDEX_DB" ]; then
    log "backing up cocoindex.db to ${COCOINDEX_DB_BACKUP}"
    mv "$COCOINDEX_DB" "$COCOINDEX_DB_BACKUP"
fi

log "running fresh docs+issues backfill (can take hours for ~55K documents) -- see this file for progress"
if "${HINDSIGHT_DIR}/with-config-env.sh" "${HINDSIGHT_DIR}/venv/bin/python3" "${HINDSIGHT_DIR}/cocoindex-flows.py" \
    --mode backfill --apps docs issues >>"$LOG_FILE" 2>&1; then
    log "docs+issues backfill complete"
else
    log "ERROR: docs+issues backfill failed (see traceback above) -- kubernaut-docs/kubernaut-issues may be partially populated, io.vectorize.cocoindex.service NOT restarted yet, restart it manually once resolved"
    exit 1
fi

log "restarting io.vectorize.cocoindex.service (live mode -- will also refresh code+transcripts once, since cocoindex.db was reset; both are idempotent/watermark-protected, see comment above)"
launchctl bootstrap "gui/${UID_NUM}" "${HOME}/Library/LaunchAgents/io.vectorize.cocoindex.service.plist"

log "=== cleanup complete ==="

# Self-remove: meant to run exactly once (see
# io.vectorize.cocoindex.cleanup-once.plist), not fire every night.
launchctl bootout "gui/${UID_NUM}/io.vectorize.cocoindex.cleanup-once" >/dev/null 2>&1 || true
rm -f "${HOME}/Library/LaunchAgents/io.vectorize.cocoindex.cleanup-once.plist"
log "self-unloaded io.vectorize.cocoindex.cleanup-once -- will not run again"
