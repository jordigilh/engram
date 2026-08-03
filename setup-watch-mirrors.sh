#!/usr/bin/env bash
# One-time (idempotent) bootstrap: creates the branch-scoped mirror
# worktrees declared in watch-mirrors-config.sh. Safe to re-run at any
# time -- existing mirrors are synced to latest instead of recreated.
#
# Run this once manually after cloning, or after adding a new entry to
# watch-mirrors-config.sh. Ongoing refresh is handled automatically by
# refresh-watch-mirrors.sh via launchd/io.vectorize.cocoindex.watch-sync.plist.
# See docs/FINDINGS.md 2026-08-03 for why these mirrors exist.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${HOME}/.hindsight/logs/watch-mirrors-setup.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $*" | tee -a "$LOG_FILE"
}

# shellcheck source=watch-mirrors-config.sh
source "${SCRIPT_DIR}/watch-mirrors-config.sh"
# shellcheck source=watch-mirrors-lib.sh
source "${SCRIPT_DIR}/watch-mirrors-lib.sh"

log "bootstrap start: ${#WATCH_MIRRORS[@]} mirrors configured"
sync_all_mirrors
log "bootstrap complete"
