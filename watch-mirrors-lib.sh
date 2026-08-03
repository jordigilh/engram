#!/usr/bin/env bash
# Shared logic for creating/refreshing the branch-scoped mirror worktrees
# declared in watch-mirrors-config.sh. Sourced by both setup-watch-mirrors.sh
# (one-time/manual bootstrap) and refresh-watch-mirrors.sh (periodic launchd
# sync) so the two can never drift apart -- see docs/FINDINGS.md 2026-08-03.
#
# Callers must define a `log()` function and set LOG_FILE before sourcing
# this file's functions are invoked (see either script for the pattern).
set -euo pipefail

# ensure_mirror NAME LIVE_CLONE BRANCH MIRROR_PATH
#
# Idempotent: creates the mirror worktree if it doesn't exist yet, otherwise
# fast-forwards it to the latest origin/BRANCH. Never touches LIVE_CLONE's
# own checked-out branch or working tree.
ensure_mirror() {
    local name="$1" live_clone="$2" branch="$3" mirror_path="$4"

    if [ ! -e "${live_clone}/.git" ]; then
        log "SKIP ${name}: no git repo at ${live_clone}"
        return 1
    fi

    if ! git -C "$live_clone" fetch origin "$branch" --quiet; then
        log "ERROR ${name}: git fetch origin ${branch} failed"
        return 1
    fi

    if [ -d "$mirror_path" ]; then
        if git -C "$live_clone" worktree list --porcelain | grep -qx "worktree ${mirror_path}"; then
            git -C "$mirror_path" reset --hard "origin/${branch}" --quiet
            log "synced ${name} -> $(git -C "$mirror_path" rev-parse --short HEAD) (origin/${branch})"
        else
            log "SKIP ${name}: ${mirror_path} exists but is not a registered worktree of ${live_clone} -- resolve manually"
            return 1
        fi
    else
        mkdir -p "$(dirname "$mirror_path")"
        git -C "$live_clone" worktree add --detach --quiet "$mirror_path" "origin/${branch}"
        log "created mirror ${name} at ${mirror_path} -> $(git -C "$mirror_path" rev-parse --short HEAD) (origin/${branch})"
    fi
}

# sync_all_mirrors: loops WATCH_MIRRORS (from watch-mirrors-config.sh), best
# effort -- one repo's failure (e.g. clone moved/deleted) doesn't stop the
# others from being synced.
sync_all_mirrors() {
    for entry in "${WATCH_MIRRORS[@]}"; do
        IFS='|' read -r name live_clone branch mirror_path <<<"$entry"
        ensure_mirror "$name" "$live_clone" "$branch" "$mirror_path" || true
    done
}
