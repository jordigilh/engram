#!/usr/bin/env bash
# Periodic refresh of the branch-scoped mirror worktrees (see
# the local watch-mirrors-config.sh for why this exists). Invoked every ~10
# minutes by launchd/io.vectorize.cocoindex.watch-sync.plist, independent of
# the nightly hindsight-api restart and independent of whatever branch is
# checked out in the live dev clones. Safe to also run manually.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${HOME}/.engram/logs/watch-mirrors-sync.log"
CONFIG_FILE="${ENGRAM_WATCH_MIRRORS_CONFIG:-${HOME}/.engram/watch-mirrors-config.sh}"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $*" >>"$LOG_FILE"
}

# shellcheck disable=SC1090
[[ -r "$CONFIG_FILE" ]] || {
    printf 'refresh-watch-mirrors: missing local config: %s\n' "$CONFIG_FILE" >&2
    exit 1
}
source "$CONFIG_FILE"
# shellcheck source=watch-mirrors-lib.sh
source "${SCRIPT_DIR}/watch-mirrors-lib.sh"

sync_all_mirrors
