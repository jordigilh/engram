#!/usr/bin/env bash
# Periodic refresh of the branch-scoped mirror worktrees (see
# watch-mirrors-config.sh for the why this exists). Invoked every ~10
# minutes by launchd/io.vectorize.cocoindex.watch-sync.plist, independent of
# the nightly hindsight-api restart and independent of whatever branch is
# checked out in the live dev clones. Safe to also run manually.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${HOME}/.hindsight/logs/watch-mirrors-sync.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $*" >>"$LOG_FILE"
}

# shellcheck source=watch-mirrors-config.sh
source "${SCRIPT_DIR}/watch-mirrors-config.sh"
# shellcheck source=watch-mirrors-lib.sh
source "${SCRIPT_DIR}/watch-mirrors-lib.sh"

sync_all_mirrors
