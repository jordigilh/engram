#!/usr/bin/env bash
# One-time (idempotent) bootstrap: creates the branch-scoped mirror
# worktrees declared in the local watch-mirrors-config.sh. Safe to re-run at any
# time -- existing mirrors are synced to latest instead of recreated.
#
# Run this once manually after cloning, or after adding a new entry to
# ~/.engram/watch-mirrors-config.sh. Ongoing refresh is handled automatically by
# refresh-watch-mirrors.sh via launchd/io.vectorize.cocoindex.watch-sync.plist.
# See docs/FINDINGS.md 2026-08-03 for why these mirrors exist.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${HOME}/.engram/logs/watch-mirrors-setup.log"
CONFIG_FILE="${ENGRAM_WATCH_MIRRORS_CONFIG:-${HOME}/.engram/watch-mirrors-config.sh}"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $*" | tee -a "$LOG_FILE"
}

# shellcheck disable=SC1090
[[ -r "$CONFIG_FILE" ]] || {
    printf 'setup-watch-mirrors: missing local config: %s\n' "$CONFIG_FILE" >&2
    exit 1
}
source "$CONFIG_FILE"
# shellcheck source=watch-mirrors-lib.sh
source "${SCRIPT_DIR}/watch-mirrors-lib.sh"

log "bootstrap start: ${#WATCH_MIRRORS[@]} mirrors configured"
sync_all_mirrors
log "bootstrap complete"
