#!/usr/bin/env bash
# Single source of truth for which repos/branches CocoIndex's docs_app and
# code_app (cocoindex-flows.py, engram-cocoindex-flows.py) are allowed to
# ingest from.
#
# Why this exists (see docs/FINDINGS.md 2026-08-03): docs_app/code_app used
# to localfs.walk_dir(..., live=True) directly against the user's live,
# actively branch-switched dev clones. Every git checkout/pull from routine
# PR work touched file mtimes for whatever differed between the two
# branches -- CocoIndex correctly detected that as "changed" and
# reprocessed it, but the delta was meaningless branch-switching noise, not
# real content evolution. One 5-minute branch-switching sequence on
# kubernaut touched 135 files, each paying a full hindsight_retain() +
# Sonnet consolidation pass.
#
# Fix: docs_app/code_app now only ever read from the dedicated mirror
# worktrees below, kept in sync by watch-mirrors-lib.sh via fetch + reset
# --hard on a single branch's linear history. That's a real content-based
# delta -- git only rewrites the mtime of a file if its blob actually
# changed between two commits on the SAME branch, unlike jumping between
# divergent feature branches.
#
# Format: "name|live_clone_path|branch|mirror_path". Add a line to onboard
# a new repo (e.g. dcm, koku) -- no script changes needed. Keep names
# lowercase-hyphenated, matching the live clone's directory name.
WATCH_MIRRORS=(
    "kubernaut|${HOME}/go/src/github.com/jordigilh/kubernaut|main|${HOME}/.hindsight/watch/kubernaut"
    "kubernaut-operator|${HOME}/go/src/github.com/jordigilh/kubernaut-operator|main|${HOME}/.hindsight/watch/kubernaut-operator"
    "kubernaut-console|${HOME}/go/src/github.com/jordigilh/kubernaut-console|main|${HOME}/.hindsight/watch/kubernaut-console"
    "kubernaut-demo-scenarios|${HOME}/go/src/github.com/jordigilh/kubernaut-demo-scenarios|main|${HOME}/.hindsight/watch/kubernaut-demo-scenarios"
    "kubernaut-docs|${HOME}/go/src/github.com/jordigilh/kubernaut-docs|main|${HOME}/.hindsight/watch/kubernaut-docs"
    "engram|${HOME}/go/src/github.com/jordigilh/engram|main|${HOME}/.hindsight/watch/engram"
)
